"""Agent upload, build, and tracked execution routes."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile

from .agent_helpers import (
    MAX_UPLOAD_SIZE,
    read_extracted_files as _read_extracted_files,
    read_upload_bytes_limited as _read_upload_bytes_limited,
    safe_extract_tar as _safe_extract_tar,
    spawn_bg as _spawn_bg,
    tenant_image_tag as _tenant_image_tag,
    validate_inspection_mode as _validate_inspection_mode,
)
from .room_helpers import (
    load_room_for_caller as _load_room_for_caller,
    room_prompt_for_run as _room_prompt_for_run,
    room_query_inspection_mode as _room_query_inspection_mode,
    room_wrap_id as _room_wrap_id,
    validate_room_provider as _validate_room_provider,
)
from ..config import Settings
from ..core import Hivemind
from ..models import QueryRequest
from ..sandbox.settings import build_sandbox_settings
from ..tenants import Caller

logger = logging.getLogger(__name__)


def register_agent_upload_routes(
    app: FastAPI,
    settings: Settings,
    bearer: Callable[[Request], str],
    requires_role: Callable[..., Callable],
    get_tenant_hive: Callable,
    require_scope_agent_id: Callable,
    ensure_scope_agent_exists: Callable,
    prepare_billing_hold: Callable,
    settle_empty_billing: Callable,
    billing_provider_for_room: Callable,
    billing_models_for_query: Callable,
) -> None:
    _bearer = bearer
    _require_scope_agent_id = require_scope_agent_id
    _ensure_scope_agent_exists = ensure_scope_agent_exists
    _prepare_billing_hold = prepare_billing_hold
    _settle_empty_billing = settle_empty_billing
    _billing_provider_for_room = billing_provider_for_room
    _billing_models_for_query = billing_models_for_query

    from ..sandbox.models import AgentConfig

    # ── Room agent upload ──

    @app.post("/v1/room-agents")
    async def upload_agent(
        name: str = Form(...),
        archive: UploadFile = File(...),
        description: str = Form(""),
        agent_type: str = Form("query"),
        entrypoint: str | None = Form(None),
        memory_mb: Annotated[int, Form(ge=16)] = 256,
        max_llm_calls: Annotated[int, Form(ge=1)] = 20,
        max_tokens: Annotated[int, Form(ge=1)] = 100_000,
        timeout_seconds: Annotated[int, Form(ge=1)] = 120,
        # JSON-encoded list of file paths to mark non-attestable (e.g.
        # secret prompts, .env). Excluded from attested_files_digest;
        # still bound by image_digest. Defaults to []  (all attestable).
        private_paths: str = Form("[]"),
        # 'full' or 'sealed'. Room query-agent uploads use the room key;
        # reusable room agents are tenant-sealed or KMS-sealed depending on mode.
        inspection_mode: str = Form("full"),
        hm: Hivemind = Depends(get_tenant_hive),
    ):
        try:
            parsed_private = json.loads(private_paths) if private_paths else []
            if not isinstance(parsed_private, list) or not all(
                isinstance(p, str) for p in parsed_private
            ):
                raise ValueError("must be JSON list of strings")
        except (ValueError, json.JSONDecodeError) as e:
            raise HTTPException(
                status_code=400,
                detail=f"private_paths: {e}",
            )
        validated_mode = _validate_inspection_mode(inspection_mode)
        try:
            content = await _read_upload_bytes_limited(
                archive,
                max_bytes=MAX_UPLOAD_SIZE,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=str(e),
            )

        tmpdir = tempfile.mkdtemp(prefix="hivemind-upload-")
        try:
            _safe_extract_tar(content, tmpdir)
        except (tarfile.TarError, ValueError) as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise HTTPException(
                status_code=400,
                detail=f"Invalid archive: {e}",
            )
        except Exception:
            shutil.rmtree(tmpdir, ignore_errors=True)
            logger.exception("Unexpected archive extraction failure")
            raise HTTPException(
                status_code=500,
                detail="Archive extraction failed",
            )

        agent_id = uuid4().hex[:12]
        run_id = uuid4().hex[:12]

        await asyncio.to_thread(hm.run_store.create, run_id, agent_id)

        async def _build_upload_agent():
            try:
                from ..sandbox.backend import _create_runner

                sandbox_settings = build_sandbox_settings(settings)
                runner = _create_runner(sandbox_settings)

                await _build_single_agent(
                    runner,
                    tmpdir,
                    agent_id,
                    agent_type,
                    name,
                    description,
                    entrypoint,
                    min(memory_mb, settings.container_memory_mb),
                    max_llm_calls,
                    max_tokens,
                    timeout_seconds,
                    hm,
                    private_paths=parsed_private,
                    inspection_mode=validated_mode,
                )

                await asyncio.to_thread(
                    hm.run_store.update_status, run_id, "completed",
                )
            except Exception as e:
                logger.error("Background agent upload %s failed: %s", run_id, e)
                try:
                    await asyncio.to_thread(
                        hm.run_store.update_status, run_id, "failed",
                        error=str(e)[:500],
                    )
                except Exception:
                    pass
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        _spawn_bg(app, _build_upload_agent())

        return {"agent_id": agent_id, "run_id": run_id, "status": "pending"}

    # ── Internal multi-agent submit ──

    async def _build_single_agent(
        runner,
        tmpdir: str,
        agent_id: str,
        agent_type: str,
        name: str,
        description: str,
        entrypoint: str | None,
        memory_mb: int,
        max_llm_calls: int,
        max_tokens: int,
        timeout_seconds: int,
        hm: Hivemind,
        private_paths: list[str] | None = None,
        inspection_mode: str = "full",
        room_id: str | None = None,
    ) -> str:
        """Build Docker image, register agent, save files. Returns image tag."""
        image_tag = _tenant_image_tag(hm.tenant_id, agent_id)
        await runner.build_image_async(tmpdir, image_tag)

        config = AgentConfig(
            agent_id=agent_id,
            name=name,
            description=description,
            agent_type=agent_type,
            image=image_tag,
            entrypoint=entrypoint,
            memory_mb=memory_mb,
            max_llm_calls=max_llm_calls,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            inspection_mode=inspection_mode,
        )
        await asyncio.to_thread(hm.agent_store.create, config)

        # Persist the upload tmpdir (Dockerfile + source). On Phala, the
        # CVM root FS — including /var/lib/docker — is reinitialized on
        # every compose update, so per-agent images get wiped. Stored
        # build context lets ensure_image_async rebuild from pgdata
        # (FDE-encrypted, governance-gated) on next invocation.
        try:
            files = await asyncio.to_thread(_read_extracted_files, tmpdir)
            await asyncio.to_thread(
                hm.agent_store.save_files,
                agent_id,
                files,
                private_paths or [],
                inspection_mode,
                room_id,
            )
        except Exception as e:
            logger.warning("Failed to save agent files for %s: %s", agent_id, e)

        return image_tag


    # ── Room query-agent submit + run tracking (async-submit flow) ──

    @app.post("/v1/rooms/{room_id}/query-agents")
    async def submit_query_agent(
        room_id: str,
        request: Request,
        name: str = Form(...),
        archive: UploadFile = File(...),
        prompt: str = Form(""),
        description: str = Form(""),
        entrypoint: str | None = Form(None),
        memory_mb: Annotated[int, Form(ge=16)] = 256,
        max_llm_calls: Annotated[int, Form(ge=1)] = 20,
        max_tokens: Annotated[int, Form(ge=1)] = 100_000,
        timeout_seconds: Annotated[int, Form(ge=1)] = 120,
        mediator_agent_id: str | None = Form(None),
        # LLM routing (optional). ``provider="tinfoil"`` requires the
        # server to have HIVEMIND_TINFOIL_API_KEY configured.
        model: str | None = Form(None),
        provider: str | None = Form(None),
        policy: str | None = Form(None),
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        """Upload query agent source into a room and kick off execution."""
        hm = caller.hive
        room = await _load_room_for_caller(caller, room_id)
        if room.get("query_mode") != "uploadable":
            raise HTTPException(
                403,
                "this room uses a fixed query agent; uploads are disabled",
            )
        if not caller.constraints.get("can_upload_query_agent") and caller.role == "query":
            raise HTTPException(
                status_code=403,
                detail="this room invite may not upload query agents",
            )
        scope_agent_id = room["scope_agent_id"]
        room_policy = room.get("policy") or ""
        requested_policy = (policy or "").strip()
        if requested_policy and requested_policy != room_policy:
            raise HTTPException(
                400,
                "room policy is fixed by the signed room manifest; "
                "caller-supplied policy cannot override it",
            )
        policy = room_policy
        _validate_room_provider(provider, room)
        validated_mode = _validate_inspection_mode(
            _room_query_inspection_mode(room),
            require_kms=False,
        )
        scope_agent_id = _require_scope_agent_id(hm, scope_agent_id)
        await _ensure_scope_agent_exists(hm, scope_agent_id)

        # Honor the room's pinned mediator. Without this, a bilateral
        # room (uploadable query agent + pinned mediator) would silently
        # skip the mediator stage at run time — `submit_room_run` in
        # api/rooms.py uses apply_room_to_query_request to do the same
        # resolution, but this upload-and-run endpoint had its own form
        # parameter and ignored the manifest, leaking unmediated output.
        manifest = room.get("manifest") or {}
        manifest_mediator = manifest.get("mediator")
        if isinstance(manifest_mediator, dict):
            fixed_mediator_agent_id = (
                manifest_mediator.get("agent_id") or ""
            ).strip()
            requested_mediator_agent_id = (mediator_agent_id or "").strip()
            if fixed_mediator_agent_id:
                if (
                    requested_mediator_agent_id
                    and requested_mediator_agent_id != fixed_mediator_agent_id
                ):
                    raise HTTPException(
                        400,
                        "room mediator agent is fixed by the signed room "
                        "manifest; caller-supplied mediator cannot override "
                        "it",
                    )
                mediator_agent_id = fixed_mediator_agent_id
            elif requested_mediator_agent_id:
                raise HTTPException(
                    400,
                    "room manifest does not allow a mediator-agent override",
                )

        room_vault_items: list[dict] = []
        bearer = _bearer(request)
        await asyncio.to_thread(
            hm.room_vault.open,
            room["room_id"],
            _room_wrap_id(caller),
            bearer,
        )
        room_vault_items = await asyncio.to_thread(
            hm.room_vault.list_items,
            room["room_id"],
        )

        try:
            content = await _read_upload_bytes_limited(
                archive, max_bytes=MAX_UPLOAD_SIZE,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        tmpdir = tempfile.mkdtemp(prefix="hivemind-upload-")
        try:
            _safe_extract_tar(content, tmpdir)
        except (tarfile.TarError, ValueError) as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"Invalid archive: {e}")

        # Create run record immediately, return fast
        agent_id = uuid4().hex[:12]
        run_id = uuid4().hex[:12]
        billing_req = QueryRequest(
            query=prompt or "run uploaded room query agent",
            query_agent_id=agent_id,
            scope_agent_id=scope_agent_id,
            mediator_agent_id=mediator_agent_id,
            max_tokens=max_tokens,
            max_llm_calls=max_llm_calls,
            timeout_seconds=timeout_seconds,
            model=model,
            provider=provider,
            policy=policy,
        )
        billing = await _prepare_billing_hold(
            request,
            caller,
            hm,
            run_id=run_id,
            provider=_billing_provider_for_room(provider, room),
            models=_billing_models_for_query(hm, billing_req),
            max_tokens=min(
                max_tokens or hm.settings.default_query_max_tokens,
                hm.settings.max_tokens,
            ),
            billable_role="query",
        )
        await asyncio.to_thread(
            hm.run_store.create, run_id, agent_id,
            scope_agent_id=scope_agent_id,
            issuer_token_id=(caller.token_id or None),
            payer_tenant_id=billing.get("payer_tenant_id"),
            payer_token_id=billing.get("payer_token_id"),
            billable_role=billing.get("billable_role"),
            billing_provider=billing.get("billing_provider"),
            billing_model=billing.get("billing_model"),
            billing_hold_micro_usd=int(billing.get("billing_hold_micro_usd") or 0),
            billing_status=billing.get("billing_status") or "unbilled",
            room_id=(room or {}).get("room_id"),
            room_manifest_hash=(room or {}).get("manifest_hash"),
            prompt=_room_prompt_for_run(room, prompt),
            output_visibility=(room or {}).get(
                "output_visibility", "owner_and_querier"
            ),
            artifacts_enabled=bool((room or {}).get("allow_artifacts", True)),
        )

        # Everything else runs in background
        _spawn_bg(
            app,
            _build_and_run(
                hm=hm,
                settings=settings,
                tmpdir=tmpdir,
                agent_id=agent_id,
                run_id=run_id,
                name=name,
                description=description,
                entrypoint=entrypoint,
                memory_mb=memory_mb,
                max_llm_calls=max_llm_calls,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                prompt=prompt,
                scope_agent_id=scope_agent_id,
                mediator_agent_id=mediator_agent_id,
                model=model,
                provider=provider,
                policy=policy,
                inspection_mode=validated_mode,
                room=room,
                room_vault_items=room_vault_items,
                payer_tenant_id=billing.get("payer_tenant_id"),
                payer_token_id=billing.get("payer_token_id"),
                billable_role=billing.get("billable_role") or "query",
                billing_provider=billing.get("billing_provider"),
                billing_model=billing.get("billing_model"),
                billing_hold_micro_usd=int(
                    billing.get("billing_hold_micro_usd") or 0
                ),
            ),
        )

        return {
            "run_id": run_id,
            "agent_id": agent_id,
            "room_id": (room or {}).get("room_id"),
            "status": "pending",
            "inspection_mode": validated_mode,
        }

    async def _build_and_run(
        hm: Hivemind,
        settings: Settings,
        tmpdir: str,
        agent_id: str,
        run_id: str,
        name: str,
        description: str,
        entrypoint: str | None,
        memory_mb: int,
        max_llm_calls: int,
        max_tokens: int,
        timeout_seconds: int,
        prompt: str,
        scope_agent_id: str | None,
        mediator_agent_id: str | None,
        model: str | None = None,
        provider: str | None = None,
        policy: str | None = None,
        inspection_mode: str = "full",
        room: dict | None = None,
        room_vault_items: list[dict] | None = None,
        payer_tenant_id: str | None = None,
        payer_token_id: str | None = None,
        billable_role: str = "query",
        billing_provider: str | None = None,
        billing_model: str | None = None,
        billing_hold_micro_usd: int = 0,
    ) -> None:
        """Background task: build image, register agent, run pipeline."""
        from ..sandbox.backend import _create_runner

        billing = {
            "payer_tenant_id": payer_tenant_id,
            "payer_token_id": payer_token_id,
            "billing_provider": billing_provider,
            "billing_model": billing_model,
            "billing_hold_micro_usd": billing_hold_micro_usd,
        }
        try:
            # -- Build Docker image --
            import time as _time

            build_t0 = _time.time()
            await asyncio.to_thread(
                hm.run_store.update_stage, run_id, "build", started_at=build_t0,
            )

            sandbox_settings = build_sandbox_settings(settings)
            runner = _create_runner(sandbox_settings)
            image_tag = _tenant_image_tag(hm.tenant_id, agent_id)

            # Capture upload tmpdir (Dockerfile + source) before the
            # finally block rmtree's it — needed for rebuild-from-pgdata
            # after a Phala compose update wipes /var/lib/docker.
            captured_files: dict[str, str] = {}
            try:
                await runner.build_image_async(tmpdir, image_tag)
                try:
                    captured_files = _read_extracted_files(tmpdir)
                except Exception as e:
                    logger.warning(
                        "Failed to read upload context for %s: %s",
                        agent_id, e,
                    )
            except Exception as e:
                logger.exception("Image build failed for agent %s", agent_id)
                await asyncio.to_thread(
                    hm.run_store.update_status, run_id, "failed",
                    error=f"Image build failed: {e}",
                )
                await _settle_empty_billing(
                    hm,
                    run_id,
                    billing,
                    billable_role=billable_role,
                )
                return
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
                await asyncio.to_thread(
                    hm.run_store.update_stage, run_id, "build",
                    ended_at=_time.time(),
                )

            # -- Register agent --
            config = AgentConfig(
                agent_id=agent_id,
                name=name,
                description=description,
                agent_type="query",
                image=image_tag,
                entrypoint=entrypoint,
                memory_mb=min(memory_mb, settings.container_memory_mb),
                max_llm_calls=max_llm_calls,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                inspection_mode=inspection_mode,
            )
            await asyncio.to_thread(hm.agent_store.create, config)

            if captured_files:
                try:
                    await asyncio.to_thread(
                        hm.agent_store.save_files,
                        agent_id,
                        captured_files,
                        None,
                        inspection_mode,
                        (room or {}).get("room_id"),
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to save agent files for %s: %s", agent_id, e,
                    )

            # -- Run pipeline --
            await hm.pipeline.run_query_agent_tracked(
                agent_id=agent_id,
                run_id=run_id,
                run_store=hm.run_store,
                artifact_store=hm.artifact_store,
                artifact_retention_seconds=hm.settings.artifact_retention_seconds,
                prompt=prompt,
                scope_agent_id=scope_agent_id,
                mediator_agent_id=mediator_agent_id,
                max_tokens=max_tokens,
                model=model,
                provider=provider,
                policy=policy,
                room_id=(room or {}).get("room_id"),
                room_manifest_hash=(room or {}).get("manifest_hash"),
                output_visibility=(room or {}).get(
                    "output_visibility", "owner_and_querier"
                ),
                allowed_llm_providers=(room or {}).get("allowed_llm_providers"),
                artifacts_enabled=bool((room or {}).get("allow_artifacts", True)),
                room_vault_items=room_vault_items or [],
                payer_tenant_id=payer_tenant_id,
                payer_token_id=payer_token_id,
                billable_role=billable_role,
                billing_provider=billing_provider,
                billing_model=billing_model,
                billing_hold_micro_usd=billing_hold_micro_usd,
            )

        except Exception as e:
            logger.error("Background build+run %s failed: %s", run_id, e)
            try:
                await asyncio.to_thread(
                    hm.run_store.update_status, run_id, "failed",
                    error=str(e)[:500],
                )
                await _settle_empty_billing(
                    hm,
                    run_id,
                    billing,
                    billable_role=billable_role,
                )
            except Exception:
                pass

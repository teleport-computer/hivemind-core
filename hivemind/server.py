import asyncio
import base64
import json
import logging
import os
import secrets
import shutil
import tarfile
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import uuid4
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .config import Settings
from .core import Hivemind
from .models import (
    HealthResponse,
    IndexRequest,
    IndexResponse,
    QueryRequest,
    StoreRequest,
    StoreResponse,
)
from .rooms import (
    RoomCreateRequest,
    RoomRunRequest,
    RoomTrustUpdateRequest,
    RoomVaultItemRequest,
    build_room_manifest,
    inspection_mode_from_visibility,
    room_constraints,
    sign_manifest,
    verify_room_envelope,
    visibility_from_inspection_mode,
)
from .room_vault import RoomVaultSealed
from .sandbox.settings import build_sandbox_settings
from .tenants import Caller, DuplicateTenantNameError, Role, TenantRegistry
from .version import APP_VERSION

logger = logging.getLogger(__name__)

_IGNORED_TAR_TYPES = {
    tarfile.XHDTYPE,         # PAX extended header
    tarfile.XGLTYPE,         # PAX global header
    tarfile.GNUTYPE_LONGNAME,
    tarfile.GNUTYPE_LONGLINK,
}

def _image_digest(image: str) -> dict:
    """Return ``{id, repo_digests}`` for a tagged Docker image.

    ``id`` is the local content-addressable sha256 of the image's config
    manifest (always present, equivalent to ``docker images --digests``).
    ``repo_digests`` is the registry-side digest list (only present when
    the image was pulled or pushed). Together they let an external
    verifier pin "the bytes that ran" by either local id or registry
    digest.

    Fail-soft: returns ``{"id": "", "repo_digests": []}`` if Docker isn't
    available or the image isn't loaded — attestation still succeeds, the
    consumer just can't pin the image layer (the source-files digest +
    compose_hash still cover most of what they care about).
    """
    try:
        import docker
        client = docker.from_env()
        attrs = client.images.get(image).attrs
        return {
            "id": attrs.get("Id", "") or "",
            "repo_digests": list(attrs.get("RepoDigests") or []),
        }
    except Exception as e:
        logger.debug("image digest lookup failed for %r: %s", image, e)
        return {"id": "", "repo_digests": []}


def _tenant_image_tag(tenant_id: str | None, agent_id: str) -> str:
    """Scope docker image tags by tenant so shared daemons don't collide.

    Tenant IDs are of the form ``t_<hex>`` (see tenants.py). Returns
    ``hivemind-agent-<tenant_id>-<agent_id>:latest`` when tenant_id is
    present, else an unscoped tag (tests / CLI-only paths).
    """
    if tenant_id:
        return f"hivemind-agent-{tenant_id}-{agent_id}:latest"
    return f"hivemind-agent-{agent_id}:latest"


MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB compressed archive bytes
MAX_UPLOAD_TAR_MEMBERS = 2_000
MAX_UPLOAD_TAR_MEMBER_BYTES = 15 * 1024 * 1024  # 15 MB per file
MAX_UPLOAD_TAR_TOTAL_BYTES = 150 * 1024 * 1024  # 150 MB total extracted size



async def _read_upload_bytes_limited(
    upload: UploadFile,
    *,
    max_bytes: int,
    chunk_size: int = 1024 * 1024,
) -> bytes:
    """Read upload content in chunks and stop once the byte cap is exceeded."""
    total = 0
    chunks: list[bytes] = []
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(
                f"Archive too large ({total} bytes). Max: {max_bytes} bytes."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_extract_tar(
    archive_bytes: bytes,
    extract_to: str,
    *,
    max_members: int = MAX_UPLOAD_TAR_MEMBERS,
    max_member_bytes: int = MAX_UPLOAD_TAR_MEMBER_BYTES,
    max_total_bytes: int = MAX_UPLOAD_TAR_TOTAL_BYTES,
) -> None:
    """Extract a tar archive while rejecting path traversal and link entries."""
    import io

    base = Path(extract_to).resolve()
    member_count = 0
    total_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tar:
        for member in tar.getmembers():
            if member.type in _IGNORED_TAR_TYPES:
                continue

            member_count += 1
            if member_count > max_members:
                raise ValueError(
                    f"Archive has too many entries ({member_count} > {max_members})"
                )

            target = (base / member.name).resolve()
            if target != base and base not in target.parents:
                raise ValueError(f"Invalid archive member path: {member.name}")

            if member.issym() or member.islnk():
                raise ValueError(f"Symlink entries are not allowed: {member.name}")

            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            if not member.isfile():
                raise ValueError(f"Unsupported archive member: {member.name}")

            member_size = int(member.size or 0)
            if member_size < 0:
                raise ValueError(f"Invalid archive member size: {member.name}")
            if member_size > max_member_bytes:
                raise ValueError(
                    f"Archive member too large ({member.name}: {member_size} bytes)"
                )
            total_bytes += member_size
            if total_bytes > max_total_bytes:
                raise ValueError(
                    f"Archive expands beyond limit ({total_bytes} > {max_total_bytes})"
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                raise ValueError(f"Invalid archive member: {member.name}")
            with src, open(target, "wb") as dst:
                remaining = member_size
                while remaining > 0:
                    chunk = src.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError(
                            f"Unexpected end of archive while extracting {member.name}"
                        )
                    dst.write(chunk)
                    remaining -= len(chunk)

            file_mode = member.mode & 0o777
            os.chmod(target, file_mode or 0o644)


def _validate_inspection_mode(mode: str, *, require_kms: bool = True) -> str:
    """Coerce/validate an owner-side ``inspection_mode`` form field.

    Inspection policy is per-agent: A picks ``full`` or ``sealed`` once,
    when uploading the scope agent. Uploaded query agents (B's path)
    inherit the bound scope agent's mode — they don't get to pick.

    Returns the normalized mode. The ``require_kms`` argument is retained
    for older call sites, but sealed room agents now use room or tenant
    keys rather than an operator-held agent KMS key.
    """
    m = (mode or "full").strip().lower() or "full"
    if m not in {"full", "sealed"}:
        raise HTTPException(
            status_code=400,
            detail=(
                f"inspection_mode must be one of 'full', 'sealed' "
                f"(got {mode!r})"
            ),
        )
    return m

def _read_extracted_files(tmpdir: str) -> dict[str, str]:
    """Read all extracted source files from a directory as {path: content}."""
    files: dict[str, str] = {}
    base = Path(tmpdir)
    for fpath in sorted(base.rglob("*")):
        if not fpath.is_file():
            continue
        rel = str(fpath.relative_to(base))
        # Skip hidden files and __pycache__
        if any(part.startswith(".") or part == "__pycache__" for part in rel.split("/")):
            continue
        try:
            files[rel] = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return files


def _spawn_bg(app: FastAPI, coro) -> asyncio.Task:
    """Schedule a fire-and-forget coroutine and pin a strong ref.

    Uses ``app.state.background_tasks`` (initialized in lifespan) so the
    event loop won't GC the task mid-flight, and so lifespan teardown can
    cancel-and-drain everything cleanly. Falls back to a bare
    ``create_task`` when state isn't initialized (e.g. tests that call
    handlers directly without going through lifespan).
    """
    task = asyncio.create_task(coro)
    bg = getattr(app.state, "background_tasks", None)
    if isinstance(bg, set):
        bg.add(task)
        task.add_done_callback(bg.discard)
    return task


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Strong-ref set for fire-and-forget tasks so the event loop can't
        # GC them mid-flight (per asyncio docs) and so we can cancel them
        # cleanly on shutdown. Spawning code uses _spawn_bg below.
        background_tasks: set[asyncio.Task] = set()
        app.state.background_tasks = background_tasks

        # Fetch TDX quote + measurements for /v1/attestation. Cheap,
        # cached for process lifetime; falls back to ready=false outside
        # a TEE so local dev boots normally.
        try:
            from . import attestation
            await asyncio.to_thread(attestation.bootstrap)
        except Exception as e:
            logger.warning("attestation bootstrap raised: %s", e)

        # Kick off agent-base image provisioning in the background — do
        # NOT block HTTP readiness on it. GHCR pull can fail (private
        # repo, network blip) and fall back to a multi-minute inline
        # Dockerfile build that will OOM-kill a 2GB CVM. When that ran
        # under `await`, lifespan never completed and uvicorn served
        # "Empty reply from server" until the whole container restart-
        # looped. Uploading agents before this task completes returns
        # the usual "agent-base not present" error — acceptable vs. a
        # hung control plane.
        async def _bootstrap_agent_base():
            try:
                from .agent_base_bootstrap import ensure_agent_base_image
                await asyncio.to_thread(ensure_agent_base_image)
            except Exception as e:
                logger.warning("agent-base bootstrap raised: %s", e)

        agent_base_task = _spawn_bg(app, _bootstrap_agent_base())

        registry = TenantRegistry(settings)
        app.state.registry = registry
        app.state.agent_base_task = agent_base_task
        yield
        # Cancel + drain any in-flight pipeline / upload runs before we
        # shut down the registry, so they don't crash mid-DB-call writing
        # against a closed connection.
        if background_tasks:
            for t in list(background_tasks):
                t.cancel()
            await asyncio.gather(*background_tasks, return_exceptions=True)
        # Close per-tenant Hivemind instances + control DB.
        await asyncio.to_thread(registry.close)

    app = FastAPI(title="Hivemind Core", version=APP_VERSION, lifespan=lifespan)

    # Translate TenantSealed (raised when an operation needs the
    # tenant's DEK but no valid bearer has thawed it since process
    # start) to a clear 503. Capability-token holders see this until
    # the owner interacts with the system after a redeploy.
    from .seal import TenantSealed as _TenantSealed
    from fastapi.responses import JSONResponse as _JSONResponse

    @app.exception_handler(_TenantSealed)
    async def _on_tenant_sealed(_request, exc):  # pragma: no cover
        return _JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "Tenant is sealed: encrypted data cannot be read "
                    "until the owner (hmk_) interacts after the last "
                    "process restart. Have the tenant owner make any "
                    "request, then retry."
                ),
                "error": str(exc),
            },
        )

    @app.exception_handler(RoomVaultSealed)
    async def _on_room_vault_sealed(_request, exc):  # pragma: no cover
        return _JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "Room data is sealed: encrypted room data cannot be "
                    "read until a room participant presents a bearer token "
                    "that has a key wrap for this room."
                ),
                "error": str(exc),
            },
        )

    cors_origins = [
        origin.strip()
        for origin in (settings.cors_allow_origins or "").split(",")
        if origin.strip()
    ]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def _registry(request: Request) -> TenantRegistry:
        return request.app.state.registry

    def _bearer(request: Request) -> str:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return auth.removeprefix("Bearer ").strip()

    async def _payer_for_request(
        request: Request,
        caller: Caller,
        *,
        billable_role: str,
    ) -> dict:
        """Resolve who pays for this run.

        Query-token calls can attach ``X-Hivemind-Payer-Key: hmk_...`` so
        the data owner is not charged for the participant's LLM spend. Owner
        calls default to the owner tenant. When credit enforcement is enabled,
        query-token calls without a payer key are rejected before work starts.
        """
        payer_key = (request.headers.get("X-Hivemind-Payer-Key") or "").strip()
        if payer_key:
            payer = await asyncio.to_thread(
                _registry(request).resolve_payer_key,
                payer_key,
            )
            if payer is None:
                raise HTTPException(401, "invalid payer credential")
            return {
                "payer_tenant_id": payer["tenant_id"],
                "payer_token_id": payer.get("payer_token_id") or "",
                "billable_role": billable_role,
            }
        if caller.role == "owner":
            return {
                "payer_tenant_id": caller.tenant_id,
                "payer_token_id": "",
                "billable_role": billable_role,
            }
        if settings.billing_enforce_credits:
            raise HTTPException(
                402,
                "query-token runs require X-Hivemind-Payer-Key when "
                "HIVEMIND_BILLING_ENFORCE_CREDITS is enabled",
            )
        return {
            "payer_tenant_id": None,
            "payer_token_id": caller.token_id or "",
            "billable_role": billable_role,
        }

    def _billing_provider_for_room(req_provider: str | None, room: dict | None) -> str:
        if room is None:
            return (req_provider or "openrouter").strip().lower()
        allowed = [
            p.strip().lower()
            for p in room.get("allowed_llm_providers") or []
            if p.strip()
        ]
        if not allowed:
            return ""
        return (req_provider or allowed[0]).strip().lower()

    def _billing_models_for_query(hm: Hivemind, req: QueryRequest) -> list[str]:
        roles = ["scope", "query"]
        if req.mediator_agent_id or hm.settings.default_mediator_agent:
            roles.append("mediator")
        models: list[str] = []
        for role in roles:
            model = hm.pipeline._model_for(role, req.model)
            if model and model not in models:
                models.append(model)
        return models

    async def _prepare_billing_hold(
        request: Request,
        caller: Caller,
        hm: Hivemind,
        *,
        run_id: str,
        provider: str,
        models: list[str],
        max_tokens: int,
        billable_role: str,
    ) -> dict:
        payer = await _payer_for_request(
            request,
            caller,
            billable_role=billable_role,
        )
        hold = {"hold_micro_usd": 0, "status": "unbilled"}
        if payer.get("payer_tenant_id"):
            try:
                hold = await asyncio.to_thread(
                    _registry(request).billing_hold_for_run,
                    tenant_id=payer["payer_tenant_id"],
                    payer_token_id=payer.get("payer_token_id") or "",
                    run_id=run_id,
                    provider=provider,
                    models=models,
                    max_tokens=max_tokens,
                    billable_role=billable_role,
                    enforce=settings.billing_enforce_credits,
                )
            except ValueError as e:
                detail = str(e)
                status = 402 if "insufficient billing credit" in detail else 400
                raise HTTPException(status, detail)
        return {
            **payer,
            "billing_provider": provider,
            "billing_model": ",".join(models),
            "billing_hold_micro_usd": int(hold.get("hold_micro_usd") or 0),
            "billing_status": hold.get("status") or "unbilled",
        }

    async def _settle_empty_billing(
        hm: Hivemind,
        run_id: str,
        billing: dict,
        *,
        billable_role: str,
    ) -> None:
        if not billing.get("payer_tenant_id") or hm.billing_meter is None:
            return
        usage = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "max_tokens": 0,
            "stages": {},
        }
        try:
            settlement = await asyncio.to_thread(
                hm.billing_meter.settle_run,
                payer_tenant_id=billing.get("payer_tenant_id"),
                payer_token_id=billing.get("payer_token_id") or "",
                run_id=run_id,
                usage=usage,
                hold_micro_usd=int(billing.get("billing_hold_micro_usd") or 0),
                billable_role=billable_role,
                default_provider=billing.get("billing_provider"),
                default_model=billing.get("billing_model"),
            )
            if hasattr(hm.run_store, "update_usage"):
                await asyncio.to_thread(
                    hm.run_store.update_usage,
                    run_id,
                    usage,
                    billing_cost_micro_usd=int(
                        settlement.get("cost_micro_usd") or 0
                    ),
                    billing_status=settlement.get("billing_status") or "settled",
                    billing_settled_at=settlement.get("settled_at"),
                )
        except Exception as e:
            logger.warning("empty billing settlement failed for %s: %s", run_id, e)

    async def get_caller(request: Request) -> Caller:
        """Auth + role resolution. Recognizes hmk_ (owner) and hmq_
        (query capability) tokens.

        Stashes ``tenant_id`` and ``caller`` on ``request.state`` so
        downstream code (logging, role-specific handlers) can read them
        without re-resolving. 401 on any missing/invalid/revoked token.
        """
        registry = _registry(request)
        token = _bearer(request)
        caller = await asyncio.to_thread(registry.resolve_any, token)
        if caller is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        request.state.tenant_id = caller.tenant_id
        request.state.caller = caller
        return caller

    def requires_role(*roles: Role):
        """Build a FastAPI dependency that gates by caller role.

        Use as ``Depends(requires_role("owner"))`` or
        ``Depends(requires_role("owner", "query"))``. Returns the resolved
        :class:`Caller` so handlers can read constraints / hive directly.
        """
        allowed = set(roles)

        async def _dep(caller: Caller = Depends(get_caller)) -> Caller:
            if caller.role not in allowed:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"role '{caller.role}' not permitted "
                        f"(need one of: {sorted(allowed)})"
                    ),
                )
            return caller

        return _dep

    async def get_tenant_hive(
        caller: Caller = Depends(requires_role("owner")),
    ) -> Hivemind:
        """Owner-only Hivemind dependency.

        Backward-compatible shim for endpoints that pre-date capability
        tokens — they all run as the tenant owner. New endpoints that
        accept query tokens should depend on :func:`get_caller` or
        :func:`requires_role` directly.
        """
        return caller.hive

    def _require_scope_agent_id(hm: Hivemind, scope_agent_id: str | None) -> str:
        """Resolve the effective scope agent or reject the request up front."""
        resolved = (scope_agent_id or hm.settings.default_scope_agent or "").strip()
        if not resolved:
            raise HTTPException(
                400,
                "scope_agent_id is required (no default scope agent configured)",
            )
        return resolved

    async def _ensure_scope_agent_exists(hm: Hivemind, scope_agent_id: str) -> None:
        agent = await asyncio.to_thread(hm.agent_store.get, scope_agent_id)
        if not agent:
            raise HTTPException(404, f"Scope agent '{scope_agent_id}' not found")

    def _caller_can_access_run(caller: Caller, run: dict) -> bool:
        """Owner sees tenant runs; query tokens see only their own runs."""
        if caller.role == "owner":
            return True
        return bool(caller.token_id) and run.get("issuer_token_id") == caller.token_id

    def _caller_can_access_run_payload(caller: Caller, run: dict) -> bool:
        """Whether caller may see output text and artifacts for a run."""
        if not _caller_can_access_run(caller, run):
            return False
        visibility = (run.get("output_visibility") or "owner_and_querier").strip()
        # In a room, A can intentionally choose querier-only results for
        # B-initiated runs. A still sees metadata/audit rows, but not the
        # answer/artifacts. Owner-initiated runs remain visible to owner.
        if (
            caller.role == "owner"
            and run.get("room_id")
            and run.get("issuer_token_id")
            and visibility == "querier_only"
        ):
            return False
        return True

    def _redact_run_payload_for_caller(caller: Caller, run: dict) -> dict:
        if _caller_can_access_run_payload(caller, run):
            return run
        redacted = dict(run)
        redacted["output"] = None
        redacted["index_output"] = None
        redacted["error"] = None
        redacted["artifacts"] = []
        redacted["payload_redacted"] = True
        return redacted

    async def _load_room_for_caller(
        caller: Caller,
        room_id: str | None,
    ) -> dict:
        rid = (room_id or "").strip()
        if caller.role == "query":
            bound = (caller.constraints.get("room_id") or "").strip()
            if not bound:
                raise HTTPException(400, "query token is not bound to a room")
            if rid and rid != bound:
                raise HTTPException(403, "query token is bound to a different room")
            rid = bound
        if not rid:
            raise HTTPException(400, "room_id is required")
        room = await asyncio.to_thread(caller.hive.room_store.get, rid)
        if not room:
            raise HTTPException(404, f"room '{rid}' not found")
        ok, reason = verify_room_envelope(room.get("envelope") or {})
        if not ok:
            raise HTTPException(
                409,
                f"room '{rid}' has an invalid signed manifest: {reason}",
            )
        if room.get("revoked_at") is not None:
            raise HTTPException(403, f"room '{rid}' is revoked")
        return room

    def _validate_room_provider(req_provider: str | None, room: dict) -> None:
        allowed = [p.strip().lower() for p in room.get("allowed_llm_providers") or []]
        requested = (req_provider or "").strip().lower()
        if not allowed:
            if requested:
                raise HTTPException(
                    400,
                    "this room disallows external LLM egress; omit provider",
                )
            return
        selected = requested or allowed[0]
        if selected not in allowed:
            raise HTTPException(
                400,
                f"provider '{selected}' is not allowed by this room "
                f"(allowed_llm_providers={allowed})",
            )

    def _room_query_inspection_mode(room: dict) -> str:
        manifest = room.get("manifest") or {}
        query = manifest.get("query") or {}
        return inspection_mode_from_visibility(query.get("visibility"))

    def _room_prompt_for_run(room: dict | None, prompt: str) -> str | None:
        if not room:
            return None
        manifest = room.get("manifest") or {}
        query = manifest.get("query") or {}
        if query.get("visibility") != "inspectable":
            return None
        return prompt

    def _room_wrap_id(caller: Caller) -> str:
        if caller.role == "owner":
            return "owner"
        token_id = (caller.token_id or "").strip()
        if not token_id:
            raise HTTPException(500, "query caller is missing token_id")
        return f"query:{token_id}"

    def _room_link(request: Request, room_id: str, token: str, pubkey_b64: str) -> str:
        base = str(request.base_url).rstrip("/")
        host = request.url.netloc or "service"
        return (
            f"hmroom://{host}/{room_id}"
            f"?service={quote(base, safe='')}"
            f"&token={quote(token, safe='')}"
            f"&owner_pubkey={quote(pubkey_b64, safe='')}"
        )

    def _apply_room_to_query_request(
        req: QueryRequest,
        room: dict,
    ) -> QueryRequest:
        manifest = room.get("manifest") or {}
        query = manifest.get("query") or {}
        mediator = manifest.get("mediator")
        mode = query.get("mode") or room.get("query_mode")
        fixed_query_agent_id = (
            query.get("agent_id")
            or room.get("fixed_query_agent_id")
            or ""
        ).strip()
        if mode == "fixed":
            if not fixed_query_agent_id:
                raise HTTPException(500, "room fixed query agent is missing")
            query_agent_id = fixed_query_agent_id
        else:
            query_agent_id = (req.query_agent_id or "").strip()
            if not query_agent_id:
                raise HTTPException(
                    400,
                    "room requires a query_agent_id; upload a query agent "
                    "or use a room with query.mode='fixed'",
                )
        room_policy = room.get("policy") or ""
        requested_policy = (req.policy or "").strip()
        if requested_policy and requested_policy != room_policy:
            raise HTTPException(
                400,
                "room policy is fixed by the signed room manifest; "
                "caller-supplied policy cannot override it",
            )
        mediator_agent_id = req.mediator_agent_id
        if isinstance(mediator, dict):
            fixed_mediator_agent_id = (mediator.get("agent_id") or "").strip()
            requested_mediator_agent_id = (req.mediator_agent_id or "").strip()
            if fixed_mediator_agent_id:
                if (
                    requested_mediator_agent_id
                    and requested_mediator_agent_id != fixed_mediator_agent_id
                ):
                    raise HTTPException(
                        400,
                        "room mediator agent is fixed by the signed room "
                        "manifest; caller-supplied mediator cannot override it",
                    )
                mediator_agent_id = fixed_mediator_agent_id
            elif requested_mediator_agent_id:
                raise HTTPException(
                    400,
                    "room manifest does not allow a mediator-agent override",
                )
        _validate_room_provider(req.provider, room)
        return req.model_copy(
            update={
                "room_id": room["room_id"],
                "scope_agent_id": room["scope_agent_id"],
                "query_agent_id": query_agent_id,
                "mediator_agent_id": mediator_agent_id,
                "policy": room_policy,
            }
        )

    def _live_compose_hash() -> str:
        from . import attestation as _att

        bundle = _att.get_bundle()
        if not bundle.get("ready"):
            return ""
        return (
            (bundle.get("attestation") or {}).get("compose_hash") or ""
        ).lower()

    def _compose_trust_from_update(
        current: dict,
        req: RoomTrustUpdateRequest,
    ) -> dict:
        mode = req.mode or current.get("mode") or "operator_updates"
        if req.allowed_composes is None:
            allowed = [
                str(c).strip().lower()
                for c in (current.get("allowed_composes") or [])
                if str(c).strip()
            ]
        else:
            allowed = [
                str(c).strip().lower()
                for c in req.allowed_composes
                if str(c).strip()
            ]
        if req.append_live:
            live = _live_compose_hash()
            if not live:
                raise HTTPException(400, "live compose_hash is not available")
            if live not in allowed:
                allowed.append(live)
        if mode in {"pinned", "owner_approved"} and not allowed:
            raise HTTPException(
                400,
                f"trust.mode='{mode}' requires allowed_composes or append_live=true",
            )
        return {"mode": mode, "allowed_composes": allowed}

    async def check_admin(request: Request):
        """Gate admin endpoints with the separate HIVEMIND_ADMIN_KEY."""
        if not settings.admin_key:
            raise HTTPException(
                status_code=503,
                detail="Admin API disabled (HIVEMIND_ADMIN_KEY unset)",
            )
        token = _bearer(request)
        if not secrets.compare_digest(token, settings.admin_key):
            raise HTTPException(status_code=401, detail="Unauthorized")

    # ── Admin: tenant CRUD (gated by HIVEMIND_ADMIN_KEY) ──
    #
    # These let the operator mint per-tenant API keys. The admin NEVER
    # sees tenant data: isolation is enforced by a separate Postgres DB
    # per tenant and by SHA-256 key hashing in the control DB. Plaintext
    # keys are returned ONCE at provisioning and never persisted.

    @app.post("/v1/admin/tenants", dependencies=[Depends(check_admin)])
    async def admin_create_tenant(payload: dict, request: Request):
        name = (payload or {}).get("name", "")
        allow_duplicate_name = bool(
            (payload or {}).get("allow_duplicate_name", False)
        )
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "'name' required")
        registry = _registry(request)
        try:
            result = await asyncio.to_thread(
                registry.provision,
                name,
                allow_duplicate_name=allow_duplicate_name,
            )
        except DuplicateTenantNameError as e:
            raise HTTPException(409, str(e))
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return result

    @app.post(
        "/v1/admin/tenants/register",
        dependencies=[Depends(check_admin)],
    )
    async def admin_register_existing(payload: dict, request: Request):
        """Adopt a pre-populated Postgres database as a tenant.

        Body: {"name": "...", "db_name": "...", "api_key": "<optional>",
               "tenant_id": "<optional t_hex>"}.
        Does NOT create or modify the database — stamps the control-plane
        row only. Pass `tenant_id` when you've already renamed the DB to
        `tenant_<tenant_id>` and want the control row to match.
        """
        name = (payload or {}).get("name", "")
        db_name = (payload or {}).get("db_name", "")
        api_key = (payload or {}).get("api_key") or None
        tenant_id = (payload or {}).get("tenant_id") or None
        allow_duplicate_name = bool(
            (payload or {}).get("allow_duplicate_name", False)
        )
        registry = _registry(request)
        try:
            result = await asyncio.to_thread(
                registry.register_existing,
                name,
                db_name,
                api_key,
                tenant_id,
                allow_duplicate_name=allow_duplicate_name,
            )
        except DuplicateTenantNameError as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return result

    @app.post(
        "/v1/admin/rename-database",
        dependencies=[Depends(check_admin)],
    )
    async def admin_rename_database(payload: dict, request: Request):
        """Rename a Postgres database on the backing cluster.

        Body: ``{"old_name": "...", "new_name": "..."}``. Intended for
        one-shot migrations (e.g., renaming an old ``hivemind`` DB to
        ``tenant_t_<hex>`` before adopting it with
        ``/v1/admin/tenants/register``). Does NOT update control-plane
        rows — call ``/v1/admin/tenants/register`` after renaming.
        """
        old_name = (payload or {}).get("old_name", "")
        new_name = (payload or {}).get("new_name", "")
        if not old_name or not new_name:
            raise HTTPException(400, "'old_name' and 'new_name' required")
        registry = _registry(request)
        admin = registry._pg_admin
        if admin is None:
            raise HTTPException(
                503,
                "rename requires HIVEMIND_SQL_PROXY_ADMIN_KEY (HTTP deploys)",
            )
        try:
            await asyncio.to_thread(admin.rename_database, old_name, new_name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except RuntimeError as e:
            # sql_proxy returned an error — surface it verbatim.
            raise HTTPException(400, str(e))
        return {"status": "ok", "old_name": old_name, "new_name": new_name}

    @app.get("/v1/admin/tenants", dependencies=[Depends(check_admin)])
    async def admin_list_tenants(request: Request):
        registry = _registry(request)
        tenants = await asyncio.to_thread(registry.list_tenants)
        return {"tenants": tenants}

    @app.get("/v1/admin/billing/{tenant_id}", dependencies=[Depends(check_admin)])
    async def admin_billing_account(
        tenant_id: str,
        request: Request,
        limit: int = 25,
    ):
        registry = _registry(request)
        try:
            return await asyncio.to_thread(
                registry.billing_account,
                tenant_id,
                limit=limit,
            )
        except KeyError as e:
            raise HTTPException(404, str(e))

    @app.post(
        "/v1/admin/billing/{tenant_id}/credits",
        dependencies=[Depends(check_admin)],
    )
    async def admin_billing_grant_credit(
        tenant_id: str,
        payload: dict,
        request: Request,
    ):
        amount = (payload or {}).get("amount_usd")
        note = str((payload or {}).get("note") or "")
        if amount is None:
            raise HTTPException(400, "'amount_usd' required")
        registry = _registry(request)
        try:
            return await asyncio.to_thread(
                registry.billing_grant_credit,
                tenant_id,
                amount,
                note=note,
                actor="admin",
            )
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/v1/admin/billing/prices", dependencies=[Depends(check_admin)])
    async def admin_billing_prices(request: Request):
        registry = _registry(request)
        prices = await asyncio.to_thread(registry.billing_list_prices)
        return {"prices": prices}

    @app.post("/v1/admin/billing/prices", dependencies=[Depends(check_admin)])
    async def admin_billing_set_price(payload: dict, request: Request):
        registry = _registry(request)
        try:
            return await asyncio.to_thread(
                registry.billing_set_price,
                str((payload or {}).get("provider") or ""),
                str((payload or {}).get("model") or ""),
                prompt_usd_per_million=(payload or {}).get(
                    "prompt_usd_per_million"
                ),
                completion_usd_per_million=(payload or {}).get(
                    "completion_usd_per_million"
                ),
                source=str((payload or {}).get("source") or "admin"),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post(
        "/v1/admin/migrate-to-roles",
        dependencies=[Depends(check_admin)],
    )
    async def admin_migrate_to_roles(request: Request):
        """Retrofit per-tenant Postgres roles onto pre-existing tenant DBs.

        Idempotent. Required once after upgrading to the Layer-1 build of
        the SQL proxy; tenants provisioned after the upgrade already have
        roles. Returns one result dict per tenant DB encountered.
        """
        registry = _registry(request)
        admin = registry._pg_admin
        if admin is None:
            raise HTTPException(
                503,
                "migrate-to-roles requires HIVEMIND_SQL_PROXY_ADMIN_KEY "
                "(HTTP deploys)",
            )
        try:
            results = await asyncio.to_thread(admin.migrate_tenants_to_roles)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        return {"status": "ok", "results": results}

    @app.delete(
        "/v1/admin/tenants/{tenant_id}",
        dependencies=[Depends(check_admin)],
    )
    async def admin_delete_tenant(tenant_id: str, request: Request):
        registry = _registry(request)
        try:
            await asyncio.to_thread(registry.delete, tenant_id)
        except KeyError:
            raise HTTPException(404, f"tenant '{tenant_id}' not found")
        return {"status": "ok", "tenant_id": tenant_id}

    @app.post(
        "/v1/admin/agents/sweep-broken",
        dependencies=[Depends(check_admin)],
    )
    async def admin_sweep_broken_agents(request: Request, dry_run: bool = False):
        """Find agents whose images are missing from the runtime.

        Iterates every non-suspended tenant, lists their agents, and
        checks each agent's image against the local Docker daemon. If
        the image isn't present, the agent is registered orphan and
        (unless dry_run=true) deleted from the tenant's agent_store.

        Why we need this: after a CVM redeploy the daemon's image cache
        is wiped. Agents whose images were either ``:local`` (built
        in-place on the previous CVM) or pulled from a private registry
        (denied on the new CVM) become unrunnable but still appear in
        listings. Room runs fail against them until they are rebuilt.
        """
        from .sandbox.backend import _create_runner

        sandbox_settings = build_sandbox_settings(settings)
        runner = _create_runner(sandbox_settings)
        registry = _registry(request)

        orphans: list[dict] = []
        for tenant in await asyncio.to_thread(registry.list_tenants):
            if tenant.get("suspended"):
                continue
            tenant_id = tenant["id"]
            try:
                hm = await asyncio.to_thread(registry.for_tenant, tenant_id)
                if hm is None:
                    continue
                agents = await asyncio.to_thread(hm.agent_store.list_agents)
                for ag in agents:
                    try:
                        present = await asyncio.to_thread(
                            runner.image_exists, ag.image
                        )
                    except Exception as e:
                        logger.warning(
                            "image_exists(%s) raised: %s — skipping",
                            ag.image, e,
                        )
                        continue
                    if present:
                        continue
                    record = {
                        "tenant_id": tenant_id,
                        "tenant_name": tenant.get("name"),
                        "agent_id": ag.agent_id,
                        "name": ag.name,
                        "agent_type": ag.agent_type,
                        "image": ag.image,
                        "deleted": False,
                    }
                    if not dry_run:
                        try:
                            await asyncio.to_thread(
                                hm.agent_store.delete, ag.agent_id
                            )
                            record["deleted"] = True
                        except Exception as e:
                            logger.warning(
                                "delete(%s) raised: %s",
                                ag.agent_id, e,
                            )
                            record["error"] = str(e)
                    orphans.append(record)
            except Exception as e:
                logger.warning(
                    "sweep-broken: tenant %s skipped: %s", tenant_id, e
                )

        return {
            "dry_run": dry_run,
            "count": len(orphans),
            "deleted": sum(1 for o in orphans if o.get("deleted")),
            "orphans": orphans,
        }

    # ── Tenant: self-service key rotation ──
    #
    # Intended as a mandatory first step: the admin who creates a tenant
    # temporarily sees the plaintext key (API response). The tenant rotates
    # it immediately on first use to cut the admin out of the trust loop.

    @app.post("/v1/tenant/rotate-key")
    async def tenant_rotate_key(
        request: Request, hm: Hivemind = Depends(get_tenant_hive),
    ):
        tenant_id = request.state.tenant_id
        registry = _registry(request)
        try:
            result = await asyncio.to_thread(registry.rotate_key, tenant_id)
        except KeyError:
            raise HTTPException(404, f"tenant '{tenant_id}' not found")
        return result

    # ── Internal invite tokens ──
    #
    # Room creation mints invite tokens directly through TenantRegistry.
    # These endpoints are intentionally hidden from the public API: the
    # product primitive is a signed room, not a naked capability token.

    @app.post("/v1/_internal/tokens", include_in_schema=False)
    async def issue_capability(
        payload: dict,
        request: Request,
        hm: Hivemind = Depends(get_tenant_hive),
    ):
        """Issue a capability token. Body: {kind, label, constraints}.

        Only ``kind="query"`` is supported. ``constraints.scope_agent_id``
        is required and pins every query through the token to that
        scope agent.
        """
        kind = (payload or {}).get("kind", "query") or "query"
        label = (payload or {}).get("label", "") or ""
        constraints = (payload or {}).get("constraints") or {}
        registry = _registry(request)
        tenant_id = request.state.tenant_id
        try:
            result = await asyncio.to_thread(
                registry.mint_capability, tenant_id, kind, label, constraints
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        except KeyError as e:
            raise HTTPException(404, str(e))
        return result

    @app.get("/v1/_internal/tokens", include_in_schema=False)
    async def list_tokens(
        request: Request, hm: Hivemind = Depends(get_tenant_hive),
    ):
        registry = _registry(request)
        tenant_id = request.state.tenant_id
        rows = await asyncio.to_thread(registry.list_capabilities, tenant_id)
        return {"tokens": rows}

    @app.delete("/v1/_internal/tokens/{token_id}", include_in_schema=False)
    async def revoke_token(
        token_id: str,
        request: Request,
        hm: Hivemind = Depends(get_tenant_hive),
    ):
        registry = _registry(request)
        tenant_id = request.state.tenant_id
        try:
            ok = await asyncio.to_thread(
                registry.revoke_capability, tenant_id, token_id
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not ok:
            raise HTTPException(404, f"token '{token_id}' not found")
        return {"status": "ok", "token_id": token_id}

    # ── Compose pins (owner-signed redeploy authorization) ──
    #
    # POST stores a tenant-signed envelope authorizing one or more
    # ``compose_hash`` values for a scope agent. The server verifies the
    # signature against the pubkey it derives from the inbound ``hmk_``
    # (so a stranger can't post pins for someone else's tenant) and
    # stashes the envelope. GET returns pins to anyone authenticated
    # against the tenant — recipients (``hmq_``) need it to verify URIs
    # across redeploys without holding the owner key.

    @app.post("/v1/tenants/compose-pin")
    async def submit_compose_pin(
        request: Request,
        payload: dict,
        caller: Caller = Depends(requires_role("owner")),
    ):
        from cryptography.hazmat.primitives import serialization

        from .compose_pin import ComposePin
        from .tenant_signing import derive_signing_keypair

        envelope_dict = (payload or {}).get("envelope")
        if not isinstance(envelope_dict, dict):
            raise HTTPException(
                400, "body must be {\"envelope\": {<ComposePin fields>}}"
            )
        try:
            pin = ComposePin.model_validate(envelope_dict)
        except ValidationError as e:
            raise HTTPException(400, f"envelope: {e}")
        if pin.tenant_id != caller.tenant_id:
            raise HTTPException(
                400,
                f"envelope.tenant_id ({pin.tenant_id}) does not match "
                f"caller tenant ({caller.tenant_id})",
            )
        bearer = _bearer(request)
        _priv, expected_pub = derive_signing_keypair(bearer, caller.tenant_id)
        expected_pub_bytes = expected_pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        if not pin.verify(expected_pubkey=expected_pub_bytes):
            raise HTTPException(
                400,
                "envelope signature invalid (signer_pubkey must match the "
                "pubkey derived from your hmk_ + tenant_id)",
            )
        if pin.is_expired():
            raise HTTPException(400, "envelope already expired")
        envelope_json = pin.to_json()
        registry = _registry(request)
        try:
            result = await asyncio.to_thread(
                registry.store_compose_pin,
                caller.tenant_id,
                envelope_json,
                pin.signer_pubkey,
            )
        except KeyError as e:
            raise HTTPException(404, str(e))
        return {**result, "envelope": pin.model_dump()}

    @app.get("/v1/tenants/compose-pin")
    async def latest_compose_pin(
        request: Request,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        registry = _registry(request)
        row = await asyncio.to_thread(
            registry.latest_compose_pin, caller.tenant_id
        )
        if not row:
            raise HTTPException(404, "no compose pin")
        from .compose_pin import ComposePin

        pin = ComposePin.from_json(row["envelope"])
        return {
            "pin_id": row["pin_id"],
            "tenant_id": caller.tenant_id,
            "envelope": pin.model_dump(),
            "created_at": row["created_at"],
            "revoked_at": row["revoked_at"],
        }

    @app.get("/v1/tenants/compose-pin/list")
    async def list_compose_pins(
        request: Request,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        registry = _registry(request)
        rows = await asyncio.to_thread(
            registry.list_compose_pins, caller.tenant_id
        )
        from .compose_pin import ComposePin

        out: list[dict] = []
        for r in rows:
            try:
                env = ComposePin.from_json(r["envelope"]).model_dump()
            except Exception:
                env = None
            out.append(
                {
                    "pin_id": r["pin_id"],
                    "envelope": env,
                    "created_at": r["created_at"],
                    "revoked_at": r["revoked_at"],
                }
            )
        return {"pins": out}

    @app.delete("/v1/tenants/compose-pin/{pin_id}")
    async def revoke_compose_pin(
        pin_id: str,
        request: Request,
        caller: Caller = Depends(requires_role("owner")),
    ):
        registry = _registry(request)
        ok = await asyncio.to_thread(
            registry.revoke_compose_pin, caller.tenant_id, pin_id
        )
        if not ok:
            raise HTTPException(404, f"pin '{pin_id}' not found")
        return {"status": "ok", "pin_id": pin_id}

    # ── Identity reflection (any authenticated caller) ──
    #
    # Lets the CLI resolve its own ``tenant_id`` and the active invite
    # constraints without decoding the bearer itself.
    @app.get("/v1/whoami")
    async def whoami(
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        return {
            "tenant_id": caller.tenant_id,
            "role": caller.role,
            "constraints": caller.constraints,
            "token_id": caller.token_id,
            "sealed": caller.sealed,
        }

    # ── Rooms ──

    @app.post("/v1/rooms")
    async def create_room(
        req: RoomCreateRequest,
        request: Request,
        caller: Caller = Depends(requires_role("owner")),
    ):
        """Create a signed room manifest and a recipient invite token."""
        hm = caller.hive
        scope_cfg = await asyncio.to_thread(
            hm.agent_store.get, req.scope_agent_id,
        )
        if not scope_cfg:
            raise HTTPException(404, f"Scope agent '{req.scope_agent_id}' not found")
        actual_scope_visibility = visibility_from_inspection_mode(
            getattr(scope_cfg, "inspection_mode", "full")
        )
        if req.scope_visibility and req.scope_visibility != actual_scope_visibility:
            raise HTTPException(
                400,
                "scope_visibility does not match the registered scope "
                f"agent inspection_mode ({actual_scope_visibility})",
            )
        scope_visibility = actual_scope_visibility

        query_visibility = req.query_visibility
        if req.query_mode == "fixed":
            query_cfg = await asyncio.to_thread(
                hm.agent_store.get, req.query_agent_id,
            )
            if not query_cfg:
                raise HTTPException(
                    404, f"Query agent '{req.query_agent_id}' not found"
                )
            query_visibility = visibility_from_inspection_mode(
                getattr(query_cfg, "inspection_mode", "full")
            )

        if not req.mediator_agent_id:
            req.mediator_agent_id = settings.default_mediator_agent or None
        mediator_visibility = None
        if req.mediator_agent_id:
            mediator_cfg = await asyncio.to_thread(
                hm.agent_store.get, req.mediator_agent_id,
            )
            if not mediator_cfg:
                raise HTTPException(
                    404, f"Mediator agent '{req.mediator_agent_id}' not found"
                )
            mediator_visibility = visibility_from_inspection_mode(
                getattr(mediator_cfg, "inspection_mode", "full")
            )
            if (
                req.mediator_visibility
                and req.mediator_visibility != mediator_visibility
            ):
                raise HTTPException(
                    400,
                    "mediator_visibility does not match the registered mediator "
                    f"agent inspection_mode ({mediator_visibility})",
                )

        if (
            req.trust.mode in {"pinned", "owner_approved"}
            and not req.trust.allowed_composes
        ):
            compose_hash = _live_compose_hash()
            if not compose_hash:
                raise HTTPException(
                    400,
                    "strict room trust mode requires a live compose_hash; "
                    "pass trust.allowed_composes explicitly or use "
                    "trust.mode='operator_updates'",
                )
            req.trust.allowed_composes = [compose_hash]

        from cryptography.hazmat.primitives import serialization
        from .tenant_signing import derive_signing_keypair

        bearer = _bearer(request)
        priv, pub = derive_signing_keypair(bearer, caller.tenant_id)
        pub_b64 = base64.b64encode(
            pub.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        ).decode("ascii")

        room_id = f"room_{uuid4().hex[:12]}"
        manifest = build_room_manifest(
            room_id=room_id,
            tenant_id=caller.tenant_id,
            created_at=time.time(),
            req=req,
            scope_visibility=scope_visibility,
            query_visibility=query_visibility,
            mediator_visibility=mediator_visibility,
            signer_pubkey_b64=pub_b64,
        )
        envelope = sign_manifest(manifest, priv)
        room = await asyncio.to_thread(hm.room_store.create, envelope)

        registry = _registry(request)
        try:
            cap = await asyncio.to_thread(
                registry.mint_capability,
                caller.tenant_id,
                "query",
                f"room:{req.name or room_id}",
                room_constraints(envelope),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))

        # Initialize a room key and wrap it to both participants.
        # The room DEK is participant-presented, not KMS-derived: after a
        # restart it can only be re-opened by the owner hmk_ or this hmq_.
        dek = await asyncio.to_thread(
            hm.room_vault.ensure_room_key,
            room_id,
            "owner",
            bearer,
        )
        await asyncio.to_thread(
            hm.room_vault.add_wrap,
            room_id,
            f"query:{cap['token_id']}",
            cap["token"],
            dek=dek,
        )

        return {
            "room_id": room_id,
            "room": room,
            "token": cap["token"],
            "token_id": cap["token_id"],
            "link": _room_link(request, room_id, cap["token"], pub_b64),
        }

    @app.get("/v1/rooms")
    async def list_rooms(
        limit: int = 50,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        if caller.role == "query":
            room = await _load_room_for_caller(
                caller, caller.constraints.get("room_id")
            )
            return {"rooms": [room]}
        rooms = await asyncio.to_thread(caller.hive.room_store.list, limit)
        return {"rooms": rooms}

    @app.get("/v1/rooms/{room_id}")
    async def get_room(
        room_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        room = await _load_room_for_caller(caller, room_id)
        return room

    @app.get("/v1/rooms/{room_id}/attest")
    async def attest_room(
        room_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        from . import attestation as _att

        room = await _load_room_for_caller(caller, room_id)
        scope = await _build_agent_attestation(caller, room["scope_agent_id"])
        fixed_query = None
        if room.get("fixed_query_agent_id"):
            fixed_query = await _build_agent_attestation(
                caller, room["fixed_query_agent_id"]
            )
        fixed_mediator = None
        if room.get("fixed_mediator_agent_id"):
            fixed_mediator = await _build_agent_attestation(
                caller, room["fixed_mediator_agent_id"]
            )
        return {
            "room": room,
            "scope_agent": scope,
            "query_agent": fixed_query,
            "mediator_agent": fixed_mediator,
            "attestation": _att.get_bundle(),
        }

    @app.get("/v1/rooms/{room_id}/key")
    async def room_vault_status(
        room_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        room = await _load_room_for_caller(caller, room_id)
        return await asyncio.to_thread(caller.hive.room_vault.status, room["room_id"])

    @app.post("/v1/rooms/{room_id}/open")
    async def open_room_vault(
        room_id: str,
        request: Request,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        room = await _load_room_for_caller(caller, room_id)
        await asyncio.to_thread(
            caller.hive.room_vault.open,
            room["room_id"],
            _room_wrap_id(caller),
            _bearer(request),
        )
        return await asyncio.to_thread(caller.hive.room_vault.status, room["room_id"])

    @app.post("/v1/rooms/{room_id}/data")
    async def add_room_vault_item(
        room_id: str,
        req: RoomVaultItemRequest,
        request: Request,
        caller: Caller = Depends(requires_role("owner")),
    ):
        room = await _load_room_for_caller(caller, room_id)
        bearer = _bearer(request)
        await asyncio.to_thread(
            caller.hive.room_vault.ensure_room_key,
            room["room_id"],
            "owner",
            bearer,
        )
        item = await asyncio.to_thread(
            caller.hive.room_vault.put_item,
            room["room_id"],
            text=req.text,
            metadata=req.metadata,
        )
        return item

    @app.get("/v1/rooms/{room_id}/data")
    async def list_room_vault_items(
        room_id: str,
        request: Request,
        caller: Caller = Depends(requires_role("owner")),
    ):
        room = await _load_room_for_caller(caller, room_id)
        items = await asyncio.to_thread(
            caller.hive.room_vault.list_items_for_bearer,
            room["room_id"],
            "owner",
            _bearer(request),
        )
        return {"items": items}

    @app.delete("/v1/rooms/{room_id}")
    async def revoke_room(
        room_id: str,
        caller: Caller = Depends(requires_role("owner")),
    ):
        ok = await asyncio.to_thread(caller.hive.room_store.revoke, room_id)
        if not ok:
            raise HTTPException(404, f"room '{room_id}' not found")
        return {"status": "ok", "room_id": room_id}

    @app.post("/v1/rooms/{room_id}/trust")
    async def update_room_trust(
        room_id: str,
        req: RoomTrustUpdateRequest,
        request: Request,
        caller: Caller = Depends(requires_role("owner")),
    ):
        """Re-sign a room manifest with updated compose trust settings.

        The room id and invite tokens do not change. Recipients using an
        old ``hmroom://`` link verify the new envelope against the same
        owner pubkey embedded in the link.
        """
        room = await _load_room_for_caller(caller, room_id)
        manifest = dict(room["manifest"])
        manifest["trust"] = _compose_trust_from_update(
            manifest.get("trust") or {},
            req,
        )
        manifest["updated_at"] = time.time()

        from .tenant_signing import derive_signing_keypair

        bearer = _bearer(request)
        priv, _pub = derive_signing_keypair(bearer, caller.tenant_id)
        envelope = sign_manifest(manifest, priv)
        updated = await asyncio.to_thread(caller.hive.room_store.update, envelope)
        if not updated:
            raise HTTPException(404, f"room '{room_id}' not found")
        return {"room_id": room_id, "room": updated}

    @app.post("/v1/rooms/{room_id}/runs")
    async def submit_room_run(
        room_id: str,
        req: RoomRunRequest,
        request: Request,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        room = await _load_room_for_caller(caller, room_id)
        qreq = QueryRequest(
            query=req.query,
            room_id=room["room_id"],
            query_agent_id=req.query_agent_id,
            mediator_agent_id=req.mediator_agent_id,
            max_tokens=req.max_tokens,
            max_llm_calls=req.max_llm_calls,
            timeout_seconds=req.timeout_seconds,
            model=req.model,
            provider=req.provider,
        )
        qreq = _apply_room_to_query_request(qreq, room)
        return await _submit_query_run_for_request(
            qreq,
            caller,
            room,
            request=request,
            bearer=_bearer(request),
        )

    # ── Liveness (unauthed, no tenant scope) ──

    @app.get("/v1/healthz")
    async def healthz():
        return {"status": "ok", "version": APP_VERSION}

    @app.get("/v1/admin/llm-probe", dependencies=[Depends(check_admin)])
    async def llm_probe(provider: str = "", model: str = ""):
        """Admin-only: probe LLM provider connectivity from inside the CVM.

        Query params:
          provider: 'openrouter' (default) or 'tinfoil'.
          model:    optional model id. If empty, uses the configured default.

        Returns timing + status for a minimal chat completion. Helps
        diagnose CVM↔provider network issues (auth, model id, network)
        without SSH'ing into the CVM.
        """
        import time as _t
        import httpx as _httpx

        prov_key = (provider or "").strip().lower() or "openrouter"
        if prov_key == "tinfoil":
            base_url = settings.tinfoil_base_url
            api_key = settings.tinfoil_api_key
        elif prov_key == "openrouter":
            base_url = settings.llm_base_url
            api_key = settings.llm_api_key
        else:
            return {"error": f"unknown provider {provider!r}, expected 'openrouter' or 'tinfoil'"}

        chosen_model = (model or "").strip() or settings.llm_model
        out: dict = {
            "provider": prov_key,
            "base_url": base_url,
            "model": chosen_model,
            "api_key_configured": bool(api_key),
            "timeout_seconds": settings.llm_timeout_seconds,
        }
        if not api_key:
            out["error"] = f"{prov_key} api_key not configured on server"
            return out

        t0 = _t.perf_counter()
        try:
            async with _httpx.AsyncClient(
                timeout=_httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
            ) as client:
                r = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": chosen_model,
                        "messages": [{"role": "user", "content": "reply OK"}],
                        "max_tokens": 5,
                    },
                )
            out["status_code"] = r.status_code
            out["elapsed_ms"] = int((_t.perf_counter() - t0) * 1000)
            out["body_head"] = r.text[:200]
        except Exception as e:
            out["error_class"] = type(e).__name__
            out["error"] = str(e)[:300]
            out["elapsed_ms"] = int((_t.perf_counter() - t0) * 1000)
        return out

    # ── Remote attestation (unauthed; pattern from feedling-mcp-v1) ──
    # The bundle is public — anyone holding Intel's root CA can verify
    # the quote. Gating it would create a chicken-and-egg: the CLI needs
    # to know it trusts this CVM before it can authenticate.

    @app.get("/v1/attestation")
    async def attestation_endpoint():
        from . import attestation as _att
        return _att.get_bundle()

    # ── Internal pipeline primitives ──
    #
    # Room endpoints below are the public execution surface. These lower-level
    # primitives are kept for tests/admin maintenance and are hidden from the
    # generated API schema so new clients do not learn the old generic path.

    @app.post(
        "/v1/_internal/store",
        response_model=StoreResponse,
        include_in_schema=False,
    )
    async def store(
        req: StoreRequest,
        caller: Caller = Depends(requires_role("owner")),
    ):
        try:
            return await caller.hive.pipeline.run_store(req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    def _force_scope_for_query_token(
        req: QueryRequest, caller: Caller
    ) -> QueryRequest:
        """If caller is a query-token holder, pin scope_agent_id to the
        token's bound scope agent. Owner-supplied scope_agent_id is left
        alone."""
        if caller.role != "query":
            return req
        bound = caller.constraints.get("scope_agent_id") or ""
        if not bound:
            raise HTTPException(
                status_code=500,
                detail="query token missing scope_agent_id constraint",
            )
        # Always overwrite — query tokens cannot bypass their bound scope.
        return req.model_copy(update={"scope_agent_id": bound})

    # ── Query submit (tracked async) ──
    #
    # The only query-execution endpoint. Synchronous ``POST /v1/query``
    # was removed because (a) it doesn't survive the Phala gateway's
    # 60s read timeout and (b) it never produced a Phase 5 signed
    # envelope, so strict-default attestation silently degraded for
    # every URI-based recipient call. Backed by the run_store table —
    # completed rows carry an Ed25519 signature over the run body.
    # Recipients poll status via ``GET /v1/runs/{run_id}``.

    async def _submit_query_run_for_request(
        req: QueryRequest,
        caller: Caller,
        room: dict | None = None,
        request: Request | None = None,
        bearer: str | None = None,
    ) -> dict:
        hm = caller.hive
        query_agent_id = req.query_agent_id or hm.settings.default_query_agent
        scope_agent_id = _require_scope_agent_id(hm, req.scope_agent_id)
        if not query_agent_id:
            raise HTTPException(
                400, "query_agent_id is required (no default configured)"
            )
        await _ensure_scope_agent_exists(hm, scope_agent_id)
        room_vault_items: list[dict] = []
        if room is not None:
            room_vault_items = await asyncio.to_thread(
                hm.room_vault.list_items_for_bearer,
                room["room_id"],
                _room_wrap_id(caller),
                bearer or "",
            )

        run_id = uuid4().hex[:12]
        effective_max_tokens = min(
            req.max_tokens or hm.settings.max_tokens,
            hm.settings.max_tokens,
        )
        billing = {
            "payer_tenant_id": None,
            "payer_token_id": caller.token_id or "",
            "billable_role": "query",
            "billing_provider": _billing_provider_for_room(req.provider, room),
            "billing_model": ",".join(_billing_models_for_query(hm, req)),
            "billing_hold_micro_usd": 0,
            "billing_status": "unbilled",
        }
        if request is not None:
            billing = await _prepare_billing_hold(
                request,
                caller,
                hm,
                run_id=run_id,
                provider=_billing_provider_for_room(req.provider, room),
                models=_billing_models_for_query(hm, req),
                max_tokens=effective_max_tokens,
                billable_role="query",
            )
        await asyncio.to_thread(
            hm.run_store.create, run_id, query_agent_id,
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
            prompt=_room_prompt_for_run(room, req.query),
            output_visibility=(room or {}).get(
                "output_visibility", "owner_and_querier"
            ),
            artifacts_enabled=bool((room or {}).get("allow_artifacts", True)),
        )

        _spawn_bg(
            app,
            hm.pipeline.run_query_agent_tracked(
                agent_id=query_agent_id,
                run_id=run_id,
                run_store=hm.run_store,
                artifact_store=hm.artifact_store,
                artifact_retention_seconds=hm.settings.artifact_retention_seconds,
                prompt=req.query,
                scope_agent_id=scope_agent_id,
                mediator_agent_id=req.mediator_agent_id,
                max_tokens=req.max_tokens,
                max_calls=req.max_llm_calls,
                timeout_seconds=req.timeout_seconds,
                model=req.model,
                provider=req.provider,
                policy=req.policy,
                room_id=(room or {}).get("room_id"),
                room_manifest_hash=(room or {}).get("manifest_hash"),
                output_visibility=(room or {}).get(
                    "output_visibility", "owner_and_querier"
                ),
                allowed_llm_providers=(room or {}).get("allowed_llm_providers"),
                artifacts_enabled=bool((room or {}).get("allow_artifacts", True)),
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
            "query_agent_id": query_agent_id,
            "scope_agent_id": scope_agent_id,
            "room_id": (room or {}).get("room_id"),
            "status": "pending",
        }

    @app.post("/v1/_internal/query/run/submit", include_in_schema=False)
    async def submit_query_run(
        req: QueryRequest,
        request: Request,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        """Submit a query for tracked async processing.

        Returns a ``run_id``; the run executes via
        ``run_query_agent_tracked`` so the completed row carries a
        signed attestation envelope.
        """
        room: dict | None = None
        room_id = (req.room_id or "").strip()
        if caller.role == "query" and caller.constraints.get("room_id"):
            room = await _load_room_for_caller(caller, room_id)
            req = _apply_room_to_query_request(req, room)
        elif room_id:
            room = await _load_room_for_caller(caller, room_id)
            req = _apply_room_to_query_request(req, room)
        else:
            req = _force_scope_for_query_token(req, caller)
        return await _submit_query_run_for_request(
            req,
            caller,
            room,
            request=request,
            bearer=_bearer(request),
        )

    @app.post(
        "/v1/_internal/index",
        response_model=IndexResponse,
        include_in_schema=False,
    )
    async def index(req: IndexRequest, hm: Hivemind = Depends(get_tenant_hive)):
        try:
            return await hm.pipeline.run_index(req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ── Admin schema endpoint ──

    @app.get(
        "/v1/admin/schema",
    )
    async def get_schema(
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        schema = await asyncio.to_thread(caller.hive.db.get_schema)
        return {"schema": schema}

    # ── Room agent registry ──

    from .sandbox.models import AgentConfig, AgentCreateRequest

    @app.post("/v1/_internal/agents/register-image", include_in_schema=False)
    async def register_agent(
        req: AgentCreateRequest,
        hm: Hivemind = Depends(get_tenant_hive),
    ):
        from .sandbox.backend import _create_runner

        sandbox_settings = build_sandbox_settings(settings)
        runner = _create_runner(sandbox_settings)
        try:
            if not runner.image_exists(req.image):
                raise HTTPException(
                    status_code=400,
                    detail=f"Image not found: {req.image}",
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Image preflight failed for %s: %s", req.image, e)
            raise HTTPException(
                status_code=503,
                detail="Container backend unavailable for image validation",
            )

        agent_id = uuid4().hex[:12]
        config = AgentConfig(
            agent_id=agent_id,
            name=req.name,
            description=req.description,
            agent_type=req.agent_type,
            image=req.image,
            entrypoint=req.entrypoint,
            memory_mb=min(req.memory_mb, settings.container_memory_mb),
            max_llm_calls=req.max_llm_calls,
            max_tokens=req.max_tokens,
            timeout_seconds=req.timeout_seconds,
        )
        await asyncio.to_thread(hm.agent_store.create, config)

        # Extract source files from image (non-fatal, no-op for Phala)
        file_count = 0
        try:
            files = await runner.extract_image_files_async(config.image)
            await asyncio.to_thread(hm.agent_store.save_files, agent_id, files)
            file_count = len(files)
        except Exception as e:
            logger.warning("Failed to extract files from %s: %s", config.image, e)

        return {
            "agent_id": agent_id,
            "name": req.name,
            "files_extracted": file_count,
        }

    def _query_token_visible_agent(caller: Caller, agent_id: str) -> bool:
        """Query-token holders can only see room-advertised agents."""
        if caller.role != "query":
            return True
        visible = {caller.constraints.get("scope_agent_id") or ""}
        fixed = caller.constraints.get("fixed_query_agent_id") or ""
        if fixed:
            visible.add(fixed)
        mediator = caller.constraints.get("fixed_mediator_agent_id") or ""
        if mediator:
            visible.add(mediator)
        return agent_id in visible

    @app.get("/v1/room-agents")
    async def list_agents(
        type: str | None = None,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        agents = await asyncio.to_thread(caller.hive.agent_store.list_agents, type)
        if caller.role == "query":
            visible = {caller.constraints.get("scope_agent_id") or ""}
            fixed = caller.constraints.get("fixed_query_agent_id") or ""
            if fixed:
                visible.add(fixed)
            mediator = caller.constraints.get("fixed_mediator_agent_id") or ""
            if mediator:
                visible.add(mediator)
            agents = [a for a in agents if a.agent_id in visible]
        return [a.model_dump() for a in agents]

    @app.get("/v1/room-agents/{agent_id}")
    async def get_agent(
        agent_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        if not _query_token_visible_agent(caller, agent_id):
            raise HTTPException(404, "Agent not found")
        agent = await asyncio.to_thread(caller.hive.agent_store.get, agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        return agent.model_dump()

    @app.delete("/v1/room-agents/{agent_id}")
    async def delete_agent(
        agent_id: str, hm: Hivemind = Depends(get_tenant_hive)
    ):
        if not await asyncio.to_thread(hm.agent_store.delete, agent_id):
            raise HTTPException(404, "Agent not found")
        return {"status": "ok"}

    # ── Agent file inspection ──
    #
    # Designed for query-token holders to audit the scope agent they're
    # bound to before submitting their own query agent. Returns the
    # extracted source files saved at agent-build time. Owner sees every
    # agent; query-token sees only the bound scope agent.

    @app.get("/v1/room-agents/{agent_id}/files")
    async def list_agent_files(
        agent_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        if not _query_token_visible_agent(caller, agent_id):
            raise HTTPException(404, "Agent not found")
        agent = await asyncio.to_thread(caller.hive.agent_store.get, agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        files = await asyncio.to_thread(
            caller.hive.agent_store.list_file_paths, agent_id
        )
        return {"agent_id": agent_id, "files": files}

    @app.get("/v1/room-agents/{agent_id}/files/{file_path:path}")
    async def read_agent_file(
        agent_id: str,
        file_path: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        from .sandbox.agents import AgentSealedReadError

        if not _query_token_visible_agent(caller, agent_id):
            raise HTTPException(404, "Agent not found")
        try:
            content = await asyncio.to_thread(
                caller.hive.agent_store.read_file, agent_id, file_path
            )
        except AgentSealedReadError:
            raise HTTPException(
                status_code=403,
                detail=(
                    "agent is sealed (inspection_mode=sealed); source "
                    "files are encrypted for runtime-only use and cannot "
                    "be read through this endpoint by anyone, including "
                    "the room owner. Image digest, attested files digest, "
                    "and file path list remain inspectable."
                ),
            )
        if content is None:
            raise HTTPException(404, "File not found")
        return Response(content=content, media_type="text/plain; charset=utf-8")

    # ── Agent attestation ──
    #
    # The per-agent attestation surface. One call returns:
    #   - the agent's saved config (image, role, budgets…)
    #   - a stable sha256 over the full set of saved source files
    #     (sha256("<path>\0<content>\0…" sorted) — re-derivable by hand
    #     after re-fetching files via /v1/room-agents/{id}/files{,/{path}})
    #   - the resolved Docker image digest (``Id`` + ``RepoDigests``)
    #   - the live /v1/attestation bundle (compose_hash, app_id, quote,
    #     TLS pubkey)
    #
    # Together these let an external verifier — not just the owner —
    # pin "this exact agent ran inside this exact CVM". compose_hash
    # chains to the on-chain ``NotarizedAppAuth`` contract for governance;
    # the source + image digests pin the workload.
    #
    # Owner can attest any agent they own. Query-token holders are
    # restricted to their bound scope agent (other ids → 404), since the
    # endpoint reveals image + file digests.

    async def _build_agent_attestation(caller: Caller, agent_id: str) -> dict:
        from . import attestation as _att

        agent = await asyncio.to_thread(
            caller.hive.agent_store.get, agent_id
        )
        if not agent:
            raise HTTPException(404, "Agent not found")
        # Two digests: ``files_digest_sha256`` (all files, what the image
        # was built from) and ``attested_files_digest_sha256`` (only files
        # marked attestable — what a recipient verifies against the
        # agent's published source). Private files (e.g. secret prompts,
        # .env) contribute to image_digest but not the attested digest.
        digests = await asyncio.to_thread(
            caller.hive.agent_store.compute_digests, agent_id
        )
        return {
            "agent_id": agent_id,
            "agent": agent.model_dump(),
            "inspection_mode": getattr(agent, "inspection_mode", "full"),
            "files_count": digests["files_count"],
            "files_digest_sha256": digests["files_digest"],
            "attested_files_count": digests["attested_files_count"],
            "attested_files_digest_sha256": digests["attested_files_digest"],
            "image_digest": _image_digest(agent.image),
            "attestation": _att.get_bundle(),
        }

    @app.get("/v1/room-agents/{agent_id}/attest")
    async def attest_agent(
        agent_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        if not _query_token_visible_agent(caller, agent_id):
            raise HTTPException(404, "Agent not found")
        return await _build_agent_attestation(caller, agent_id)

    # Internal scope-attest — thin wrapper around the agent-attest helper for
    # the query-token recipient flow. Resolves the agent_id from the
    # token binding (query) or the ?scope_agent_id= query param (owner)
    # and delegates to the canonical helper. Adds ``scope_agent_id`` at
    # the top of the response for clients that key off that field.
    @app.get("/v1/_internal/scope-attest", include_in_schema=False)
    async def scope_attest(
        scope_agent_id: str | None = None,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        if caller.role == "query":
            scope_id = caller.constraints.get("scope_agent_id") or ""
            if not scope_id:
                raise HTTPException(
                    500, "query token missing scope_agent_id constraint"
                )
        else:
            scope_id = (scope_agent_id or "").strip()
            if not scope_id:
                raise HTTPException(
                    400,
                    "owner must pass ?scope_agent_id=… (no token binding)",
                )
        if not _query_token_visible_agent(caller, scope_id):
            raise HTTPException(404, "Agent not found")
        body = await _build_agent_attestation(caller, scope_id)
        return {"scope_agent_id": scope_id, **body}

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
                from .sandbox.backend import _create_runner

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

    @app.post("/v1/_internal/agents/submit", include_in_schema=False)
    async def submit_agents(
        request: Request,
        # Query agent (required)
        query_archive: UploadFile = File(...),
        query_name: str = Form(...),
        query_description: str = Form(""),
        query_entrypoint: str | None = Form(None),
        # Scope agent (optional)
        scope_archive: UploadFile | None = File(None),
        scope_name: str = Form(""),
        scope_entrypoint: str | None = Form(None),
        # Index agent (optional)
        index_archive: UploadFile | None = File(None),
        index_name: str = Form(""),
        index_entrypoint: str | None = Form(None),
        # Shared params
        prompt: str = Form(""),
        memory_mb: Annotated[int, Form(ge=16)] = 256,
        max_llm_calls: Annotated[int, Form(ge=1)] = 20,
        max_tokens: Annotated[int, Form(ge=1)] = 100_000,
        timeout_seconds: Annotated[int, Form(ge=1)] = 120,
        # Mediator (use existing registered agent)
        mediator_agent_id: str | None = Form(None),
        # Index data (required when index_archive is provided)
        document_data: str | None = Form(None),
        document_metadata: str | None = Form(None),
        # LLM routing (optional). ``provider="tinfoil"`` requires the
        # server to have HIVEMIND_TINFOIL_API_KEY configured. Empty
        # falls back to the global default (openrouter).
        model: str | None = Form(None),
        provider: str | None = Form(None),
        policy: str | None = Form(None),
        # Inspection-mode policy applies only to the query agent. The
        # scope/index agents in this endpoint are A's own agents: their
        # source is owner-readable by design (default 'full').
        query_inspection_mode: str = Form("full"),
        caller: Caller = Depends(requires_role("owner")),
    ):
        """Upload query agent (required) + optional scope/index agents,
        build all, then run the full pipeline with tracking."""
        hm = caller.hive

        validated_query_mode = _validate_inspection_mode(query_inspection_mode)
        has_scope_upload = bool(scope_archive and scope_archive.filename)
        default_scope_id = (hm.settings.default_scope_agent or "").strip()
        if not has_scope_upload and not default_scope_id:
            raise HTTPException(
                400,
                "scope_archive or default scope agent is required",
            )
        if not has_scope_upload:
            await _ensure_scope_agent_exists(hm, default_scope_id)

        # Read archives
        try:
            query_bytes = await _read_upload_bytes_limited(
                query_archive, max_bytes=MAX_UPLOAD_SIZE,
            )
        except ValueError as e:
            raise HTTPException(400, f"query_archive: {e}")

        scope_bytes = None
        if scope_archive and scope_archive.filename:
            try:
                scope_bytes = await _read_upload_bytes_limited(
                    scope_archive, max_bytes=MAX_UPLOAD_SIZE,
                )
            except ValueError as e:
                raise HTTPException(400, f"scope_archive: {e}")

        index_bytes = None
        if index_archive and index_archive.filename:
            if not document_data:
                raise HTTPException(
                    400, "document_data is required when index_archive is provided"
                )
            try:
                index_bytes = await _read_upload_bytes_limited(
                    index_archive, max_bytes=MAX_UPLOAD_SIZE,
                )
            except ValueError as e:
                raise HTTPException(400, f"index_archive: {e}")

        # Extract archives to temp dirs
        tmpdirs: list[str] = []
        try:
            query_tmpdir = tempfile.mkdtemp(prefix="hm-query-")
            tmpdirs.append(query_tmpdir)
            _safe_extract_tar(query_bytes, query_tmpdir)

            scope_tmpdir = None
            if scope_bytes:
                scope_tmpdir = tempfile.mkdtemp(prefix="hm-scope-")
                tmpdirs.append(scope_tmpdir)
                _safe_extract_tar(scope_bytes, scope_tmpdir)

            index_tmpdir = None
            if index_bytes:
                index_tmpdir = tempfile.mkdtemp(prefix="hm-index-")
                tmpdirs.append(index_tmpdir)
                _safe_extract_tar(index_bytes, index_tmpdir)
        except (tarfile.TarError, ValueError) as e:
            for d in tmpdirs:
                shutil.rmtree(d, ignore_errors=True)
            raise HTTPException(400, f"Invalid archive: {e}")

        # Generate IDs
        query_agent_id = uuid4().hex[:12]
        scope_agent_id = uuid4().hex[:12] if scope_tmpdir else None
        effective_scope_agent_id = scope_agent_id or default_scope_id
        index_agent_id = uuid4().hex[:12] if index_tmpdir else None
        run_id = uuid4().hex[:12]
        billing_req = QueryRequest(
            query=prompt or "run uploaded query agent",
            mediator_agent_id=mediator_agent_id,
            max_tokens=max_tokens,
            max_llm_calls=max_llm_calls,
            timeout_seconds=timeout_seconds,
            model=model,
            provider=provider,
            policy=policy,
        )
        billing_models = _billing_models_for_query(hm, billing_req)
        if index_agent_id:
            index_model = hm.pipeline._model_for("index", model)
            if index_model not in billing_models:
                billing_models.append(index_model)
        effective_max_tokens = min(
            max_tokens or hm.settings.max_tokens,
            hm.settings.max_tokens,
        )
        billing = await _prepare_billing_hold(
            request,
            caller,
            hm,
            run_id=run_id,
            provider=(provider or "openrouter").strip().lower(),
            models=billing_models,
            max_tokens=effective_max_tokens * (2 if index_agent_id else 1),
            billable_role="query",
        )

        await asyncio.to_thread(
            hm.run_store.create, run_id, query_agent_id,
            scope_agent_id=effective_scope_agent_id,
            index_agent_id=index_agent_id,
            payer_tenant_id=billing.get("payer_tenant_id"),
            payer_token_id=billing.get("payer_token_id"),
            billable_role=billing.get("billable_role"),
            billing_provider=billing.get("billing_provider"),
            billing_model=billing.get("billing_model"),
            billing_hold_micro_usd=int(billing.get("billing_hold_micro_usd") or 0),
            billing_status=billing.get("billing_status") or "unbilled",
        )

        # Run everything in background
        _spawn_bg(
            app,
            _build_and_run_all(
                hm=hm,
                settings=settings,
                run_id=run_id,
                # Query
                query_tmpdir=query_tmpdir,
                query_agent_id=query_agent_id,
                query_name=query_name,
                query_description=query_description,
                query_entrypoint=query_entrypoint,
                # Scope
                scope_tmpdir=scope_tmpdir,
                scope_agent_id=effective_scope_agent_id,
                scope_name=scope_name,
                scope_entrypoint=scope_entrypoint,
                # Index
                index_tmpdir=index_tmpdir,
                index_agent_id=index_agent_id,
                index_name=index_name,
                index_entrypoint=index_entrypoint,
                # Shared
                prompt=prompt,
                memory_mb=memory_mb,
                max_llm_calls=max_llm_calls,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                mediator_agent_id=mediator_agent_id,
                document_data=document_data,
                document_metadata=document_metadata,
                model=model,
                provider=provider,
                policy=policy,
                tmpdirs=tmpdirs,
                query_inspection_mode=validated_query_mode,
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
            "query_agent_id": query_agent_id,
            "scope_agent_id": effective_scope_agent_id,
            "index_agent_id": index_agent_id,
            "status": "pending",
            "query_inspection_mode": validated_query_mode,
        }

    async def _build_and_run_all(
        hm: Hivemind,
        settings: Settings,
        run_id: str,
        # Query
        query_tmpdir: str,
        query_agent_id: str,
        query_name: str,
        query_description: str,
        query_entrypoint: str | None,
        # Scope
        scope_tmpdir: str | None,
        scope_agent_id: str | None,
        scope_name: str,
        scope_entrypoint: str | None,
        # Index
        index_tmpdir: str | None,
        index_agent_id: str | None,
        index_name: str,
        index_entrypoint: str | None,
        # Shared
        prompt: str,
        memory_mb: int,
        max_llm_calls: int,
        max_tokens: int,
        timeout_seconds: int,
        mediator_agent_id: str | None,
        document_data: str | None,
        document_metadata: str | None,
        model: str | None,
        provider: str | None,
        policy: str | None,
        tmpdirs: list[str],
        query_inspection_mode: str = "full",
        payer_tenant_id: str | None = None,
        payer_token_id: str | None = None,
        billable_role: str = "query",
        billing_provider: str | None = None,
        billing_model: str | None = None,
        billing_hold_micro_usd: int = 0,
    ) -> None:
        """Background: build all agent images in parallel, then run pipeline."""
        import time as _time

        from .sandbox.backend import _create_runner

        billing = {
            "payer_tenant_id": payer_tenant_id,
            "payer_token_id": payer_token_id,
            "billing_provider": billing_provider,
            "billing_model": billing_model,
            "billing_hold_micro_usd": billing_hold_micro_usd,
        }
        try:
            sandbox_settings = build_sandbox_settings(settings)
            runner = _create_runner(sandbox_settings)
            capped_mb = min(memory_mb, settings.container_memory_mb)

            # -- Build stage: build all agents in parallel --
            build_t0 = _time.time()
            await asyncio.to_thread(
                hm.run_store.update_stage, run_id, "build", started_at=build_t0,
            )

            build_tasks = []
            # Query agent (always)
            build_tasks.append(
                _build_single_agent(
                    runner, query_tmpdir, query_agent_id, "query",
                    query_name, query_description, query_entrypoint,
                    capped_mb, max_llm_calls, max_tokens, timeout_seconds, hm,
                    inspection_mode=query_inspection_mode,
                )
            )
            # Scope agent (optional)
            if scope_tmpdir and scope_agent_id:
                build_tasks.append(
                    _build_single_agent(
                        runner, scope_tmpdir, scope_agent_id, "scope",
                        scope_name or f"scope-{scope_agent_id}",
                        "", scope_entrypoint,
                        capped_mb, max_llm_calls, max_tokens, timeout_seconds, hm,
                    )
                )
            # Index agent (optional)
            if index_tmpdir and index_agent_id:
                build_tasks.append(
                    _build_single_agent(
                        runner, index_tmpdir, index_agent_id, "index",
                        index_name or f"index-{index_agent_id}",
                        "", index_entrypoint,
                        capped_mb, max_llm_calls, max_tokens, timeout_seconds, hm,
                    )
                )

            try:
                await asyncio.gather(*build_tasks)
            except Exception as e:
                logger.exception("Image build failed in unified submit %s", run_id)
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
                for d in tmpdirs:
                    shutil.rmtree(d, ignore_errors=True)
                await asyncio.to_thread(
                    hm.run_store.update_stage, run_id, "build",
                    ended_at=_time.time(),
                )

            # -- Index stage (optional, runs before query) --
            if index_agent_id and document_data:
                try:
                    await asyncio.to_thread(
                        hm.run_store.update_status, run_id, "running",
                    )
                    await hm.pipeline.run_index_tracked(
                        index_agent_id=index_agent_id,
                        run_id=run_id,
                        run_store=hm.run_store,
                        document_data=document_data,
                        document_metadata=document_metadata or "{}",
                        max_tokens=max_tokens,
                        model=model,
                        provider=provider,
                        payer_tenant_id=payer_tenant_id,
                        payer_token_id=payer_token_id,
                        billable_role="index",
                        billing_provider=billing_provider,
                        billing_model=billing_model,
                        billing_hold_micro_usd=0,
                    )
                except Exception as e:
                    logger.warning(
                        "Index agent '%s' failed for run %s; "
                        "continuing with query: %s",
                        index_agent_id, run_id, e,
                    )

            # -- Query pipeline (scope → query → mediator) --
            await hm.pipeline.run_query_agent_tracked(
                agent_id=query_agent_id,
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
                payer_tenant_id=payer_tenant_id,
                payer_token_id=payer_token_id,
                billable_role=billable_role,
                billing_provider=billing_provider,
                billing_model=billing_model,
                billing_hold_micro_usd=billing_hold_micro_usd,
            )

        except Exception as e:
            logger.error("Unified submit run %s failed: %s", run_id, e)
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
            max_tokens=min(max_tokens or hm.settings.max_tokens, hm.settings.max_tokens),
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
        from .sandbox.backend import _create_runner

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

    # ── Run status / list ──

    @app.get("/v1/runs/{run_id}")
    async def get_agent_run(
        run_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        """Get the status and result of an agent run."""
        hm = caller.hive
        run = await asyncio.to_thread(hm.run_store.get, run_id)
        if not run or not _caller_can_access_run(caller, run):
            raise HTTPException(404, "Run not found")
        if _caller_can_access_run_payload(caller, run) and run.get(
            "artifacts_enabled", True
        ):
            run["artifacts"] = await asyncio.to_thread(
                hm.artifact_store.list_for_run, run_id
            )
        else:
            run["artifacts"] = []
        run["artifact_retention_seconds"] = hm.settings.artifact_retention_seconds
        run = _redact_run_payload_for_caller(caller, run)
        return JSONResponse(
            content=run,
            headers={"Cache-Control": "no-cache, no-store"},
        )

    @app.get("/v1/runs")
    async def list_agent_runs(
        limit: int = 20,
        token_id: str | None = None,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        """List recent agent runs.

        ``token_id`` (owner-only): filter to runs initiated by a single
        capability token. Pass the 12-hex prefix returned by
            the room creation response. Lets A audit
        per-token activity without leaking the raw bearer.
        Query-token callers can't pass this — their view is implicitly
        scoped to the runs they themselves initiated, but listing other
        tokens' activity is owner-only.
        """
        capped = min(limit, 100)
        if token_id:
            tid = token_id.strip().lower()
            if caller.role != "owner":
                raise HTTPException(
                    status_code=403,
                    detail="token_id filter is owner-only",
                )
            if not tid or len(tid) < 6:
                raise HTTPException(
                    status_code=400,
                    detail="token_id must be at least 6 hex chars",
                )
            rows = await asyncio.to_thread(
                caller.hive.run_store.list_by_token, tid, capped,
            )
            return [_redact_run_payload_for_caller(caller, r) for r in rows]
        if caller.role == "query":
            rows = await asyncio.to_thread(
                caller.hive.run_store.list_by_token, caller.token_id, capped,
            )
            return [_redact_run_payload_for_caller(caller, r) for r in rows]
        rows = await asyncio.to_thread(
            caller.hive.run_store.list_recent, capped,
        )
        return [_redact_run_payload_for_caller(caller, r) for r in rows]

    # ── Artifact fetch (Postgres-backed; no S3) ──

    @app.get(
        "/v1/runs/{run_id}/artifacts/{filename:path}",
    )
    async def get_run_artifact(
        run_id: str,
        filename: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        hm = caller.hive
        """Stream a query-agent artifact.

        Artifacts live in Postgres and expire after
        `artifact_retention_seconds` (default 24h). Nothing is written to
        external object storage.
        """
        run = await asyncio.to_thread(hm.run_store.get, run_id)
        if (
            not run
            or not _caller_can_access_run_payload(caller, run)
            or not run.get("artifacts_enabled", True)
        ):
            raise HTTPException(404, "Artifact not found or expired")
        from .sandbox.models import validate_artifact_filename

        try:
            safe_filename = validate_artifact_filename(filename)
        except ValueError:
            raise HTTPException(400, "Invalid artifact filename")
        artifact = await asyncio.to_thread(
            hm.artifact_store.get, run_id, safe_filename
        )
        if not artifact:
            raise HTTPException(404, "Artifact not found or expired")
        ttl = hm.settings.artifact_retention_seconds
        import email.utils
        from urllib.parse import quote
        expires_at = float(artifact["created_at"]) + ttl
        content_type = artifact["content_type"] or "application/octet-stream"
        if len(content_type) > 100 or any(
            ord(ch) < 32 or ord(ch) == 127 for ch in content_type
        ):
            content_type = "application/octet-stream"
        return Response(
            content=bytes(artifact["content"]),
            media_type=content_type,
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Retention-Seconds": str(ttl),
                "Expires": email.utils.formatdate(expires_at, usegmt=True),
                "Content-Disposition": (
                    f'attachment; filename="{safe_filename}"; '
                    f"filename*=UTF-8''{quote(safe_filename)}"
                ),
            },
        )

    # ── Health ──

    @app.get("/v1/health", response_model=HealthResponse)
    async def health(
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        return await asyncio.to_thread(caller.hive.health)

    return app


class _LazyApp:
    """ASGI wrapper that delays Settings/.env loading until first request."""

    def __init__(self):
        self._app: FastAPI | None = None
        self._lock = threading.Lock()

    def _get_app(self) -> FastAPI:
        if self._app is None:
            with self._lock:
                if self._app is None:
                    self._app = create_app()
        return self._app

    async def __call__(self, scope, receive, send):
        await self._get_app()(scope, receive, send)


app = _LazyApp()


def main():
    import os
    import tempfile
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings()

    # When enclave-terminated TLS is on, bootstrap attestation BEFORE
    # uvicorn.run() so we have the cert/key in hand before the socket
    # opens. The lifespan call becomes a no-op thanks to bootstrap's
    # idempotency guard.
    ssl_kwargs: dict = {}
    if os.environ.get("HIVEMIND_ENCLAVE_TLS"):
        from . import attestation as _att

        logger.info("HIVEMIND_ENCLAVE_TLS=1 — bootstrapping TLS before listen")
        _att.bootstrap()
        tls = _att.get_tls_material()
        if tls is None:
            logger.error(
                "Enclave TLS requested but derivation failed; falling back to HTTP. "
                "Check DSTACK_SIMULATOR_ENDPOINT / /var/run/dstack.sock."
            )
        else:
            cert_pem, key_pem = tls
            # uvicorn wants filesystem paths. tmpfs mounts are safe inside
            # the enclave; the cert/key are derived fresh every boot anyway.
            tdir = tempfile.mkdtemp(prefix="hivemind-tls-")
            cert_path = os.path.join(tdir, "cert.pem")
            key_path = os.path.join(tdir, "key.pem")
            with open(cert_path, "wb") as f:
                f.write(cert_pem)
            with open(key_path, "wb") as f:
                f.write(key_pem)
            os.chmod(key_path, 0o600)
            ssl_kwargs = {
                "ssl_certfile": cert_path,
                "ssl_keyfile": key_path,
            }
            logger.info(
                "TLS cert derived from dstack-KMS; "
                "fingerprint bound into REPORT_DATA v2"
            )

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()

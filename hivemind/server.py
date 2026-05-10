import asyncio
import logging
import re
import secrets
import signal
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from .api.admin_tenants import register_admin_tenant_routes
from .api.agent_registry import register_agent_registry_routes
from .api.agent_uploads import register_agent_upload_routes
from .api.agent_helpers import (
    image_digest as _server_image_digest,
    spawn_bg as _spawn_bg,
    tenant_image_tag as _server_tenant_image_tag,
)
from .api.admin_runs import register_admin_run_routes
from .api.billing import register_admin_billing_routes, register_owner_billing_routes
from .api.runs import register_run_routes
from .api.room_helpers import (
    room_prompt_for_run as _room_prompt_for_run,
    room_wrap_id as _room_wrap_id,
)
from .api.rooms import register_room_routes
from .api.signup import register_signup_routes
from .api.system import register_system_routes
from .api.tenant_owner import register_tenant_owner_routes
from .config import Settings
from .core import Hivemind
from .models import (
    QueryRequest,
    StoreRequest,
    StoreResponse,
)
from .room_vault import RoomVaultSealed
from .tenants import Caller, Role, TenantRegistry
from .version import APP_VERSION

logger = logging.getLogger(__name__)
DEFAULT_ROOM_LLM_PROVIDER = "openrouter"
_RUN_IDEMPOTENCY_HEADER = "x-hivemind-idempotency-key"
_RUN_IDEMPOTENCY_RE = re.compile(r"^[a-f0-9]{12,64}$")


class _AttestationBootstrapTimeout(TimeoutError):
    pass


def _bootstrap_attestation_bounded(timeout_seconds: int) -> bool:
    """Run attestation bootstrap with a hard wall-clock bound.

    This is called on the main thread before uvicorn starts when enclave TLS
    is enabled. A stalled dstack KMS/quote path should degrade attestation,
    not keep the API socket closed indefinitely.
    """
    from . import attestation as _att

    if timeout_seconds <= 0:
        _att.bootstrap()
        return bool(_att.get_bundle().get("ready"))
    if threading.current_thread() is not threading.main_thread():
        _att.bootstrap()
        return bool(_att.get_bundle().get("ready"))

    def _raise_timeout(_signum, _frame):
        raise _AttestationBootstrapTimeout(
            f"attestation bootstrap exceeded {timeout_seconds}s"
        )

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        _att.bootstrap()
    except _AttestationBootstrapTimeout as e:
        _att.disable(str(e))
        return False
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)
    return bool(_att.get_bundle().get("ready"))

# Backward-compatible private helper export used by older tests/tools.
_image_digest = _server_image_digest
_tenant_image_tag = _server_tenant_image_tag


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
            await asyncio.wait_for(
                asyncio.to_thread(attestation.bootstrap),
                timeout=max(1, settings.attestation_bootstrap_timeout_seconds),
            )
        except asyncio.TimeoutError:
            reason = (
                "attestation bootstrap timed out during FastAPI startup "
                f"after {settings.attestation_bootstrap_timeout_seconds}s"
            )
            logger.warning(reason)
            attestation.disable(reason)
        except Exception as e:
            logger.warning("attestation bootstrap raised: %s", e)

        # Docker image pulls/builds must not be coupled to control-plane
        # readiness on small CVMs. DockerRunner ensures base images on demand
        # before agent builds; this optional warmup is for larger operators
        # that explicitly prefer cold-start work at process startup.
        agent_base_task = None
        if settings.bootstrap_agent_base_on_startup:
            async def _bootstrap_agent_base():
                try:
                    from .agent_base_bootstrap import (
                        ensure_agent_base_hermes_image,
                        ensure_agent_base_image,
                    )
                    await asyncio.to_thread(ensure_agent_base_image)
                    await asyncio.to_thread(ensure_agent_base_hermes_image)
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

        Query-token calls attach the participant tenant's ``hmk_`` API key so
        the data owner is not charged for the participant's LLM spend. Owner
        calls default to the owner tenant. Query-token calls without a tenant
        API key are rejected before work starts; the credit-enforcement setting
        controls whether a known payer must have enough positive balance, not
        whether a payer is required.
        """
        payer_key = (
            request.headers.get("X-Hivemind-Api-Key")
            or request.headers.get("X-Hivemind-Payer-Key")
            or ""
        ).strip()
        if payer_key:
            payer = await asyncio.to_thread(
                _registry(request).resolve_payer_key,
                payer_key,
            )
            if payer is None:
                raise HTTPException(401, "invalid tenant API key")
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
        if caller.role == "share":
            raise HTTPException(
                402,
                "share-link asks require X-Hivemind-Api-Key so usage can be "
                "charged to the asker's tenant. Pass your own hmk_ key in "
                "X-Hivemind-Api-Key (or use `hmctl --profile NAME room ask "
                "'hmroom://...?share=...'` from a CLI profile that has one).",
            )
        raise HTTPException(
            402,
            "room invite queries require a tenant API key so usage can be "
            "charged to the querying tenant. In the CLI, run "
            "`hmctl --profile NAME init --service URL --api-key hmk_...` "
            "and then retry with `hmctl --profile NAME room ask ...`.",
        )

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
        if req_provider:
            return req_provider.strip().lower()
        if DEFAULT_ROOM_LLM_PROVIDER in allowed:
            return DEFAULT_ROOM_LLM_PROVIDER
        return allowed[0]

    def _billing_models_for_query(hm: Hivemind, req: QueryRequest) -> list[str]:
        roles = ["scope", "query"]
        if req.mediator_agent_id or hm.settings.default_mediator_agent:
            roles.append("mediator")
        models: list[str] = []
        for role in roles:
            role_override = {
                "scope": req.scope_model,
                "query": req.query_model,
                "mediator": req.mediator_model,
            }.get(role)
            model = hm.pipeline._model_for(role, role_override or req.model)
            if model and model not in models:
                models.append(model)
        return models

    def _route_error_status(exc: ValueError) -> int:
        msg = str(exc).lower()
        if "disabled by operator" in msg or "requires hivemind_" in msg:
            return 503
        return 400

    def _run_id_for_request(request: Request | None) -> str:
        if request is None:
            return uuid4().hex[:12]
        key = (request.headers.get(_RUN_IDEMPOTENCY_HEADER) or "").strip().lower()
        if not key:
            return uuid4().hex[:12]
        if not _RUN_IDEMPOTENCY_RE.fullmatch(key):
            raise HTTPException(
                400,
                "X-Hivemind-Idempotency-Key must be 12-64 lowercase hex "
                "characters",
            )
        return key

    def _run_response_for_existing(
        existing: dict,
        room: dict | None,
        caller: Caller,
    ) -> dict:
        expected_room_id = (room or {}).get("room_id")
        existing_room_id = existing.get("room_id")
        if expected_room_id and existing_room_id != expected_room_id:
            raise HTTPException(
                409,
                "idempotency key already belongs to a different room run",
            )
        if (existing.get("issuer_token_id") or "") != (caller.token_id or ""):
            raise HTTPException(
                409,
                "idempotency key already belongs to a different caller",
            )
        return {
            "run_id": existing["run_id"],
            "query_agent_id": existing.get("agent_id"),
            "scope_agent_id": existing.get("scope_agent_id"),
            "room_id": existing_room_id,
            "status": existing.get("status") or "pending",
            "idempotent_replay": True,
        }

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
        if caller.hive.start_default_agent_image_warmup():
            _spawn_bg(request.app, caller.hive.warm_default_agent_images())
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

    register_signup_routes(app, settings)
    register_admin_tenant_routes(app, settings, check_admin)
    register_admin_billing_routes(app, check_admin, settings)
    register_admin_run_routes(app, check_admin)
    register_owner_billing_routes(app, requires_role)
    register_tenant_owner_routes(app, _bearer, requires_role, get_tenant_hive)
    register_system_routes(app, settings, check_admin, requires_role)
    register_run_routes(app, requires_role)
    register_agent_registry_routes(app, settings, requires_role, get_tenant_hive)

    # ── Internal pipeline primitives ──
    #
    # Room endpoints below are the public execution surface. The tenant SQL
    # primitive lets owners run DDL/DML against their tenant database from
    # outside a room — used by the website's database browser and the
    # bootstrap scripts to seed tables before binding them into rooms.

    @app.post("/v1/tenant/sql", response_model=StoreResponse)
    async def tenant_sql(
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

        run_id = _run_id_for_request(request)
        existing = await asyncio.to_thread(hm.run_store.get, run_id)
        if existing is not None:
            return _run_response_for_existing(existing, room, caller)

        requested_max_tokens = req.max_tokens or hm.settings.default_query_max_tokens
        effective_max_tokens = min(requested_max_tokens, hm.settings.max_tokens)
        try:
            hm.pipeline.validate_llm_route(
                req.provider,
                (room or {}).get("allowed_llm_providers"),
                _billing_models_for_query(hm, req),
            )
        except ValueError as e:
            raise HTTPException(_route_error_status(e), str(e)) from e
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
                max_tokens=effective_max_tokens,
                max_calls=req.max_llm_calls,
                timeout_seconds=req.timeout_seconds,
                model=req.model,
                scope_model=req.scope_model,
                query_model=req.query_model,
                mediator_model=req.mediator_model,
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
                # Per-room SQL data sources from the signed manifest. Legacy
                # rooms without this field pass None and keep the old
                # unrestricted behavior — see hivemind/tools.py for enforcement.
                allowed_tables=(
                    ((room or {}).get("manifest") or {}).get("allowed_tables")
                ),
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

    register_room_routes(
        app,
        settings,
        _bearer,
        requires_role,
        _submit_query_run_for_request,
    )

    register_agent_upload_routes(
        app,
        settings,
        _bearer,
        requires_role,
        get_tenant_hive,
        _require_scope_agent_id,
        _ensure_scope_agent_exists,
        _prepare_billing_hold,
        _settle_empty_billing,
        _billing_provider_for_room,
        _billing_models_for_query,
    )

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
    from . import attestation as _att

    if _att.enclave_tls_enabled():
        logger.info("HIVEMIND_ENCLAVE_TLS enabled — bootstrapping TLS before listen")
        ready = _bootstrap_attestation_bounded(
            max(1, settings.attestation_bootstrap_timeout_seconds)
        )
        tls = _att.get_tls_material()
        if tls is None:
            from . import tls as _tls

            reason = (
                "attestation TLS derivation unavailable before listen; "
                "serving degraded temporary TLS"
            )
            if not ready:
                reason = (_att.get_bundle().get("reason") or reason)
            _att.disable(reason)
            logger.error("%s", reason)
            fallback = _tls.generate_ephemeral_tls_cert_and_key()
            tls = fallback["cert_pem"], fallback["key_pem"]
        else:
            logger.info(
                "TLS cert derived from dstack-KMS; "
                "fingerprint bound into REPORT_DATA v2"
            )
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

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()

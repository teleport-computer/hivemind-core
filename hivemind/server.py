import asyncio
import json
import logging
import os
import secrets
import shutil
import tarfile
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import ValidationError

from .config import Settings
from .core import Hivemind
from .models import (
    HealthResponse,
    IndexRequest,
    IndexResponse,
    QueryRequest,
    QueryResponse,
    StoreRequest,
    StoreResponse,
)
from .sandbox.settings import build_sandbox_settings
from .tenants import Caller, Role, TenantRegistry
from .version import APP_VERSION

logger = logging.getLogger(__name__)

# Load UI HTML from file next to this module
_UI_HTML_PATH = Path(__file__).with_name("ui.html")
_UI_HTML = _UI_HTML_PATH.read_text(encoding="utf-8") if _UI_HTML_PATH.exists() else "<h1>UI not found</h1>"

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


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
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

        agent_base_task = asyncio.create_task(_bootstrap_agent_base())

        registry = TenantRegistry(settings)
        app.state.registry = registry
        app.state.agent_base_task = agent_base_task
        yield
        # Close per-tenant Hivemind instances + control DB.
        await asyncio.to_thread(registry.close)
        agent_base_task.cancel()

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
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "'name' required")
        registry = _registry(request)
        try:
            result = await asyncio.to_thread(registry.provision, name)
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
        registry = _registry(request)
        try:
            result = await asyncio.to_thread(
                registry.register_existing, name, db_name, api_key, tenant_id
            )
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
        one-shot migrations (e.g., renaming the legacy ``hivemind`` DB to
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
        listings. ``hivemind run`` fails 500 against them.
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

    # ── Capability tokens (delegated query) ──
    #
    # Owner mints query tokens to share narrow capabilities with third
    # parties without exposing their hmk_ key. Plaintext is shown
    # exactly once at /issue; only the hash is stored. Listing reveals
    # token_id (a short hex prefix of the hash) for revocation; the
    # plaintext is never recoverable.

    @app.post("/v1/tokens")
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

    @app.get("/v1/tokens")
    async def list_tokens(
        request: Request, hm: Hivemind = Depends(get_tenant_hive),
    ):
        registry = _registry(request)
        tenant_id = request.state.tenant_id
        rows = await asyncio.to_thread(registry.list_capabilities, tenant_id)
        return {"tokens": rows}

    @app.delete("/v1/tokens/{token_id}")
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

    # ── Pipeline endpoints ──

    @app.post(
        "/v1/store",
        response_model=StoreResponse,
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

    @app.post(
        "/v1/query",
        response_model=QueryResponse,
    )
    async def query(
        req: QueryRequest,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        req = _force_scope_for_query_token(req, caller)
        try:
            return await caller.hive.pipeline.run_query(req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ── Async query (submit + poll) ──
    # For deployments behind reverse proxies with short timeouts (e.g. Phala 60s).

    _pending_queries: dict[str, dict] = {}

    @app.post("/v1/query/submit")
    async def submit_query(
        req: QueryRequest,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        """Submit a query for async processing. Returns a run_id to poll."""
        req = _force_scope_for_query_token(req, caller)
        hm = caller.hive
        run_id = uuid4().hex[:12]
        _pending_queries[run_id] = {
            "status": "running",
            "result": None,
            "error": None,
            "tenant_id": hm.tenant_id,
        }

        async def _run():
            try:
                result = await hm.pipeline.run_query(req)
                _pending_queries[run_id] = {
                    "status": "completed",
                    "result": result.model_dump(),
                    "error": None,
                    "tenant_id": hm.tenant_id,
                }
            except Exception as e:
                _pending_queries[run_id] = {
                    "status": "failed",
                    "result": None,
                    "error": str(e),
                    "tenant_id": hm.tenant_id,
                }

        asyncio.create_task(_run())
        return {"run_id": run_id, "status": "running"}

    @app.get("/v1/query/runs/{run_id}")
    async def get_query_status(
        run_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        """Poll the status of an async query."""
        entry = _pending_queries.get(run_id)
        if not entry or entry.get("tenant_id") != caller.tenant_id:
            raise HTTPException(404, "Query run not found")
        payload = {k: v for k, v in entry.items() if k != "tenant_id"}
        return JSONResponse(
            content={"run_id": run_id, **payload},
            headers={"Cache-Control": "no-cache, no-store"},
        )

    @app.post(
        "/v1/index",
        response_model=IndexResponse,
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

    # ── Agent CRUD ──

    from .sandbox.models import AgentConfig, AgentCreateRequest

    @app.post("/v1/agents")
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
        """Query-token holders can only see the scope agent they're bound to."""
        if caller.role != "query":
            return True
        return agent_id == (caller.constraints.get("scope_agent_id") or "")

    @app.get("/v1/agents")
    async def list_agents(
        type: str | None = None,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        agents = await asyncio.to_thread(caller.hive.agent_store.list_agents, type)
        if caller.role == "query":
            bound = caller.constraints.get("scope_agent_id") or ""
            agents = [a for a in agents if a.agent_id == bound]
        return [a.model_dump() for a in agents]

    @app.get("/v1/agents/{agent_id}")
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

    @app.delete("/v1/agents/{agent_id}")
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

    @app.get("/v1/agents/{agent_id}/files")
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

    @app.get("/v1/agents/{agent_id}/files/{file_path:path}")
    async def read_agent_file(
        agent_id: str,
        file_path: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        if not _query_token_visible_agent(caller, agent_id):
            raise HTTPException(404, "Agent not found")
        content = await asyncio.to_thread(
            caller.hive.agent_store.read_file, agent_id, file_path
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
    #     after re-fetching files via /v1/agents/{id}/files{,/{path}})
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
            "files_count": digests["files_count"],
            "files_digest_sha256": digests["files_digest"],
            "attested_files_count": digests["attested_files_count"],
            "attested_files_digest_sha256": digests["attested_files_digest"],
            "image_digest": _image_digest(agent.image),
            "attestation": _att.get_bundle(),
        }

    @app.get("/v1/agents/{agent_id}/attest")
    async def attest_agent(
        agent_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        if not _query_token_visible_agent(caller, agent_id):
            raise HTTPException(404, "Agent not found")
        return await _build_agent_attestation(caller, agent_id)

    # /v1/scope-attest — backwards-compatible alias designed for the
    # query-token recipient flow. Resolves the agent_id from the token
    # binding (query) or the ?scope_agent_id= query param (owner) and
    # delegates to the canonical helper. Adds ``scope_agent_id`` at the
    # top of the response for clients that key off that field.
    @app.get("/v1/scope-attest")
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

    # ── Agent upload ──

    @app.post("/v1/agents/upload")
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

        asyncio.create_task(_build_upload_agent())

        return {"agent_id": agent_id, "run_id": run_id, "status": "pending"}

    # ── Unified agent submit ──

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
            )
        except Exception as e:
            logger.warning("Failed to save agent files for %s: %s", agent_id, e)

        return image_tag

    @app.post("/v1/agents/submit")
    async def submit_agents(
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
        hm: Hivemind = Depends(get_tenant_hive),
    ):
        """Upload query agent (required) + optional scope/index agents,
        build all, then run the full pipeline with tracking."""
        from .sandbox.backend import _create_runner

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
        index_agent_id = uuid4().hex[:12] if index_tmpdir else None
        run_id = uuid4().hex[:12]

        await asyncio.to_thread(
            hm.run_store.create, run_id, query_agent_id,
            scope_agent_id=scope_agent_id,
            index_agent_id=index_agent_id,
        )

        # Run everything in background
        asyncio.create_task(
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
                scope_agent_id=scope_agent_id,
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
                tmpdirs=tmpdirs,
            )
        )

        return {
            "run_id": run_id,
            "query_agent_id": query_agent_id,
            "scope_agent_id": scope_agent_id,
            "index_agent_id": index_agent_id,
            "status": "pending",
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
        tmpdirs: list[str],
    ) -> None:
        """Background: build all agent images in parallel, then run pipeline."""
        import time as _time

        from .sandbox.backend import _create_runner

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
            )

        except Exception as e:
            logger.error("Unified submit run %s failed: %s", run_id, e)
            try:
                await asyncio.to_thread(
                    hm.run_store.update_status, run_id, "failed",
                    error=str(e)[:500],
                )
            except Exception:
                pass

    # ── Query agent submit + run tracking (async-submit flow) ──

    @app.post("/v1/query-agents/submit")
    async def submit_query_agent(
        name: str = Form(...),
        archive: UploadFile = File(...),
        prompt: str = Form(""),
        description: str = Form(""),
        entrypoint: str | None = Form(None),
        memory_mb: Annotated[int, Form(ge=16)] = 256,
        max_llm_calls: Annotated[int, Form(ge=1)] = 20,
        max_tokens: Annotated[int, Form(ge=1)] = 100_000,
        timeout_seconds: Annotated[int, Form(ge=1)] = 120,
        scope_agent_id: str | None = Form(None),
        mediator_agent_id: str | None = Form(None),
        # LLM routing (optional). ``provider="tinfoil"`` requires the
        # server to have HIVEMIND_TINFOIL_API_KEY configured.
        model: str | None = Form(None),
        provider: str | None = Form(None),
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        """Upload query agent source, create a run record, and kick off execution."""
        # Query-token callers cannot pick their own scope agent — pin it
        # to the one the owner bound the token to.
        if caller.role == "query":
            scope_agent_id = caller.constraints.get("scope_agent_id") or ""
        hm = caller.hive
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
        await asyncio.to_thread(hm.run_store.create, run_id, agent_id)

        # Everything else runs in background
        asyncio.create_task(
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
            )
        )

        return {
            "run_id": run_id,
            "agent_id": agent_id,
            "status": "pending",
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
    ) -> None:
        """Background task: build image, register agent, run pipeline."""
        from .sandbox.backend import _create_runner

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
            )
            await asyncio.to_thread(hm.agent_store.create, config)

            if captured_files:
                try:
                    await asyncio.to_thread(
                        hm.agent_store.save_files, agent_id, captured_files,
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
            )

        except Exception as e:
            logger.error("Background build+run %s failed: %s", run_id, e)
            try:
                await asyncio.to_thread(
                    hm.run_store.update_status, run_id, "failed",
                    error=str(e)[:500],
                )
            except Exception:
                pass

    @app.get("/v1/query-agents/runs/{run_id}")
    async def get_query_run(
        run_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        """Get the status and result of a query agent run."""
        hm = caller.hive
        run = await asyncio.to_thread(hm.run_store.get, run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        run["artifacts"] = await asyncio.to_thread(
            hm.artifact_store.list_for_run, run_id
        )
        run["artifact_retention_seconds"] = hm.settings.artifact_retention_seconds
        return JSONResponse(
            content=run,
            headers={"Cache-Control": "no-cache, no-store"},
        )

    # ── List recent runs ──

    @app.get("/v1/query-agents/runs")
    async def list_query_runs(
        limit: int = 20,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        """List recent query agent runs."""
        return await asyncio.to_thread(
            caller.hive.run_store.list_recent, min(limit, 100)
        )

    # ── Unified run status ──

    @app.get("/v1/agent-runs/{run_id}")
    async def get_agent_run(
        run_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        """Get the status and result of an agent run."""
        hm = caller.hive
        run = await asyncio.to_thread(hm.run_store.get, run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        run["artifacts"] = await asyncio.to_thread(
            hm.artifact_store.list_for_run, run_id
        )
        run["artifact_retention_seconds"] = hm.settings.artifact_retention_seconds
        return JSONResponse(
            content=run,
            headers={"Cache-Control": "no-cache, no-store"},
        )

    @app.get("/v1/agent-runs")
    async def list_agent_runs(
        limit: int = 20,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        """List recent agent runs."""
        return await asyncio.to_thread(
            caller.hive.run_store.list_recent, min(limit, 100)
        )

    # ── Artifact fetch (Postgres-backed; no S3) ──

    @app.get(
        "/v1/query/runs/{run_id}/artifacts/{filename:path}",
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
        artifact = await asyncio.to_thread(
            hm.artifact_store.get, run_id, filename
        )
        if not artifact:
            raise HTTPException(404, "Artifact not found or expired")
        ttl = hm.settings.artifact_retention_seconds
        import email.utils
        expires_at = float(artifact["created_at"]) + ttl
        return Response(
            content=bytes(artifact["content"]),
            media_type=artifact["content_type"] or "application/octet-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Retention-Seconds": str(ttl),
                "Expires": email.utils.formatdate(expires_at, usegmt=True),
                "Content-Disposition": (
                    f'attachment; filename="{filename}"'
                ),
            },
        )

    # ── Health ──

    @app.get("/v1/health", response_model=HealthResponse)
    async def health(
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        return await asyncio.to_thread(caller.hive.health)

    # ── Web UI ──

    @app.get("/", response_class=HTMLResponse)
    async def ui_page():
        return _UI_HTML

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

import asyncio
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
from .tenants import TenantRegistry
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
        # Ensure hivemind-agent-base:latest exists before accepting uploads.
        # Runs off the event loop because image build can take minutes.
        try:
            from .agent_base_bootstrap import ensure_agent_base_image
            await asyncio.to_thread(ensure_agent_base_image)
        except Exception as e:
            logger.warning("agent-base bootstrap raised: %s", e)
        registry = TenantRegistry(settings)
        app.state.registry = registry
        yield
        # Close per-tenant Hivemind instances + control DB.
        await asyncio.to_thread(registry.close)

    app = FastAPI(title="Hivemind Core", version=APP_VERSION, lifespan=lifespan)

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

    async def get_tenant_hive(request: Request) -> Hivemind:
        """Auth + tenant resolution in one dep.

        Extracts the bearer token, looks it up in the control DB, and
        returns the caller's per-tenant Hivemind (LRU-cached). Raises
        401 for any missing/invalid/suspended key.
        """
        registry = _registry(request)
        token = _bearer(request)
        resolved = await asyncio.to_thread(registry.resolve, token)
        if resolved is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        tenant_id, hm = resolved
        request.state.tenant_id = tenant_id
        return hm

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

    # ── Liveness (unauthed, no tenant scope) ──

    @app.get("/v1/healthz")
    async def healthz():
        return {"status": "ok", "version": APP_VERSION}

    # ── Pipeline endpoints ──

    @app.post(
        "/v1/store",
        response_model=StoreResponse,
    )
    async def store(req: StoreRequest, hm: Hivemind = Depends(get_tenant_hive)):
        try:
            return await hm.pipeline.run_store(req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post(
        "/v1/query",
        response_model=QueryResponse,
    )
    async def query(req: QueryRequest, hm: Hivemind = Depends(get_tenant_hive)):
        try:
            return await hm.pipeline.run_query(req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ── Async query (submit + poll) ──
    # For deployments behind reverse proxies with short timeouts (e.g. Phala 60s).

    _pending_queries: dict[str, dict] = {}

    @app.post("/v1/query/submit")
    async def submit_query(req: QueryRequest, hm: Hivemind = Depends(get_tenant_hive)):
        """Submit a query for async processing. Returns a run_id to poll."""
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
        run_id: str, hm: Hivemind = Depends(get_tenant_hive)
    ):
        """Poll the status of an async query."""
        entry = _pending_queries.get(run_id)
        if not entry or entry.get("tenant_id") != hm.tenant_id:
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
    async def get_schema(hm: Hivemind = Depends(get_tenant_hive)):
        schema = await asyncio.to_thread(hm.db.get_schema)
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

    @app.get("/v1/agents")
    async def list_agents(
        type: str | None = None,
        hm: Hivemind = Depends(get_tenant_hive),
    ):
        agents = await asyncio.to_thread(hm.agent_store.list_agents, type)
        return [a.model_dump() for a in agents]

    @app.get("/v1/agents/{agent_id}")
    async def get_agent(
        agent_id: str, hm: Hivemind = Depends(get_tenant_hive)
    ):
        agent = await asyncio.to_thread(hm.agent_store.get, agent_id)
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
        hm: Hivemind = Depends(get_tenant_hive),
    ):
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

        try:
            files = await runner.extract_image_files_async(image_tag)
            await asyncio.to_thread(hm.agent_store.save_files, agent_id, files)
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
        hm: Hivemind = Depends(get_tenant_hive),
    ):
        """Upload query agent source, create a run record, and kick off execution."""
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

            try:
                await runner.build_image_async(tmpdir, image_tag)
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

            try:
                files = await runner.extract_image_files_async(image_tag)
                await asyncio.to_thread(hm.agent_store.save_files, agent_id, files)
            except Exception as e:
                logger.warning("Failed to save agent files for %s: %s", agent_id, e)

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
        run_id: str, hm: Hivemind = Depends(get_tenant_hive)
    ):
        """Get the status and result of a query agent run."""
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
        limit: int = 20, hm: Hivemind = Depends(get_tenant_hive)
    ):
        """List recent query agent runs."""
        return await asyncio.to_thread(hm.run_store.list_recent, min(limit, 100))

    # ── Unified run status ──

    @app.get("/v1/agent-runs/{run_id}")
    async def get_agent_run(
        run_id: str, hm: Hivemind = Depends(get_tenant_hive)
    ):
        """Get the status and result of an agent run."""
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
        limit: int = 20, hm: Hivemind = Depends(get_tenant_hive)
    ):
        """List recent agent runs."""
        return await asyncio.to_thread(hm.run_store.list_recent, min(limit, 100))

    # ── Artifact fetch (Postgres-backed; no S3) ──

    @app.get(
        "/v1/query/runs/{run_id}/artifacts/{filename:path}",
    )
    async def get_run_artifact(
        run_id: str, filename: str, hm: Hivemind = Depends(get_tenant_hive)
    ):
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
    async def health(hm: Hivemind = Depends(get_tenant_hive)):
        return await asyncio.to_thread(hm.health)

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
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()

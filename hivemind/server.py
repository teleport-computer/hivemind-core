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
from .sandbox.bridge_dispatch import get_dispatcher
from .sandbox.settings import build_sandbox_settings
from .version import APP_VERSION

logger = logging.getLogger(__name__)

_IGNORED_TAR_TYPES = {
    tarfile.XHDTYPE,         # PAX extended header
    tarfile.XGLTYPE,         # PAX global header
    tarfile.GNUTYPE_LONGNAME,
    tarfile.GNUTYPE_LONGLINK,
}

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
        hm = Hivemind(settings)
        app.state.hivemind = hm
        yield
        await hm.close()

    app = FastAPI(title="Hivemind Core", version=APP_VERSION, lifespan=lifespan)

    # Mount bridge dispatcher for Phala mode (remote CVMs route bridge
    # requests through the main server, dispatched by session token).
    if settings.sandbox_backend == "phala":
        app.mount("/bridge", get_dispatcher())

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

    def get_hivemind(request: Request) -> Hivemind:
        return request.app.state.hivemind

    async def check_auth(request: Request):
        if not settings.api_key:
            return
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Unauthorized")
        token = auth.removeprefix("Bearer ").strip()
        if not secrets.compare_digest(token, settings.api_key):
            raise HTTPException(status_code=401, detail="Unauthorized")

    # ── Pipeline endpoints ──

    @app.post(
        "/v1/store",
        response_model=StoreResponse,
        dependencies=[Depends(check_auth)],
    )
    async def store(req: StoreRequest, hm: Hivemind = Depends(get_hivemind)):
        try:
            return await hm.pipeline.run_store(req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post(
        "/v1/query",
        response_model=QueryResponse,
        dependencies=[Depends(check_auth)],
    )
    async def query(req: QueryRequest, hm: Hivemind = Depends(get_hivemind)):
        try:
            return await hm.pipeline.run_query(req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ── Async query (submit + poll) ──
    # For deployments behind reverse proxies with short timeouts (e.g. Phala 60s).

    _pending_queries: dict[str, dict] = {}

    @app.post("/v1/query/submit", dependencies=[Depends(check_auth)])
    async def submit_query(req: QueryRequest, hm: Hivemind = Depends(get_hivemind)):
        """Submit a query for async processing. Returns a run_id to poll."""
        run_id = uuid4().hex[:12]
        _pending_queries[run_id] = {"status": "running", "result": None, "error": None}

        async def _run():
            try:
                result = await hm.pipeline.run_query(req)
                _pending_queries[run_id] = {
                    "status": "completed",
                    "result": result.model_dump(),
                    "error": None,
                }
            except Exception as e:
                _pending_queries[run_id] = {
                    "status": "failed",
                    "result": None,
                    "error": str(e),
                }

        asyncio.create_task(_run())
        return {"run_id": run_id, "status": "running"}

    @app.get("/v1/query/runs/{run_id}", dependencies=[Depends(check_auth)])
    async def get_query_status(run_id: str):
        """Poll the status of an async query."""
        entry = _pending_queries.get(run_id)
        if not entry:
            raise HTTPException(404, "Query run not found")
        return {"run_id": run_id, **entry}

    @app.post(
        "/v1/index",
        response_model=IndexResponse,
        dependencies=[Depends(check_auth)],
    )
    async def index(req: IndexRequest, hm: Hivemind = Depends(get_hivemind)):
        try:
            return await hm.pipeline.run_index(req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ── Admin schema endpoint ──

    @app.get(
        "/v1/admin/schema",
        dependencies=[Depends(check_auth)],
    )
    async def get_schema(hm: Hivemind = Depends(get_hivemind)):
        schema = await asyncio.to_thread(hm.db.get_schema)
        return {"schema": schema}

    # ── Agent CRUD ──

    from .sandbox.models import AgentConfig, AgentCreateRequest

    @app.post("/v1/agents", dependencies=[Depends(check_auth)])
    async def register_agent(
        req: AgentCreateRequest,
        hm: Hivemind = Depends(get_hivemind),
    ):
        from .core import _create_runner

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

    @app.get("/v1/agents", dependencies=[Depends(check_auth)])
    async def list_agents(hm: Hivemind = Depends(get_hivemind)):
        agents = await asyncio.to_thread(hm.agent_store.list_agents)
        return [a.model_dump() for a in agents]

    @app.get("/v1/agents/{agent_id}", dependencies=[Depends(check_auth)])
    async def get_agent(
        agent_id: str, hm: Hivemind = Depends(get_hivemind)
    ):
        agent = await asyncio.to_thread(hm.agent_store.get, agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        return agent.model_dump()

    @app.delete("/v1/agents/{agent_id}", dependencies=[Depends(check_auth)])
    async def delete_agent(
        agent_id: str, hm: Hivemind = Depends(get_hivemind)
    ):
        if not await asyncio.to_thread(hm.agent_store.delete, agent_id):
            raise HTTPException(404, "Agent not found")
        return {"status": "ok"}

    # ── Agent upload ──

    @app.post("/v1/agents/upload", dependencies=[Depends(check_auth)])
    async def upload_agent(
        name: str = Form(...),
        archive: UploadFile = File(...),
        description: str = Form(""),
        entrypoint: str | None = Form(None),
        memory_mb: Annotated[int, Form(ge=16)] = 256,
        max_llm_calls: Annotated[int, Form(ge=1)] = 20,
        max_tokens: Annotated[int, Form(ge=1)] = 100_000,
        timeout_seconds: Annotated[int, Form(ge=1)] = 120,
        hm: Hivemind = Depends(get_hivemind),
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
            try:
                _safe_extract_tar(content, tmpdir)
            except (tarfile.TarError, ValueError) as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid archive: {e}",
                )
            except Exception:
                logger.exception("Unexpected archive extraction failure")
                raise HTTPException(
                    status_code=500,
                    detail="Archive extraction failed",
                )

            agent_id = uuid4().hex[:12]
            is_phala = settings.sandbox_backend == "phala"

            if is_phala:
                # Phala mode: store source files directly, no Docker build.
                # PhalaRunner will bundle them into CVM env at runtime.
                from .sandbox.backend import PHALA_QUERY_BASE_IMAGE

                image_tag = PHALA_QUERY_BASE_IMAGE
            else:
                # Docker mode: build image locally
                from .core import _create_runner

                sandbox_settings = build_sandbox_settings(settings)
                runner = _create_runner(sandbox_settings)

                image_tag = f"hivemind-agent-{agent_id}:latest"
                try:
                    await runner.build_image_async(tmpdir, image_tag)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))
                except Exception:
                    logger.exception("Image build failed for uploaded agent")
                    raise HTTPException(
                        status_code=500,
                        detail="Image build failed",
                    )

            try:
                config = AgentConfig(
                    agent_id=agent_id,
                    name=name,
                    description=description,
                    image=image_tag,
                    entrypoint=entrypoint,
                    memory_mb=min(memory_mb, settings.container_memory_mb),
                    max_llm_calls=max_llm_calls,
                    max_tokens=max_tokens,
                    timeout_seconds=timeout_seconds,
                )
            except ValidationError as e:
                raise HTTPException(
                    status_code=422,
                    detail=e.errors(),
                )
            await asyncio.to_thread(hm.agent_store.create, config)

            # Save source files to DB
            file_count = 0
            try:
                if is_phala:
                    # Read files directly from extracted archive
                    files = _read_extracted_files(tmpdir)
                else:
                    # Extract from built Docker image
                    files = await runner.extract_image_files_async(image_tag)
                await asyncio.to_thread(
                    hm.agent_store.save_files, agent_id, files
                )
                file_count = len(files)
            except Exception as e:
                logger.warning(
                    "Failed to save agent files for %s: %s", agent_id, e
                )

            return {
                "agent_id": agent_id,
                "name": name,
                "files_extracted": file_count,
            }
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ── Query agent submit + run tracking ──

    @app.post("/v1/query-agents/submit", dependencies=[Depends(check_auth)])
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
        hm: Hivemind = Depends(get_hivemind),
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
            try:
                _safe_extract_tar(content, tmpdir)
            except (tarfile.TarError, ValueError) as e:
                raise HTTPException(status_code=400, detail=f"Invalid archive: {e}")

            agent_id = uuid4().hex[:12]
            is_phala = settings.sandbox_backend == "phala"

            if is_phala:
                from .sandbox.backend import PHALA_QUERY_BASE_IMAGE

                image_tag = PHALA_QUERY_BASE_IMAGE
            else:
                from .core import _create_runner

                sandbox_settings = build_sandbox_settings(settings)
                runner = _create_runner(sandbox_settings)

                image_tag = f"hivemind-agent-{agent_id}:latest"
                try:
                    await runner.build_image_async(tmpdir, image_tag)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))
                except Exception:
                    logger.exception("Image build failed for uploaded query agent")
                    raise HTTPException(status_code=500, detail="Image build failed")

            try:
                config = AgentConfig(
                    agent_id=agent_id,
                    name=name,
                    description=description,
                    image=image_tag,
                    entrypoint=entrypoint,
                    memory_mb=min(memory_mb, settings.container_memory_mb),
                    max_llm_calls=max_llm_calls,
                    max_tokens=max_tokens,
                    timeout_seconds=timeout_seconds,
                )
            except ValidationError as e:
                raise HTTPException(status_code=422, detail=e.errors())

            await asyncio.to_thread(hm.agent_store.create, config)

            # Save source files to DB
            try:
                if is_phala:
                    files = _read_extracted_files(tmpdir)
                else:
                    files = await runner.extract_image_files_async(image_tag)
                await asyncio.to_thread(hm.agent_store.save_files, agent_id, files)
            except Exception as e:
                logger.warning("Failed to save agent files for %s: %s", agent_id, e)

            # Create run record
            run_id = uuid4().hex[:12]
            await asyncio.to_thread(hm.run_store.create, run_id, agent_id)

            # Kick off background execution (full 3-stage pipeline)
            asyncio.create_task(
                hm.pipeline.run_query_agent_tracked(
                    agent_id=agent_id,
                    run_id=run_id,
                    run_store=hm.run_store,
                    s3_uploader=hm.s3_uploader,
                    prompt=prompt,
                    scope_agent_id=scope_agent_id,
                    mediator_agent_id=mediator_agent_id,
                    max_tokens=max_tokens,
                )
            )

            return {
                "run_id": run_id,
                "agent_id": agent_id,
                "status": "pending",
            }
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @app.get("/v1/query-agents/runs/{run_id}", dependencies=[Depends(check_auth)])
    async def get_query_run(
        run_id: str, hm: Hivemind = Depends(get_hivemind)
    ):
        """Get the status and result of a query agent run."""
        run = await asyncio.to_thread(hm.run_store.get, run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        run["download_url"] = None
        if run.get("s3_url") and hm.s3_uploader:
            run["download_url"] = await asyncio.to_thread(
                hm.s3_uploader.presign_url, run["s3_url"]
            )
        return run

    # ── Health ──

    @app.get("/v1/health", response_model=HealthResponse)
    async def health(hm: Hivemind = Depends(get_hivemind)):
        return await asyncio.to_thread(hm.health)

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

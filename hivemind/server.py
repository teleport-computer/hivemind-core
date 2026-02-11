import json
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings
from .core import Hivemind
from .models import (
    HealthResponse,
    IndexEntry,
    QueryRequest,
    QueryResponse,
    StoreRequest,
    StoreResponse,
)


logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.hivemind = Hivemind(settings)
        yield
        app.state.hivemind.storage.close()

    app = FastAPI(title="Hivemind Core", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def get_hivemind(request: Request) -> Hivemind:
        return request.app.state.hivemind

    async def check_auth(request: Request):
        if not settings.api_key:
            return
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {settings.api_key}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.post(
        "/v1/store",
        response_model=StoreResponse,
        dependencies=[Depends(check_auth)],
    )
    async def store(req: StoreRequest, hm: Hivemind = Depends(get_hivemind)):
        try:
            return await hm.store(req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post(
        "/v1/query",
        response_model=QueryResponse,
        dependencies=[Depends(check_auth)],
    )
    async def query(req: QueryRequest, hm: Hivemind = Depends(get_hivemind)):
        try:
            return await hm.query(req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.patch(
        "/v1/records/{record_id}/index",
        dependencies=[Depends(check_auth)],
    )
    async def update_index(
        record_id: str,
        index: IndexEntry,
        hm: Hivemind = Depends(get_hivemind),
    ):
        ok = hm.storage.update_index(
            record_id=record_id,
            title=index.title,
            summary=index.summary,
            tags=",".join(index.tags),
            key_claims=",".join(index.key_claims),
            extra=json.dumps(index.extra),
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Record not found")
        return {"status": "ok"}

    @app.get("/v1/health", response_model=HealthResponse)
    async def health(hm: Hivemind = Depends(get_hivemind)):
        return hm.health()

    @app.get("/v1/spaces", dependencies=[Depends(check_auth)])
    async def spaces(hm: Hivemind = Depends(get_hivemind)):
        return hm.storage.list_spaces()

    @app.delete(
        "/v1/records/{record_id}",
        dependencies=[Depends(check_auth)],
    )
    async def delete_record(
        record_id: str, hm: Hivemind = Depends(get_hivemind)
    ):
        ok = hm.storage.delete_record(record_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Record not found")
        return {"status": "ok"}

    # ── Agent CRUD (sandbox mode only) ──

    if settings.sandbox_enabled:
        from .sandbox.models import AgentConfig, AgentCreateRequest

        @app.post("/v1/agents", dependencies=[Depends(check_auth)])
        async def register_agent(
            req: AgentCreateRequest,
            hm: Hivemind = Depends(get_hivemind),
        ):
            if not hm.agent_store:
                raise HTTPException(500, "Sandbox not enabled")
            agent_id = uuid4().hex[:12]
            config = AgentConfig(
                agent_id=agent_id,
                name=req.name,
                description=req.description,
                image=req.image,
                entrypoint=req.entrypoint,
                memory_mb=req.memory_mb,
                max_llm_calls=req.max_llm_calls,
                max_tokens=req.max_tokens,
                timeout_seconds=req.timeout_seconds,
            )
            hm.agent_store.create(config)

            # Extract source files from Docker image (non-fatal)
            file_count = 0
            try:
                from .sandbox.docker_runner import DockerRunner

                runner = DockerRunner(hm._sandbox_settings())
                files = await runner.extract_image_files_async(config.image)
                hm.agent_store.save_files(agent_id, files)
                file_count = len(files)
            except Exception as e:
                logger.warning(
                    "Failed to extract files from %s: %s", config.image, e
                )

            return {
                "agent_id": agent_id,
                "name": req.name,
                "files_extracted": file_count,
            }

        @app.get("/v1/agents", dependencies=[Depends(check_auth)])
        async def list_agents(hm: Hivemind = Depends(get_hivemind)):
            if not hm.agent_store:
                raise HTTPException(500, "Sandbox not enabled")
            return [a.model_dump() for a in hm.agent_store.list_agents()]

        @app.get("/v1/agents/{agent_id}", dependencies=[Depends(check_auth)])
        async def get_agent(
            agent_id: str, hm: Hivemind = Depends(get_hivemind)
        ):
            if not hm.agent_store:
                raise HTTPException(500, "Sandbox not enabled")
            agent = hm.agent_store.get(agent_id)
            if not agent:
                raise HTTPException(404, "Agent not found")
            return agent.model_dump()

        @app.delete("/v1/agents/{agent_id}", dependencies=[Depends(check_auth)])
        async def delete_agent(
            agent_id: str, hm: Hivemind = Depends(get_hivemind)
        ):
            if not hm.agent_store:
                raise HTTPException(500, "Sandbox not enabled")
            if not hm.agent_store.delete(agent_id):
                raise HTTPException(404, "Agent not found")
            return {"status": "ok"}

    return app


app = create_app()


def main():
    import uvicorn

    settings = Settings()
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()

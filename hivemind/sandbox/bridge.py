import asyncio
import logging
import socket
from typing import Callable

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request

from ..tools import Tool
from .budget import Budget
from .models import (
    BridgeLLMRequest,
    BridgeLLMResponse,
    BridgeToolRequest,
    BridgeToolResponse,
)

logger = logging.getLogger(__name__)


class BridgeServer:
    """Ephemeral HTTP server exposing LLM proxy and tools to a sandboxed agent.

    The bridge is the single network exit point for the agent container.
    It serves:
      - POST /llm/chat — passthrough proxy to OpenRouter (budget-enforced)
      - POST /tools/{name} — dispatch to scoped tool handlers
      - GET  /tools — list available tools with schemas
      - GET  /health — liveness check + budget info

    The agent controls model selection, messages, temperature, and all
    other LLM parameters. The bridge just forwards and enforces budget.

    All endpoints except /health require a session token.

    Lifecycle:
        bridge = BridgeServer(...)
        port = await bridge.start()   # starts listening
        # ... run agent container ...
        await bridge.stop()           # tears down
    """

    def __init__(
        self,
        session_token: str,
        tools: list[Tool],
        on_tool_call: Callable,
        llm_caller: Callable,
        budget: Budget,
        host: str = "127.0.0.1",
    ):
        self.session_token = session_token
        self.tools = {t.name: t for t in tools}
        self.on_tool_call = on_tool_call
        self.llm_caller = llm_caller
        self.budget = budget
        self.host = host
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self.port: int = 0

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Hivemind Sandbox Bridge", docs_url=None, redoc_url=None)
        bridge = self

        async def _check_token(request: Request):
            token = (
                request.headers.get("Authorization", "")
                .removeprefix("Bearer ")
                .strip()
            )
            if token != bridge.session_token:
                raise HTTPException(status_code=401, detail="Invalid session token")

        @app.get("/health")
        async def health():
            return {"status": "ok", "budget": bridge.budget.summary()}

        @app.get("/tools", dependencies=[Depends(_check_token)])
        async def list_tools():
            return [t.to_openai_def() for t in bridge.tools.values()]

        @app.post("/llm/chat", dependencies=[Depends(_check_token)])
        async def llm_chat(req: BridgeLLMRequest) -> BridgeLLMResponse:
            # Hard budget enforcement — reject the call entirely
            budget_error = bridge.budget.check()
            if budget_error:
                raise HTTPException(status_code=429, detail=budget_error)

            # Pure passthrough — agent controls model, messages, params
            kwargs: dict = {
                "messages": req.messages,
                "max_tokens": req.max_tokens,
            }
            if req.model is not None:
                kwargs["model"] = req.model
            if req.temperature is not None:
                kwargs["temperature"] = req.temperature
            if req.top_p is not None:
                kwargs["top_p"] = req.top_p

            result = await bridge.llm_caller(**kwargs)

            # Record usage
            usage = result.get("usage", {})
            bridge.budget.record(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            )

            return BridgeLLMResponse(
                content=result.get("content", ""),
                usage=usage,
            )

        @app.post("/tools/{tool_name}", dependencies=[Depends(_check_token)])
        async def call_tool(
            tool_name: str, req: BridgeToolRequest
        ) -> BridgeToolResponse:
            if tool_name not in bridge.tools:
                return BridgeToolResponse(
                    result="",
                    error=f"Unknown tool '{tool_name}'. "
                    f"Available: {', '.join(bridge.tools)}",
                )
            try:
                result = await bridge.on_tool_call(tool_name, req.arguments)
                return BridgeToolResponse(result=result)
            except Exception as e:
                logger.warning("Tool %s error: %s", tool_name, e)
                return BridgeToolResponse(result="", error=str(e))

        return app

    async def start(self) -> int:
        """Start the bridge server. Returns the port it's listening on."""
        app = self._build_app()

        # Find a free port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((self.host, 0))
        self.port = sock.getsockname()[1]
        sock.close()

        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())

        # Wait for server to be ready
        for _ in range(50):  # 5 seconds max
            await asyncio.sleep(0.1)
            if self._server.started:
                break

        logger.info("Bridge server started on %s:%d", self.host, self.port)
        return self.port

    async def stop(self):
        """Shut down the bridge server."""
        if self._server:
            self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        logger.info("Bridge server stopped")

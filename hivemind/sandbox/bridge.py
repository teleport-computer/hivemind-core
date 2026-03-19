import asyncio
import json
import logging
import secrets
import socket
import time
from typing import Callable
from uuid import uuid4

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request

from ..tools import Tool
from .budget import Budget
from .models import (
    AnthropicMessagesRequest,
    BridgeLLMRequest,
    BridgeLLMResponse,
    BridgeToolRequest,
    BridgeToolResponse,
    OpenAIChatRequest,
    SimulateRequest,
    SimulateResponse,
)
from .tape import Tape, hash_request

logger = logging.getLogger(__name__)


def _estimate_prompt_tokens(messages: list[dict]) -> int:
    """Conservative token estimate used for preflight budget checks."""
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        else:
            try:
                total_chars += len(json.dumps(content, ensure_ascii=False))
            except Exception:
                total_chars += len(str(content))
    return max(1, total_chars // 3)


def _anthropic_to_internal(req: AnthropicMessagesRequest) -> dict:
    """Translate an Anthropic /v1/messages request to internal (OpenAI) kwargs."""
    messages: list[dict] = []

    # System → prepend as system message
    if req.system is not None:
        if isinstance(req.system, str):
            system_text = req.system
        else:
            # Array of {type: "text", text: "..."} blocks
            system_text = "\n\n".join(
                block.get("text", "") for block in req.system if block.get("type") == "text"
            )
        if system_text:
            messages.append({"role": "system", "content": system_text})

    # Messages — handle content blocks and tool_result/tool_use
    for msg in req.messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        # Content is an array of blocks
        if not isinstance(content, list):
            messages.append({"role": role, "content": str(content)})
            continue

        # Separate block types
        text_parts: list[str] = []
        tool_uses: list[dict] = []
        tool_results: list[dict] = []

        for block in content:
            block_type = block.get("type", "")
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "tool_use":
                tool_uses.append(block)
            elif block_type == "tool_result":
                tool_results.append(block)

        if role == "assistant" and tool_uses:
            # Assistant message with tool_use → OpenAI tool_calls format
            assistant_msg: dict = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else ""}
            assistant_msg["tool_calls"] = [
                {
                    "id": tu.get("id", f"call_{i}"),
                    "type": "function",
                    "function": {
                        "name": tu.get("name", ""),
                        "arguments": json.dumps(tu.get("input", {})),
                    },
                }
                for i, tu in enumerate(tool_uses)
            ]
            messages.append(assistant_msg)
        elif tool_results:
            # User message with tool_result blocks → separate tool role messages
            for tr in tool_results:
                tr_content = tr.get("content", "")
                if isinstance(tr_content, list):
                    tr_content = "\n".join(
                        b.get("text", "") for b in tr_content if isinstance(b, dict) and b.get("type") == "text"
                    )
                messages.append({
                    "role": "tool",
                    "content": str(tr_content),
                    "tool_call_id": tr.get("tool_use_id", ""),
                })
        else:
            # Plain text blocks
            messages.append({"role": role, "content": "\n".join(text_parts) if text_parts else ""})

    kwargs: dict = {
        "messages": messages,
        "max_tokens": req.max_tokens,
        "model": req.model,
    }
    if req.temperature is not None:
        kwargs["temperature"] = req.temperature
    if req.top_p is not None:
        kwargs["top_p"] = req.top_p

    # Tools: Anthropic input_schema → OpenAI function parameters
    if req.tools:
        kwargs["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                },
            }
            for t in req.tools
        ]

    # Tool choice translation
    if req.tool_choice is not None:
        tc_type = req.tool_choice.get("type", "auto")
        if tc_type == "auto":
            kwargs["tool_choice"] = "auto"
        elif tc_type == "any":
            kwargs["tool_choice"] = "required"
        elif tc_type == "tool":
            kwargs["tool_choice"] = {
                "type": "function",
                "function": {"name": req.tool_choice.get("name", "")},
            }

    return kwargs


def _internal_to_anthropic(result: dict, model: str) -> dict:
    """Translate internal (OpenAI) result dict to Anthropic /v1/messages response."""
    content_blocks: list[dict] = []

    # Text content
    text = result.get("content", "")
    if text:
        content_blocks.append({"type": "text", "text": text})

    # Tool calls → tool_use blocks
    if "tool_calls" in result:
        for tc in result["tool_calls"]:
            fn = tc.get("function", {})
            try:
                input_dict = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                input_dict = {}
            content_blocks.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "input": input_dict,
            })

    # Stop reason mapping
    finish_reason = result.get("finish_reason", "stop")
    stop_reason_map = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
    }
    stop_reason = stop_reason_map.get(finish_reason, "end_turn")

    # Usage mapping
    usage = result.get("usage", {})
    anthropic_usage = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }

    return {
        "id": f"msg_{uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": anthropic_usage,
    }


class BridgeServer:
    """Ephemeral HTTP server exposing LLM proxy and tools to a sandboxed agent.

    The bridge is the single network exit point for the agent container.
    It serves:
      - POST /llm/chat — passthrough proxy to LLM (budget-enforced)
      - POST /tools/{name} — dispatch to scoped tool handlers
      - GET  /tools — list available tools with schemas
      - GET  /health — liveness check + budget info

    For scope agents (role="scope"), additional endpoints:
      - POST /sandbox/simulate — nested query agent run
      - GET  /sandbox/agents/{id}/files — list agent source files
      - GET  /sandbox/agents/{id}/files/{path} — read agent source file

    All endpoints except /health require a session token.
    """

    def __init__(
        self,
        session_token: str,
        tools: list[Tool],
        on_tool_call: Callable,
        llm_caller: Callable,
        budget: Budget,
        host: str = "127.0.0.1",
        role: str = "query",
        agent_store=None,
        run_query_fn: Callable | None = None,
        scope_query_agent_id: str | None = None,
        replay_tape: list[dict] | None = None,
    ):
        self.session_token = session_token
        self.tools = {t.name: t for t in tools}
        self.on_tool_call = on_tool_call
        self.llm_caller = llm_caller
        self.budget = budget
        self.host = host
        self.role = role
        self.agent_store = agent_store
        self.run_query_fn = run_query_fn
        self.scope_query_agent_id = scope_query_agent_id
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self._sock: socket.socket | None = None
        self.port: int = 0
        self._llm_lock = asyncio.Lock()

        # Tape recording/replay
        self._recording_tape = Tape()
        self._replay_tape: Tape | None = None
        if replay_tape:
            self._replay_tape = Tape.from_json(replay_tape)
            self._replay_tape.enable_replay()

    async def _handle_llm_call(self, kwargs: dict) -> dict:
        """Shared LLM call handler with tape replay/record and budget enforcement.

        Must be called under self._llm_lock.
        Returns the raw result dict from llm_caller (or tape cache).
        Raises HTTPException(429) if budget is exhausted.
        """
        req_hash = hash_request(kwargs)

        # Try tape replay first
        if self._replay_tape is not None:
            cached = self._replay_tape.try_replay(req_hash)
            if cached is not None:
                # Record to new tape but do NOT charge budget
                self._recording_tape.record(req_hash, kwargs, cached)
                return cached

        # Live call — enforce budget
        planned_prompt_tokens = _estimate_prompt_tokens(kwargs["messages"])
        planned_completion_tokens = kwargs.get("max_tokens", 4096)
        budget_error = self.budget.check(
            planned_prompt_tokens=planned_prompt_tokens,
            planned_completion_tokens=planned_completion_tokens,
        )
        if budget_error:
            raise HTTPException(status_code=429, detail=budget_error)

        result = await self.llm_caller(**kwargs)

        usage = result.get("usage", {})
        self.budget.record(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )

        # Record to tape
        self._recording_tape.record(req_hash, kwargs, result)

        return result

    def get_recorded_tape(self) -> list[dict]:
        """Return the tape recorded during this session, serialized for JSON transport."""
        return self._recording_tape.to_json()

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Hivemind Sandbox Bridge", docs_url=None, redoc_url=None)
        bridge = self

        async def _check_token(request: Request):
            # Accept either Authorization: Bearer <token> or x-api-key: <token>
            token = (
                request.headers.get("Authorization", "")
                .removeprefix("Bearer ")
                .strip()
            )
            if not token:
                token = request.headers.get("x-api-key", "").strip()
            if not token or not secrets.compare_digest(token, bridge.session_token):
                raise HTTPException(status_code=401, detail="Invalid session token")

        def _enforce_scope_query_agent(agent_id: str) -> None:
            allowed = bridge.scope_query_agent_id
            if not allowed:
                raise HTTPException(400, "No query agent available for this scope session")
            if agent_id != allowed:
                raise HTTPException(
                    403,
                    f"Scope session can only access query agent '{allowed}'",
                )

        @app.get("/health")
        async def health():
            return {"status": "ok", "budget": bridge.budget.summary()}

        @app.get("/tools", dependencies=[Depends(_check_token)])
        async def list_tools():
            return [t.to_openai_def() for t in bridge.tools.values()]

        @app.post("/llm/chat", dependencies=[Depends(_check_token)])
        async def llm_chat(req: BridgeLLMRequest) -> BridgeLLMResponse:
            async with bridge._llm_lock:
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

                result = await bridge._handle_llm_call(kwargs)

                return BridgeLLMResponse(
                    content=result.get("content", ""),
                    usage=result.get("usage", {}),
                )

        @app.post("/v1/chat/completions", dependencies=[Depends(_check_token)])
        async def openai_chat_completions(req: OpenAIChatRequest):
            """OpenAI-compatible chat completions endpoint.

            Standard OpenAI SDKs route here via OPENAI_BASE_URL env var.
            Same budget enforcement and tape recording as /llm/chat.
            """
            async with bridge._llm_lock:
                max_tokens = req.max_tokens or 4096
                kwargs: dict = {
                    "messages": req.messages,
                    "max_tokens": max_tokens,
                }
                if req.model is not None:
                    kwargs["model"] = req.model
                if req.temperature is not None:
                    kwargs["temperature"] = req.temperature
                if req.top_p is not None:
                    kwargs["top_p"] = req.top_p
                if req.tools is not None:
                    kwargs["tools"] = req.tools
                if req.tool_choice is not None:
                    kwargs["tool_choice"] = req.tool_choice

                result = await bridge._handle_llm_call(kwargs)

                # Build OpenAI-format message
                usage = result.get("usage", {})
                message: dict = {"role": "assistant", "content": result.get("content", "")}
                if "tool_calls" in result:
                    message["tool_calls"] = result["tool_calls"]

                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)

                return {
                    "id": f"chatcmpl-{uuid4().hex[:12]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": req.model or "default",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": result.get("finish_reason") or "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                }

        @app.post("/v1/messages/count_tokens", dependencies=[Depends(_check_token)])
        async def anthropic_count_tokens(req: AnthropicMessagesRequest):
            """Anthropic-compatible token counting endpoint.

            Returns an estimated input token count. Used by Claude Code CLI
            for context window management. No budget charge.
            """
            kwargs = _anthropic_to_internal(req)
            input_tokens = _estimate_prompt_tokens(kwargs["messages"])
            # Add rough estimate for tool definitions
            if "tools" in kwargs:
                input_tokens += _estimate_prompt_tokens(
                    [{"content": json.dumps(kwargs["tools"])}]
                )
            return {"input_tokens": input_tokens}

        @app.post("/v1/messages", dependencies=[Depends(_check_token)])
        async def anthropic_messages(req: AnthropicMessagesRequest):
            """Anthropic-compatible messages endpoint.

            Anthropic SDKs route here via ANTHROPIC_BASE_URL env var.
            Translates to internal format, same budget/tape enforcement.
            """
            if req.stream:
                raise HTTPException(
                    status_code=400,
                    detail="Streaming is not supported by the bridge",
                )
            async with bridge._llm_lock:
                kwargs = _anthropic_to_internal(req)
                result = await bridge._handle_llm_call(kwargs)
                return _internal_to_anthropic(result, req.model)

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

        # ── Scope-agent-only endpoints ──

        if bridge.role == "scope":

            @app.post(
                "/sandbox/simulate",
                dependencies=[Depends(_check_token)],
                response_model=SimulateResponse,
            )
            async def simulate(req: SimulateRequest) -> SimulateResponse:
                if not bridge.run_query_fn:
                    raise HTTPException(
                        500, "Simulation not available (no run_query_fn)"
                    )
                _enforce_scope_query_agent(req.query_agent_id)
                # Pass full remaining budget to simulation
                remaining = bridge.budget.remaining()
                remaining_calls = remaining["calls"]
                remaining_tokens = remaining["tokens"]
                if remaining_calls < 1 or remaining_tokens < 1:
                    raise HTTPException(
                        429, "Insufficient budget for simulation"
                    )

                try:
                    sim_result = await bridge.run_query_fn(
                        query_agent_id=req.query_agent_id,
                        prompt=req.prompt,
                        scope_fn_source=req.scope_fn_source,
                        max_calls=remaining_calls,
                        max_tokens=remaining_tokens,
                        replay_tape=req.replay_tape,
                    )

                    usage = None
                    tape = None
                    if isinstance(sim_result, tuple) and len(sim_result) == 3:
                        output, usage, tape = sim_result
                    elif isinstance(sim_result, tuple) and len(sim_result) == 2:
                        output, usage = sim_result
                    else:
                        output = sim_result

                    if isinstance(usage, dict):
                        bridge.budget.record(
                            calls=int(usage.get("calls", 0) or 0),
                            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                        )
                    else:
                        bridge.budget.record(
                            calls=remaining_calls,
                            prompt_tokens=remaining_tokens // 2,
                            completion_tokens=remaining_tokens // 2,
                        )

                    return SimulateResponse(
                        output=output,
                        tape=tape,
                    )
                except Exception as e:
                    logger.warning("Simulation failed: %s", e)
                    raise HTTPException(500, f"Simulation failed: {e}")

            @app.get(
                "/sandbox/agents/{agent_id}/files",
                dependencies=[Depends(_check_token)],
            )
            async def list_agent_files(agent_id: str):
                if not bridge.agent_store:
                    raise HTTPException(500, "Agent store not available")
                _enforce_scope_query_agent(agent_id)
                files = await asyncio.to_thread(
                    bridge.agent_store.list_file_paths, agent_id
                )
                return {"files": files}

            @app.get(
                "/sandbox/agents/{agent_id}/files/{file_path:path}",
                dependencies=[Depends(_check_token)],
            )
            async def read_agent_file(agent_id: str, file_path: str):
                if not bridge.agent_store:
                    raise HTTPException(500, "Agent store not available")
                _enforce_scope_query_agent(agent_id)
                content = await asyncio.to_thread(
                    bridge.agent_store.read_file, agent_id, file_path
                )
                if content is None:
                    raise HTTPException(404, "File not found")
                return {"content": content}

        return app

    async def start(self) -> int:
        """Start the bridge server. Returns the port it's listening on."""
        app = self._build_app()

        config = uvicorn.Config(
            app,
            host=self.host,
            port=0,
            log_level="warning",
        )
        self._sock = config.bind_socket()
        self.port = self._sock.getsockname()[1]
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[self._sock]))

        for _ in range(50):  # 5 seconds max
            await asyncio.sleep(0.1)
            if self._server.started:
                logger.info("Bridge server started on %s:%d", self.host, self.port)
                return self.port
            if self._task and self._task.done():
                try:
                    self._task.result()
                except Exception as e:
                    await self.stop()
                    raise RuntimeError(
                        f"Bridge server failed to start on {self.host}:{self.port}"
                    ) from e
                break

        await self.stop()
        raise RuntimeError(
            f"Bridge server did not start within timeout on {self.host}:{self.port}"
        )

    async def stop(self):
        """Shut down the bridge server."""
        if self._server:
            self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception as e:
                logger.debug("Bridge server task exited with error during shutdown: %s", e)
        if self._sock:
            try:
                self._sock.close()
            except Exception as e:
                logger.debug("Bridge server socket close failed: %s", e)
            finally:
                self._sock = None
        logger.info("Bridge server stopped")

import asyncio
import json
import logging
import os
import secrets
import socket
import time
from typing import Callable
from uuid import uuid4

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request

from ..tools import Tool
from .budget import Budget
from .models import (
    AnthropicMessagesRequest,
    BridgeLLMRequest,
    BridgeLLMResponse,
    BridgeArtifactUploadRequest,
    BridgeArtifactUploadResponse,
    BridgeToolRequest,
    BridgeToolResponse,
    OpenAIChatRequest,
    ScopeTestResult,
    SimulateBatchItem,
    SimulateBatchRequest,
    SimulateBatchResponse,
    SimulateRequest,
    SimulateResponse,
    VerifyScopeRequest,
    VerifyScopeResponse,
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
        artifact_store=None,
        artifact_retention_seconds: int = 86400,
        run_id: str | None = None,
        run_store=None,
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
        self.artifact_store = artifact_store
        self.artifact_retention_seconds = artifact_retention_seconds
        self.run_id = run_id
        self.run_store = run_store
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

        # Live call — enforce budget.
        #
        # Clamp the caller's requested max_tokens to what's actually left in
        # the budget. Clients like Claude Code CLI send a large nominal cap
        # (~40k) that almost never reflects actual completion size (<1k for
        # most turns); rejecting on the nominal cap wastes otherwise-fine
        # calls. Real usage is still recorded post-hoc, so the budget
        # invariant holds.
        planned_prompt_tokens = _estimate_prompt_tokens(kwargs["messages"])
        remaining_tokens = self.budget.remaining()["tokens"]
        room_for_completion = max(1, remaining_tokens - planned_prompt_tokens)
        requested_completion = kwargs.get("max_tokens", 4096)
        if requested_completion > room_for_completion:
            kwargs["max_tokens"] = room_for_completion
        planned_completion_tokens = kwargs["max_tokens"]
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
            Streaming requests are handled by making a non-streaming call
            and wrapping the result in SSE format.
            """
            async with bridge._llm_lock:
                kwargs = _anthropic_to_internal(req)
                result = await bridge._handle_llm_call(kwargs)
                anthropic_resp = _internal_to_anthropic(result, req.model)

            if not req.stream:
                return anthropic_resp

            # Wrap non-streaming result as SSE events for streaming clients
            from starlette.responses import StreamingResponse

            async def _sse_events():
                msg_id = anthropic_resp["id"]
                model = anthropic_resp["model"]
                usage = anthropic_resp.get("usage", {})
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)

                # message_start
                yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': model, 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': input_tokens, 'output_tokens': 0}}})}\n\n"

                # content blocks
                for idx, block in enumerate(anthropic_resp.get("content", [])):
                    if block["type"] == "text":
                        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                        yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': idx, 'delta': {'type': 'text_delta', 'text': block['text']}})}\n\n"
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': idx})}\n\n"
                    elif block["type"] == "tool_use":
                        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': idx, 'content_block': {'type': 'tool_use', 'id': block['id'], 'name': block['name'], 'input': {}}})}\n\n"
                        yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': idx, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}})}\n\n"
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': idx})}\n\n"

                # message_delta + message_stop
                yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': anthropic_resp.get('stop_reason', 'end_turn'), 'stop_sequence': None}, 'usage': {'output_tokens': output_tokens}})}\n\n"
                yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

            return StreamingResponse(
                _sse_events(),
                media_type="text/event-stream",
            )

        @app.post("/tools/{tool_name}", dependencies=[Depends(_check_token)])
        async def call_tool(
            tool_name: str, req: BridgeToolRequest
        ) -> BridgeToolResponse:
            # Telemetry: per-tool usage count + approx arg-size for this
            # scope/query/mediator invocation. Grep logs for TOOL_CALL
            # after a bench run to see how scope actually exercises its
            # capabilities.
            import json as _json
            args_size = len(_json.dumps(req.arguments, default=str))
            logger.info(
                "TOOL_CALL role=%s tool=%s args_size=%d",
                bridge.role,
                tool_name,
                args_size,
            )
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
            async def simulate(
                req: SimulateRequest,
                x_simulate_caller: str = Header(default="unknown"),
            ) -> SimulateResponse:
                if not bridge.run_query_fn:
                    raise HTTPException(
                        500, "Simulation not available (no run_query_fn)"
                    )
                _enforce_scope_query_agent(req.query_agent_id)
                # Telemetry: log WHICH surface (MCP via bridge_simulate vs
                # Bash via play.py) scope actually used for this sim call.
                logger.info(
                    "SCOPE_SIM caller=%s scope_fn_len=%d prompt_len=%d",
                    x_simulate_caller,
                    len(req.scope_fn_source or ""),
                    len(req.prompt or ""),
                )
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
                    import traceback, sys
                    print(f"SIMULATION_ERROR: {type(e).__name__}: {e}", flush=True)
                    traceback.print_exc()
                    sys.stdout.flush()
                    raise HTTPException(500, f"Simulation failed: {e}")

            @app.post(
                "/sandbox/simulate_batch",
                dependencies=[Depends(_check_token)],
                response_model=SimulateBatchResponse,
            )
            async def simulate_batch(
                req: SimulateBatchRequest,
                x_simulate_caller: str = Header(default="unknown"),
            ) -> SimulateBatchResponse:
                if not bridge.run_query_fn:
                    raise HTTPException(
                        500, "Simulation not available (no run_query_fn)"
                    )
                _enforce_scope_query_agent(req.query_agent_id)
                n = len(req.candidates)
                logger.info(
                    "SCOPE_SIM_BATCH caller=%s n=%d prompt_len=%d",
                    x_simulate_caller,
                    n,
                    len(req.prompt or ""),
                )
                remaining = bridge.budget.remaining()
                remaining_calls = remaining["calls"]
                remaining_tokens = remaining["tokens"]
                if remaining_calls < n or remaining_tokens < n:
                    raise HTTPException(
                        429, "Insufficient budget for batch simulation"
                    )
                per_calls = max(1, remaining_calls // n)
                per_tokens = max(1, remaining_tokens // n)

                async def _run_one(idx: int, scope_fn_source: str):
                    try:
                        sim_result = await bridge.run_query_fn(
                            query_agent_id=req.query_agent_id,
                            prompt=req.prompt,
                            scope_fn_source=scope_fn_source,
                            max_calls=per_calls,
                            max_tokens=per_tokens,
                            replay_tape=req.replay_tape,
                        )
                        usage: dict | None = None
                        if isinstance(sim_result, tuple) and len(sim_result) == 3:
                            output, usage, _tape = sim_result
                        elif isinstance(sim_result, tuple) and len(sim_result) == 2:
                            output, usage = sim_result
                        else:
                            output = sim_result
                        return (idx, output, usage, None)
                    except Exception as e:
                        return (idx, "", None, f"{type(e).__name__}: {e}")

                gathered = await asyncio.gather(
                    *[_run_one(i, c) for i, c in enumerate(req.candidates)]
                )

                total_calls = 0
                total_prompt = 0
                total_completion = 0
                items: list[SimulateBatchItem] = []
                for idx, output, usage, error in gathered:
                    if isinstance(usage, dict):
                        total_calls += int(usage.get("calls", 0) or 0)
                        total_prompt += int(usage.get("prompt_tokens", 0) or 0)
                        total_completion += int(usage.get("completion_tokens", 0) or 0)
                    items.append(
                        SimulateBatchItem(
                            idx=idx,
                            output=output or "",
                            error=error,
                        )
                    )

                if total_calls == 0 and total_prompt == 0 and total_completion == 0:
                    # Fallback: no usage metadata, charge worst case per candidate
                    bridge.budget.record(
                        calls=min(remaining_calls, per_calls * n),
                        prompt_tokens=min(remaining_tokens // 2, (per_tokens * n) // 2),
                        completion_tokens=min(remaining_tokens // 2, (per_tokens * n) // 2),
                    )
                else:
                    bridge.budget.record(
                        calls=total_calls,
                        prompt_tokens=total_prompt,
                        completion_tokens=total_completion,
                    )

                items.sort(key=lambda it: it.idx)
                return SimulateBatchResponse(results=items)

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

            @app.post(
                "/sandbox/verify_scope_fn",
                dependencies=[Depends(_check_token)],
                response_model=VerifyScopeResponse,
            )
            async def verify_scope_fn_endpoint(
                req: VerifyScopeRequest,
            ) -> VerifyScopeResponse:
                """Compile the provided scope_fn source and run it against the
                given synthetic test cases.

                This is the canonical correctness check: uses the SAME
                compile_scope_fn + apply_scope_fn the real pipeline uses,
                so if verify says ok, the real pipeline will accept it.
                """
                from ..scope import apply_scope_fn, compile_scope_fn

                try:
                    scope_fn = await asyncio.to_thread(
                        compile_scope_fn, req.source
                    )
                except ValueError as e:
                    return VerifyScopeResponse(
                        compiles=False,
                        compile_error=str(e),
                        all_tests_passed=False,
                        results=[],
                    )

                results: list[ScopeTestResult] = []
                all_passed = True
                for tc in req.tests:
                    try:
                        outcome = await asyncio.to_thread(
                            apply_scope_fn,
                            scope_fn,
                            tc.sql,
                            list(tc.params),
                            list(tc.rows),
                            _source=req.source,
                        )
                    except Exception as e:
                        outcome = {"allow": False, "error": f"apply_scope_fn raised: {e}"}

                    allow = bool(outcome.get("allow", False))
                    error = outcome.get("error")
                    rows_returned = (
                        len(outcome.get("rows", []))
                        if allow and isinstance(outcome.get("rows"), list)
                        else 0
                    )
                    passed: bool | None = None
                    if tc.expect_allow is not None:
                        passed = allow == tc.expect_allow
                        if not passed:
                            all_passed = False
                    results.append(
                        ScopeTestResult(
                            label=tc.label,
                            sql=tc.sql[:200],
                            allow=allow,
                            error=str(error) if error else None,
                            rows_returned=rows_returned,
                            expected_allow=tc.expect_allow,
                            passed=passed,
                        )
                    )

                return VerifyScopeResponse(
                    compiles=True,
                    compile_error=None,
                    all_tests_passed=all_passed,
                    results=results,
                )

        # ── Artifact upload endpoint (query agents with run tracking) ──

        if bridge.run_id and bridge.artifact_store:

            @app.post(
                "/sandbox/artifact-upload",
                dependencies=[Depends(_check_token)],
                response_model=BridgeArtifactUploadResponse,
            )
            async def artifact_upload(
                req: BridgeArtifactUploadRequest,
            ) -> BridgeArtifactUploadResponse:
                import base64

                try:
                    data = base64.b64decode(req.content_base64)
                except Exception:
                    raise HTTPException(400, "Invalid base64 content")

                # Write directly to the Postgres-backed ArtifactStore.
                # No S3, no bucket credentials, no presigned URLs.
                result = await asyncio.to_thread(
                    bridge.artifact_store.put,
                    bridge.run_id,
                    req.filename,
                    data,
                    req.content_type,
                )
                path = (
                    f"/v1/query/runs/{bridge.run_id}/artifacts/{req.filename}"
                )
                return BridgeArtifactUploadResponse(
                    path=path,
                    size_bytes=result["size_bytes"],
                    retention_seconds=bridge.artifact_retention_seconds,
                )

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
        # Persist tape if HIVEMIND_TRACE_DIR is set. Zero-cost when unset.
        trace_dir = os.environ.get("HIVEMIND_TRACE_DIR")
        if trace_dir:
            try:
                from pathlib import Path
                import time as _time
                import json as _json
                Path(trace_dir).mkdir(parents=True, exist_ok=True)
                ts = _time.strftime("%Y%m%dT%H%M%S")
                token8 = (self.session_token or "anon")[:8]
                role = getattr(self, "role", "unknown") or "unknown"
                fname = f"{ts}_{role}_{token8}.jsonl"
                path = Path(trace_dir) / fname
                tape = self._recording_tape.to_json()
                with path.open("w") as f:
                    for entry in tape:
                        f.write(_json.dumps(entry) + "\n")
                logger.info("Tape persisted to %s (%d entries)", path, len(tape))
            except Exception as e:
                logger.warning("Tape persistence failed: %s", e)
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

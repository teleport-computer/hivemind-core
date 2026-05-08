import json
from unittest.mock import patch

import pytest
import pytest_asyncio
import httpx

from hivemind.sandbox.bridge import BridgeServer
from hivemind.sandbox.budget import Budget
from hivemind.sandbox.tape import hash_request
from hivemind.tools import Tool


def _make_tools():
    """Create mock tools for testing."""

    def execute_sql(sql: str, params: list | None = None) -> str:
        return '[{"id": 1, "name": "test"}]'

    def get_schema() -> str:
        return '[{"table_name": "users", "column_name": "id", "data_type": "integer"}]'

    return [
        Tool(
            name="execute_sql",
            description="Execute SQL",
            parameters={
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            },
            handler=execute_sql,
        ),
        Tool(
            name="get_schema",
            description="Get schema",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=get_schema,
        ),
    ]


async def _mock_llm_caller(messages, max_tokens, model=None, temperature=None, top_p=None, **kwargs):
    result = {
        "content": f"LLM response. model={model}, temp={temperature}",
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        "finish_reason": "stop",
    }
    if kwargs.get("tools"):
        result["tool_calls"] = [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"location":"SF"}'},
            }
        ]
        result["finish_reason"] = "tool_calls"
    return result


async def _mock_on_tool_call(name, args):
    tools = {t.name: t.handler for t in _make_tools()}
    if name not in tools:
        return f"Error: unknown tool '{name}'"
    return tools[name](**args)


@pytest_asyncio.fixture
async def bridge():
    budget = Budget(max_calls=10, max_tokens=100_000)
    server = BridgeServer(
        session_token="test-token-123",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=_mock_llm_caller,
        budget=budget,
        host="127.0.0.1",
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    yield server, client, budget

    await client.aclose()
    await server.stop()


@pytest.mark.asyncio
async def test_health(bridge):
    server, client, budget = bridge
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "budget" in data


@pytest.mark.asyncio
async def test_auth_required(bridge):
    server, client, budget = bridge
    resp = await client.get("/tools")
    assert resp.status_code == 401

    resp = await client.get(
        "/tools", headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp.status_code == 401

    resp = await client.get(
        "/tools", headers={"Authorization": "Bearer test-token-123"}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_list_tools(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.get("/tools", headers=headers)
    assert resp.status_code == 200
    tools = resp.json()
    assert len(tools) == 2
    names = {t["function"]["name"] for t in tools}
    assert names == {"execute_sql", "get_schema"}


@pytest.mark.asyncio
async def test_llm_chat(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/llm/chat",
        headers=headers,
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "LLM response" in data["content"]
    assert budget.summary()["calls"] == 1


@pytest.mark.asyncio
async def test_llm_chat_model_override(bridge):
    """Model is NOT forwarded from /llm/chat — the bridge uses its configured model.
    Temperature IS forwarded."""
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/llm/chat",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "anthropic/claude-haiku-4.5",
            "temperature": 0.7,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "0.7" in data["content"]


@pytest.mark.asyncio
async def test_llm_chat_rejects_excessive_max_tokens(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/llm/chat",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 20000,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_tool_call(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/tools/execute_sql",
        headers=headers,
        json={"arguments": {"sql": "SELECT 1"}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "test" in data["result"]
    assert data["error"] is None


@pytest.mark.asyncio
async def test_tool_call_unknown(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/tools/nonexistent_tool",
        headers=headers,
        json={"arguments": {}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"] is not None
    assert "Unknown tool" in data["error"]


@pytest.mark.asyncio
async def test_budget_enforcement(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}

    budget.max_calls = 2
    budget.record(prompt_tokens=10, completion_tokens=10)
    budget.record(prompt_tokens=10, completion_tokens=10)

    resp = await client.post(
        "/llm/chat",
        headers=headers,
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert resp.status_code == 429
    data = resp.json()
    assert "Budget exhausted" in data["detail"]
    assert budget.summary()["calls"] == 2


@pytest.mark.asyncio
async def test_budget_enforcement_uses_prompt_size_estimate(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    budget.max_tokens = 50

    resp = await client.post(
        "/llm/chat",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "x" * 1000}],
            "max_tokens": 1,
        },
    )
    assert resp.status_code == 429
    data = resp.json()
    assert "Budget exhausted" in data["detail"]


@pytest.mark.asyncio
async def test_scope_endpoints_not_available_for_query_role():
    """Query-role bridge should NOT have /sandbox/simulate endpoint."""
    budget = Budget(max_calls=10, max_tokens=100_000)
    server = BridgeServer(
        session_token="tok",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=_mock_llm_caller,
        budget=budget,
        host="127.0.0.1",
        role="query",
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    try:
        headers = {"Authorization": "Bearer tok"}
        resp = await client.post(
            "/sandbox/simulate",
            headers=headers,
            json={"query_agent_id": "q1", "prompt": "test", "scope_fn_source": "def scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}"},
        )
        # Should get 404 (endpoint not registered) or 405
        assert resp.status_code in (404, 405, 422)
    finally:
        await client.aclose()
        await server.stop()


# ── OpenAI-compatible /v1/chat/completions tests ──


@pytest.mark.asyncio
async def test_openai_chat_completions_basic(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert resp.status_code == 200
    data = resp.json()

    # OpenAI response format
    assert data["object"] == "chat.completion"
    assert data["id"].startswith("chatcmpl-")
    assert len(data["choices"]) == 1
    choice = data["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert "LLM response" in choice["message"]["content"]
    assert choice["finish_reason"] == "stop"

    # Usage
    assert data["usage"]["prompt_tokens"] == 100
    assert data["usage"]["completion_tokens"] == 50
    assert data["usage"]["total_tokens"] == 150

    # Budget was charged
    assert budget.summary()["calls"] == 1


@pytest.mark.asyncio
async def test_openai_chat_completions_budget_enforcement(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}

    budget.max_calls = 2
    budget.record(prompt_tokens=10, completion_tokens=10)
    budget.record(prompt_tokens=10, completion_tokens=10)

    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert resp.status_code == 429
    assert "Budget exhausted" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_openai_chat_completions_with_tools(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "What's the weather?"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "parameters": {"type": "object", "properties": {"location": {"type": "string"}}},
                    },
                }
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    choice = data["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert "tool_calls" in choice["message"]
    tc = choice["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "get_weather"
    assert tc["id"] == "call_abc123"


@pytest.mark.asyncio
async def test_openai_chat_completions_forwards_extra_body():
    captured = {}

    async def llm_caller(**kwargs):
        captured.update(kwargs)
        return {
            "content": "OK",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "finish_reason": "stop",
        }

    budget = Budget(max_calls=10, max_tokens=100_000)
    server = BridgeServer(
        session_token="tok",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=llm_caller,
        budget=budget,
        host="127.0.0.1",
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    try:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer tok"},
            json={
                "messages": [{"role": "user", "content": "Hello"}],
                "reasoning": {"effort": "none", "exclude": True},
            },
        )
        assert resp.status_code == 200
        assert captured["extra_body"] == {
            "reasoning": {"effort": "none", "exclude": True}
        }
    finally:
        await client.aclose()
        await server.stop()


def _parse_sse_payloads(text: str) -> list[dict | str]:
    payloads: list[dict | str] = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        raw = line.removeprefix("data: ")
        if raw == "[DONE]":
            payloads.append(raw)
        else:
            payloads.append(json.loads(raw))
    return payloads


@pytest.mark.asyncio
async def test_openai_chat_completions_streams_text(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    payloads = _parse_sse_payloads(resp.text)
    deltas = [
        p["choices"][0]["delta"]
        for p in payloads
        if isinstance(p, dict) and p.get("choices")
    ]
    assert deltas[0] == {"role": "assistant"}
    assert any(d.get("content", "").startswith("LLM response.") for d in deltas)
    assert any(
        isinstance(p, dict)
        and p.get("choices") == []
        and p.get("usage", {}).get("total_tokens") == 150
        for p in payloads
    )
    assert payloads[-1] == "[DONE]"


@pytest.mark.asyncio
async def test_openai_chat_completions_streams_tool_calls(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "What's the weather?"}],
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"location": {"type": "string"}},
                        },
                    },
                }
            ],
        },
    )
    assert resp.status_code == 200

    payloads = _parse_sse_payloads(resp.text)
    chunks = [p for p in payloads if isinstance(p, dict) and p.get("choices")]
    tool_delta = next(
        c["choices"][0]["delta"]
        for c in chunks
        if c["choices"][0]["delta"].get("tool_calls")
    )
    tool_call = tool_delta["tool_calls"][0]
    assert tool_call["index"] == 0
    assert tool_call["id"] == "call_abc123"
    assert tool_call["function"]["name"] == "get_weather"
    assert tool_call["function"]["arguments"] == '{"location":"SF"}'
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_openai_chat_completions_preserves_reasoning_fields():
    async def reasoning_llm_caller(**kwargs):
        return {
            "content": "",
            "reasoning": "hidden reasoning",
            "reasoning_content": "hidden reasoning",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "finish_reason": "stop",
        }

    budget = Budget(max_calls=10, max_tokens=100_000)
    server = BridgeServer(
        session_token="test-token-123",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=reasoning_llm_caller,
        budget=budget,
        host="127.0.0.1",
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")
    try:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-token-123"},
            json={"messages": [{"role": "user", "content": "Hello"}]},
        )
        assert resp.status_code == 200
        message = resp.json()["choices"][0]["message"]
        assert message["reasoning"] == "hidden reasoning"
        assert message["reasoning_content"] == "hidden reasoning"
    finally:
        await client.aclose()
        await server.stop()


@pytest.mark.asyncio
async def test_openai_chat_completions_auth_required(bridge):
    server, client, budget = bridge
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bridge_start_raises_if_server_fails_to_boot():
    class _FailingServer:
        def __init__(self, config):
            self.started = False
            self.should_exit = False

        async def serve(self, sockets=None):
            raise RuntimeError("boom")

    budget = Budget(max_calls=10, max_tokens=100_000)
    server = BridgeServer(
        session_token="tok",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=_mock_llm_caller,
        budget=budget,
        host="127.0.0.1",
    )

    with patch("hivemind.sandbox.bridge.uvicorn.Server", _FailingServer):
        with pytest.raises(RuntimeError, match="failed to start"):
            await server.start()


# ── Tape recording/replay tests ──


@pytest.mark.asyncio
async def test_bridge_records_tape(bridge):
    """LLM calls should produce tape entries."""
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}

    await client.post(
        "/llm/chat",
        headers=headers,
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    await client.post(
        "/llm/chat",
        headers=headers,
        json={"messages": [{"role": "user", "content": "World"}]},
    )

    tape = server.get_recorded_tape()
    assert len(tape) == 2
    assert tape[0]["response"]["content"].startswith("LLM response")
    assert tape[1]["response"]["content"].startswith("LLM response")
    assert tape[0]["request_hash"] != tape[1]["request_hash"]


@pytest.mark.asyncio
async def test_bridge_replays_from_tape():
    """When replay tape matches, return cached response without calling llm_caller."""
    call_count = 0

    async def counting_llm_caller(messages, max_tokens, **kwargs):
        nonlocal call_count
        call_count += 1
        return {
            "content": "live response",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "finish_reason": "stop",
        }

    # Build a replay tape with a known request
    req_kwargs = {"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 4096}
    replay_tape = [
        {
            "request_hash": hash_request(req_kwargs),
            "response": {
                "content": "cached response",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "finish_reason": "stop",
            },
            "request_kwargs": req_kwargs,
        }
    ]

    budget = Budget(max_calls=10, max_tokens=100_000)
    server = BridgeServer(
        session_token="test-token-123",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=counting_llm_caller,
        budget=budget,
        host="127.0.0.1",
        replay_tape=replay_tape,
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    try:
        headers = {"Authorization": "Bearer test-token-123"}
        resp = await client.post(
            "/llm/chat",
            headers=headers,
            json={"messages": [{"role": "user", "content": "Hello"}]},
        )
        assert resp.status_code == 200
        assert resp.json()["content"] == "cached response"
        assert call_count == 0  # llm_caller was NOT called
    finally:
        await client.aclose()
        await server.stop()


@pytest.mark.asyncio
async def test_bridge_replay_divergence():
    """When request hash doesn't match tape, switch to live calls."""
    call_count = 0

    async def counting_llm_caller(messages, max_tokens, **kwargs):
        nonlocal call_count
        call_count += 1
        return {
            "content": "live response",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "finish_reason": "stop",
        }

    # Tape has a different message than what we'll send
    replay_tape = [
        {
            "request_hash": hash_request(
                {"messages": [{"role": "user", "content": "Different"}], "max_tokens": 4096}
            ),
            "response": {"content": "cached", "usage": {}, "finish_reason": "stop"},
            "request_kwargs": {},
        }
    ]

    budget = Budget(max_calls=10, max_tokens=100_000)
    server = BridgeServer(
        session_token="tok",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=counting_llm_caller,
        budget=budget,
        host="127.0.0.1",
        replay_tape=replay_tape,
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    try:
        headers = {"Authorization": "Bearer tok"}
        resp = await client.post(
            "/llm/chat",
            headers=headers,
            json={"messages": [{"role": "user", "content": "Mismatch"}]},
        )
        assert resp.status_code == 200
        assert resp.json()["content"] == "live response"
        assert call_count == 1  # llm_caller WAS called
    finally:
        await client.aclose()
        await server.stop()


@pytest.mark.asyncio
async def test_bridge_replay_no_budget_charge():
    """Cached responses should NOT charge the budget."""
    req_kwargs = {"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 4096}
    replay_tape = [
        {
            "request_hash": hash_request(req_kwargs),
            "response": {
                "content": "cached",
                "usage": {"prompt_tokens": 999, "completion_tokens": 999},
                "finish_reason": "stop",
            },
            "request_kwargs": req_kwargs,
        }
    ]

    budget = Budget(max_calls=10, max_tokens=100_000)
    server = BridgeServer(
        session_token="tok",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=_mock_llm_caller,
        budget=budget,
        host="127.0.0.1",
        replay_tape=replay_tape,
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    try:
        headers = {"Authorization": "Bearer tok"}
        await client.post(
            "/llm/chat",
            headers=headers,
            json={"messages": [{"role": "user", "content": "Hello"}]},
        )
        summary = budget.summary()
        assert summary["calls"] == 0
        assert summary["total_tokens"] == 0
    finally:
        await client.aclose()
        await server.stop()


@pytest.mark.asyncio
async def test_bridge_replay_openai_endpoint():
    """Tape replay should work on /v1/chat/completions too."""
    req_kwargs = {"messages": [{"role": "user", "content": "Hi"}], "max_tokens": 4096}
    replay_tape = [
        {
            "request_hash": hash_request(req_kwargs),
            "response": {
                "content": "openai cached",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "finish_reason": "stop",
            },
            "request_kwargs": req_kwargs,
        }
    ]

    call_count = 0

    async def counting_caller(messages, max_tokens, **kwargs):
        nonlocal call_count
        call_count += 1
        return {"content": "live", "usage": {}, "finish_reason": "stop"}

    budget = Budget(max_calls=10, max_tokens=100_000)
    server = BridgeServer(
        session_token="tok",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=counting_caller,
        budget=budget,
        host="127.0.0.1",
        replay_tape=replay_tape,
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    try:
        headers = {"Authorization": "Bearer tok"}
        resp = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["choices"][0]["message"]["content"] == "openai cached"
        assert call_count == 0
    finally:
        await client.aclose()
        await server.stop()


@pytest.mark.asyncio
async def test_bridge_tape_records_both_replayed_and_live():
    """The recording tape should contain both replayed and live entries."""
    req_kwargs_1 = {"messages": [{"role": "user", "content": "First"}], "max_tokens": 4096}
    replay_tape = [
        {
            "request_hash": hash_request(req_kwargs_1),
            "response": {
                "content": "cached first",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "finish_reason": "stop",
            },
            "request_kwargs": req_kwargs_1,
        }
    ]

    budget = Budget(max_calls=10, max_tokens=100_000)
    server = BridgeServer(
        session_token="tok",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=_mock_llm_caller,
        budget=budget,
        host="127.0.0.1",
        replay_tape=replay_tape,
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    try:
        headers = {"Authorization": "Bearer tok"}
        # First call: should be replayed
        await client.post(
            "/llm/chat",
            headers=headers,
            json={"messages": [{"role": "user", "content": "First"}]},
        )
        # Second call: different message, should be live
        await client.post(
            "/llm/chat",
            headers=headers,
            json={"messages": [{"role": "user", "content": "Second"}]},
        )

        tape = server.get_recorded_tape()
        assert len(tape) == 2
        assert tape[0]["response"]["content"] == "cached first"
        assert tape[1]["response"]["content"].startswith("LLM response")
    finally:
        await client.aclose()
        await server.stop()

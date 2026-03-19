"""Tests for the Anthropic /v1/messages bridge endpoint and format translation."""

import json
import pytest
import pytest_asyncio
import httpx

from hivemind.sandbox.bridge import (
    BridgeServer,
    _anthropic_to_internal,
    _internal_to_anthropic,
)
from hivemind.sandbox.budget import Budget
from hivemind.sandbox.models import AnthropicMessagesRequest


# ── Translation: Anthropic → Internal ──


def _make_req(**overrides) -> AnthropicMessagesRequest:
    defaults = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Hello"}],
    }
    defaults.update(overrides)
    return AnthropicMessagesRequest(**defaults)


class TestAnthropicToInternal:
    def test_simple_text(self):
        req = _make_req(messages=[{"role": "user", "content": "Hello"}])
        kwargs = _anthropic_to_internal(req)
        assert kwargs["messages"] == [{"role": "user", "content": "Hello"}]
        assert kwargs["max_tokens"] == 1024
        assert kwargs["model"] == "claude-sonnet-4-20250514"

    def test_system_string(self):
        req = _make_req(system="You are helpful.")
        kwargs = _anthropic_to_internal(req)
        assert kwargs["messages"][0] == {"role": "system", "content": "You are helpful."}
        assert kwargs["messages"][1] == {"role": "user", "content": "Hello"}

    def test_system_blocks(self):
        req = _make_req(system=[
            {"type": "text", "text": "Part one."},
            {"type": "text", "text": "Part two."},
        ])
        kwargs = _anthropic_to_internal(req)
        assert kwargs["messages"][0] == {
            "role": "system",
            "content": "Part one.\n\nPart two.",
        }

    def test_system_blocks_with_cache_control(self):
        """Cache control metadata is ignored (not relevant for bridge)."""
        req = _make_req(system=[
            {"type": "text", "text": "Cached.", "cache_control": {"type": "ephemeral"}},
        ])
        kwargs = _anthropic_to_internal(req)
        assert kwargs["messages"][0]["content"] == "Cached."

    def test_tool_result_messages(self):
        """tool_result content blocks → OpenAI tool role messages."""
        req = _make_req(messages=[
            {"role": "user", "content": "Use the tool"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_123", "name": "search", "input": {"query": "test"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_123", "content": "Found 3 results"},
            ]},
        ])
        kwargs = _anthropic_to_internal(req)
        # Message 0: user text
        assert kwargs["messages"][0] == {"role": "user", "content": "Use the tool"}
        # Message 1: assistant with tool_calls
        assert kwargs["messages"][1]["role"] == "assistant"
        assert len(kwargs["messages"][1]["tool_calls"]) == 1
        tc = kwargs["messages"][1]["tool_calls"][0]
        assert tc["id"] == "toolu_123"
        assert tc["function"]["name"] == "search"
        assert json.loads(tc["function"]["arguments"]) == {"query": "test"}
        # Message 2: tool result
        assert kwargs["messages"][2] == {
            "role": "tool",
            "content": "Found 3 results",
            "tool_call_id": "toolu_123",
        }

    def test_tool_result_with_content_blocks(self):
        """tool_result with content as array of blocks."""
        req = _make_req(messages=[
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [
                    {"type": "text", "text": "line1"},
                    {"type": "text", "text": "line2"},
                ]},
            ]},
        ])
        kwargs = _anthropic_to_internal(req)
        assert kwargs["messages"][0]["content"] == "line1\nline2"

    def test_assistant_tool_use(self):
        """Assistant message with text + tool_use blocks."""
        req = _make_req(messages=[
            {"role": "assistant", "content": [
                {"type": "text", "text": "Let me search."},
                {"type": "tool_use", "id": "call_1", "name": "search", "input": {"q": "x"}},
                {"type": "tool_use", "id": "call_2", "name": "read", "input": {"id": "r1"}},
            ]},
        ])
        kwargs = _anthropic_to_internal(req)
        msg = kwargs["messages"][0]
        assert msg["role"] == "assistant"
        assert msg["content"] == "Let me search."
        assert len(msg["tool_calls"]) == 2
        assert msg["tool_calls"][0]["function"]["name"] == "search"
        assert msg["tool_calls"][1]["function"]["name"] == "read"
        assert json.loads(msg["tool_calls"][1]["function"]["arguments"]) == {"id": "r1"}

    def test_tools_schema(self):
        """Anthropic input_schema → OpenAI function parameters."""
        req = _make_req(tools=[
            {
                "name": "search",
                "description": "Search records",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        ])
        kwargs = _anthropic_to_internal(req)
        assert kwargs["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search records",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            },
        ]

    def test_tool_choice_auto(self):
        req = _make_req(tool_choice={"type": "auto"})
        kwargs = _anthropic_to_internal(req)
        assert kwargs["tool_choice"] == "auto"

    def test_tool_choice_any(self):
        req = _make_req(tool_choice={"type": "any"})
        kwargs = _anthropic_to_internal(req)
        assert kwargs["tool_choice"] == "required"

    def test_tool_choice_specific(self):
        req = _make_req(tool_choice={"type": "tool", "name": "search"})
        kwargs = _anthropic_to_internal(req)
        assert kwargs["tool_choice"] == {
            "type": "function",
            "function": {"name": "search"},
        }

    def test_no_tool_choice(self):
        req = _make_req()
        kwargs = _anthropic_to_internal(req)
        assert "tool_choice" not in kwargs

    def test_temperature_and_top_p(self):
        req = _make_req(temperature=0.5, top_p=0.9)
        kwargs = _anthropic_to_internal(req)
        assert kwargs["temperature"] == 0.5
        assert kwargs["top_p"] == 0.9

    def test_no_optional_params(self):
        req = _make_req()
        kwargs = _anthropic_to_internal(req)
        assert "temperature" not in kwargs
        assert "top_p" not in kwargs
        assert "tools" not in kwargs
        assert "tool_choice" not in kwargs


# ── Translation: Internal → Anthropic ──


class TestInternalToAnthropic:
    def test_text_only(self):
        result = {"content": "Hello world", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        resp = _internal_to_anthropic(result, "claude-test")
        assert resp["type"] == "message"
        assert resp["role"] == "assistant"
        assert resp["model"] == "claude-test"
        assert resp["content"] == [{"type": "text", "text": "Hello world"}]
        assert resp["stop_reason"] == "end_turn"
        assert resp["usage"] == {"input_tokens": 10, "output_tokens": 5}

    def test_empty_content(self):
        result = {"content": "", "usage": {}}
        resp = _internal_to_anthropic(result, "claude-test")
        assert resp["content"] == []  # empty string → no text block

    def test_with_tool_calls(self):
        result = {
            "content": "Let me search.",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {
                        "name": "search",
                        "arguments": '{"query": "test"}',
                    },
                },
            ],
            "finish_reason": "tool_calls",
            "usage": {"prompt_tokens": 50, "completion_tokens": 20},
        }
        resp = _internal_to_anthropic(result, "claude-test")
        assert len(resp["content"]) == 2
        assert resp["content"][0] == {"type": "text", "text": "Let me search."}
        assert resp["content"][1]["type"] == "tool_use"
        assert resp["content"][1]["id"] == "call_abc"
        assert resp["content"][1]["name"] == "search"
        assert resp["content"][1]["input"] == {"query": "test"}
        assert resp["stop_reason"] == "tool_use"

    def test_tool_calls_with_invalid_json(self):
        """Malformed arguments JSON → empty dict input."""
        result = {
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "x", "arguments": "not json"}},
            ],
            "finish_reason": "tool_calls",
            "usage": {},
        }
        resp = _internal_to_anthropic(result, "test")
        assert resp["content"][0]["input"] == {}

    def test_stop_reason_stop(self):
        result = {"content": "done", "finish_reason": "stop", "usage": {}}
        resp = _internal_to_anthropic(result, "test")
        assert resp["stop_reason"] == "end_turn"

    def test_stop_reason_length(self):
        result = {"content": "...", "finish_reason": "length", "usage": {}}
        resp = _internal_to_anthropic(result, "test")
        assert resp["stop_reason"] == "max_tokens"

    def test_stop_reason_tool_calls(self):
        result = {"content": "", "finish_reason": "tool_calls", "usage": {}, "tool_calls": []}
        resp = _internal_to_anthropic(result, "test")
        assert resp["stop_reason"] == "tool_use"

    def test_stop_reason_unknown(self):
        result = {"content": "x", "finish_reason": "some_other", "usage": {}}
        resp = _internal_to_anthropic(result, "test")
        assert resp["stop_reason"] == "end_turn"

    def test_usage_mapping(self):
        result = {"content": "x", "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        resp = _internal_to_anthropic(result, "test")
        assert resp["usage"] == {"input_tokens": 100, "output_tokens": 50}

    def test_usage_empty(self):
        result = {"content": "x", "usage": {}}
        resp = _internal_to_anthropic(result, "test")
        assert resp["usage"] == {"input_tokens": 0, "output_tokens": 0}

    def test_response_has_id(self):
        result = {"content": "x", "usage": {}}
        resp = _internal_to_anthropic(result, "test")
        assert resp["id"].startswith("msg_")

    def test_stop_sequence_is_null(self):
        result = {"content": "x", "usage": {}}
        resp = _internal_to_anthropic(result, "test")
        assert resp["stop_sequence"] is None


# ── Endpoint integration tests ──


@pytest_asyncio.fixture()
async def bridge_app():
    """Create a BridgeServer with a mock LLM caller and return (app, token)."""
    token = "test-token-abc"

    async def mock_llm_caller(messages, max_tokens, **kwargs):
        return {
            "content": f"Echo: {messages[-1].get('content', '')}",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "finish_reason": "stop",
        }

    bridge = BridgeServer(
        session_token=token,
        tools=[],
        on_tool_call=lambda name, args: "",
        llm_caller=mock_llm_caller,
        budget=Budget(max_calls=100, max_tokens=100000),
    )
    app = bridge._build_app()
    yield app, token


@pytest.mark.asyncio
async def test_anthropic_endpoint_roundtrip(bridge_app):
    app, token = bridge_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "claude-test",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi there"}],
            },
            headers={"x-api-key": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "message"
        assert data["role"] == "assistant"
        assert data["model"] == "claude-test"
        assert data["stop_reason"] == "end_turn"
        assert len(data["content"]) == 1
        assert data["content"][0]["type"] == "text"
        assert "Hi there" in data["content"][0]["text"]
        assert data["usage"]["input_tokens"] == 10
        assert data["usage"]["output_tokens"] == 5


@pytest.mark.asyncio
async def test_anthropic_endpoint_with_system(bridge_app):
    app, token = bridge_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "claude-test",
                "max_tokens": 100,
                "system": "Be brief.",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            headers={"x-api-key": token},
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_xapikey_auth_accepted(bridge_app):
    app, token = bridge_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # x-api-key works
        resp = await client.post(
            "/v1/messages",
            json={"model": "test", "max_tokens": 10, "messages": [{"role": "user", "content": "Hi"}]},
            headers={"x-api-key": token},
        )
        assert resp.status_code == 200

        # Bearer token also still works
        resp = await client.post(
            "/v1/messages",
            json={"model": "test", "max_tokens": 10, "messages": [{"role": "user", "content": "Hi"}]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

        # Wrong token fails
        resp = await client.post(
            "/v1/messages",
            json={"model": "test", "max_tokens": 10, "messages": [{"role": "user", "content": "Hi"}]},
            headers={"x-api-key": "wrong-token"},
        )
        assert resp.status_code == 401

        # No token fails
        resp = await client.post(
            "/v1/messages",
            json={"model": "test", "max_tokens": 10, "messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stream_rejected(bridge_app):
    app, token = bridge_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "test",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
            headers={"x-api-key": token},
        )
        assert resp.status_code == 400
        assert "Streaming" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_anthropic_endpoint_with_tools(bridge_app):
    """Verify tools are passed through correctly."""
    app, token = bridge_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "test",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Search for X"}],
                "tools": [
                    {
                        "name": "search",
                        "description": "Search records",
                        "input_schema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    },
                ],
                "tool_choice": {"type": "auto"},
            },
            headers={"x-api-key": token},
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_count_tokens_endpoint(bridge_app):
    """count_tokens returns an estimated token count without charging budget."""
    app, token = bridge_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "claude-test",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hello world"}],
            },
            headers={"x-api-key": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "input_tokens" in data
        assert isinstance(data["input_tokens"], int)
        assert data["input_tokens"] > 0


@pytest.mark.asyncio
async def test_count_tokens_with_tools(bridge_app):
    """count_tokens includes tool definitions in the estimate."""
    app, token = bridge_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp_no_tools = await client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "claude-test",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers={"x-api-key": token},
        )
        resp_with_tools = await client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "claude-test",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}],
                "tools": [
                    {
                        "name": "search",
                        "description": "Search the knowledge base by query",
                        "input_schema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    },
                ],
            },
            headers={"x-api-key": token},
        )
        no_tools_count = resp_no_tools.json()["input_tokens"]
        with_tools_count = resp_with_tools.json()["input_tokens"]
        assert with_tools_count > no_tools_count


@pytest.mark.asyncio
async def test_count_tokens_no_budget_charge(bridge_app):
    """count_tokens should NOT charge the budget."""
    token = "count-test"

    async def mock_llm(messages, max_tokens, **kwargs):
        return {"content": "ok", "usage": {"prompt_tokens": 10, "completion_tokens": 10}, "finish_reason": "stop"}

    from hivemind.sandbox.budget import Budget
    budget = Budget(max_calls=1, max_tokens=100000)
    bridge = BridgeServer(
        session_token=token,
        tools=[],
        on_tool_call=lambda n, a: "",
        llm_caller=mock_llm,
        budget=budget,
    )
    app = bridge._build_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Call count_tokens many times — should never exhaust budget
        for _ in range(5):
            resp = await client.post(
                "/v1/messages/count_tokens",
                json={"model": "t", "max_tokens": 100, "messages": [{"role": "user", "content": "Hi"}]},
                headers={"x-api-key": token},
            )
            assert resp.status_code == 200

        # Real LLM call should still work (budget not consumed)
        resp = await client.post(
            "/v1/messages",
            json={"model": "t", "max_tokens": 100, "messages": [{"role": "user", "content": "Hi"}]},
            headers={"x-api-key": token},
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_anthropic_budget_enforcement(bridge_app):
    """Budget enforcement works through the Anthropic endpoint."""
    token = "budget-test"

    async def mock_llm(messages, max_tokens, **kwargs):
        return {"content": "ok", "usage": {"prompt_tokens": 10, "completion_tokens": 10}, "finish_reason": "stop"}

    bridge = BridgeServer(
        session_token=token,
        tools=[],
        on_tool_call=lambda n, a: "",
        llm_caller=mock_llm,
        budget=Budget(max_calls=2, max_tokens=100000),
    )
    app = bridge._build_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # First call succeeds
        resp = await client.post(
            "/v1/messages",
            json={"model": "t", "max_tokens": 100, "messages": [{"role": "user", "content": "Hi"}]},
            headers={"x-api-key": token},
        )
        assert resp.status_code == 200

        # Second call succeeds
        resp = await client.post(
            "/v1/messages",
            json={"model": "t", "max_tokens": 100, "messages": [{"role": "user", "content": "Hi"}]},
            headers={"x-api-key": token},
        )
        assert resp.status_code == 200

        # Third call should fail (budget exhausted: 2 calls used)
        resp = await client.post(
            "/v1/messages",
            json={"model": "t", "max_tokens": 100, "messages": [{"role": "user", "content": "Hi"}]},
            headers={"x-api-key": token},
        )
        assert resp.status_code == 429

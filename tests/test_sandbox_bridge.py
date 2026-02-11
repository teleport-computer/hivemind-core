import asyncio

import pytest
import pytest_asyncio
import httpx

from hivemind.sandbox.bridge import BridgeServer
from hivemind.sandbox.budget import Budget
from hivemind.tools import Tool


def _make_tools():
    """Create mock tools for testing."""

    def search_index(query: str, limit: int = 20) -> str:
        return f'[{{"record_id": "r1", "title": "Test Record", "query": "{query}"}}]'

    def read_record(record_id: str) -> str:
        if record_id == "r1":
            return "This is the record text for testing."
        return "Record not found"

    return [
        Tool(
            name="search_index",
            description="Search the index",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=search_index,
        ),
        Tool(
            name="read_record",
            description="Read a record",
            parameters={
                "type": "object",
                "properties": {"record_id": {"type": "string"}},
                "required": ["record_id"],
            },
            handler=read_record,
        ),
    ]


async def _mock_llm_caller(messages, max_tokens, model=None, temperature=None, top_p=None):
    """Mock LLM caller that echoes back what it received."""
    return {
        "content": f"LLM response. model={model}, temp={temperature}",
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


async def _mock_on_tool_call(name, args):
    """Mock on_tool_call that dispatches to tool handlers."""
    tools = {t.name: t.handler for t in _make_tools()}
    if name not in tools:
        return f"Error: unknown tool '{name}'"
    return tools[name](**args)


@pytest_asyncio.fixture
async def bridge():
    """Start a bridge server and yield (server, client)."""
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
    # No auth header
    resp = await client.get("/tools")
    assert resp.status_code == 401

    # Wrong token
    resp = await client.get(
        "/tools", headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp.status_code == 401

    # Correct token
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
    assert names == {"search_index", "read_record"}


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
    # Verify budget was updated
    assert budget.summary()["calls"] == 1


@pytest.mark.asyncio
async def test_llm_chat_model_override(bridge):
    """Agent can specify model, temperature, top_p — bridge forwards them."""
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
    assert "claude-haiku" in data["content"]
    assert "0.7" in data["content"]


@pytest.mark.asyncio
async def test_tool_call(bridge):
    server, client, budget = bridge
    headers = {"Authorization": "Bearer test-token-123"}
    resp = await client.post(
        "/tools/search_index",
        headers=headers,
        json={"arguments": {"query": "test query"}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "r1" in data["result"]
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

    # Set budget to 2 calls
    budget.max_calls = 2
    budget.record(prompt_tokens=10, completion_tokens=10)
    budget.record(prompt_tokens=10, completion_tokens=10)

    # Third call should be hard-rejected with 429
    resp = await client.post(
        "/llm/chat",
        headers=headers,
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert resp.status_code == 429
    data = resp.json()
    assert "Budget exhausted" in data["detail"]
    # Budget should NOT have increased (call was rejected)
    assert budget.summary()["calls"] == 2

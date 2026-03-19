"""Tests for scope-agent simulation (nested query agent runs) + budget carving."""

import pytest
import pytest_asyncio
import httpx

from hivemind.sandbox.bridge import BridgeServer
from hivemind.sandbox.budget import Budget
from hivemind.tools import Tool


def _make_tools():
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


async def _mock_llm_caller(messages, max_tokens, model=None, temperature=None, top_p=None):
    return {
        "content": "LLM response",
        "usage": {"prompt_tokens": 10, "completion_tokens": 10},
    }


async def _mock_on_tool_call(name, args):
    tools = {t.name: t.handler for t in _make_tools()}
    if name not in tools:
        return f"Error: unknown tool '{name}'"
    return tools[name](**args)


@pytest_asyncio.fixture
async def scope_bridge():
    """Bridge configured with role=scope and a mock run_query_fn."""

    async def mock_run_query_fn(
        query_agent_id, prompt, scope_fn_source, max_calls, max_tokens, **kwargs
    ):
        return (
            f"Query output for '{prompt}' with scope_fn",
            {"calls": 2, "prompt_tokens": 20, "completion_tokens": 10},
        )

    budget = Budget(max_calls=30, max_tokens=100_000)
    server = BridgeServer(
        session_token="scope-token",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=_mock_llm_caller,
        budget=budget,
        host="127.0.0.1",
        role="scope",
        run_query_fn=mock_run_query_fn,
        scope_query_agent_id="q1",
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    yield server, client, budget

    await client.aclose()
    await server.stop()


@pytest.mark.asyncio
async def test_simulate_returns_output(scope_bridge):
    server, client, budget = scope_bridge
    headers = {"Authorization": "Bearer scope-token"}
    resp = await client.post(
        "/sandbox/simulate",
        headers=headers,
        json={
            "query_agent_id": "q1",
            "prompt": "What happened?",
            "scope_fn_source": "def scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "Query output" in data["output"]


@pytest.mark.asyncio
async def test_simulate_charges_parent_budget(scope_bridge):
    server, client, budget = scope_bridge
    headers = {"Authorization": "Bearer scope-token"}
    calls_before = budget.summary()["calls"]

    await client.post(
        "/sandbox/simulate",
        headers=headers,
        json={
            "query_agent_id": "q1",
            "prompt": "test",
            "scope_fn_source": "def scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}",
        },
    )
    calls_after = budget.summary()["calls"]
    assert calls_after - calls_before == 2


@pytest.mark.asyncio
async def test_simulate_rejects_other_query_agent_id(scope_bridge):
    server, client, budget = scope_bridge
    headers = {"Authorization": "Bearer scope-token"}
    resp = await client.post(
        "/sandbox/simulate",
        headers=headers,
        json={
            "query_agent_id": "other-agent",
            "prompt": "test",
            "scope_fn_source": "def scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}",
        },
    )
    assert resp.status_code == 403
    assert "only access query agent" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_simulate_budget_carving():
    """Simulation should be denied when parent budget is nearly exhausted."""

    async def mock_run_query_fn(**kwargs):
        return ("output", {})

    budget = Budget(max_calls=3, max_tokens=100_000)
    budget.record(prompt_tokens=10, completion_tokens=10)
    budget.record(prompt_tokens=10, completion_tokens=10)
    budget.record(prompt_tokens=10, completion_tokens=10)

    server = BridgeServer(
        session_token="tok",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=_mock_llm_caller,
        budget=budget,
        host="127.0.0.1",
        role="scope",
        run_query_fn=mock_run_query_fn,
        scope_query_agent_id="q1",
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    try:
        headers = {"Authorization": "Bearer tok"}
        resp = await client.post(
            "/sandbox/simulate",
            headers=headers,
            json={
                "query_agent_id": "q1",
                "prompt": "test",
                "scope_fn_source": "def scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}",
            },
        )
        assert resp.status_code == 429
    finally:
        await client.aclose()
        await server.stop()


@pytest.mark.asyncio
async def test_simulate_passes_full_remaining_budget():
    """Simulation should pass full remaining budget, not a fraction."""
    captured = {}

    async def capturing_run_query_fn(
        query_agent_id, prompt, scope_fn_source, max_calls, max_tokens, **kwargs
    ):
        captured["max_calls"] = max_calls
        captured["max_tokens"] = max_tokens
        return ("output", {"calls": 1, "prompt_tokens": 10, "completion_tokens": 10})

    budget = Budget(max_calls=100, max_tokens=100_000)
    server = BridgeServer(
        session_token="tok",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=_mock_llm_caller,
        budget=budget,
        host="127.0.0.1",
        role="scope",
        run_query_fn=capturing_run_query_fn,
        scope_query_agent_id="q1",
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    try:
        headers = {"Authorization": "Bearer tok"}
        await client.post(
            "/sandbox/simulate",
            headers=headers,
            json={
                "query_agent_id": "q1",
                "prompt": "test",
                "scope_fn_source": "def scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}",
            },
        )
        assert captured["max_calls"] == 100
        assert captured["max_tokens"] == 100_000
    finally:
        await client.aclose()
        await server.stop()


@pytest.mark.asyncio
async def test_simulate_requires_active_query_agent_id():
    async def mock_run_query_fn(**kwargs):
        return ("output", {})

    budget = Budget(max_calls=10, max_tokens=100_000)
    server = BridgeServer(
        session_token="tok",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=_mock_llm_caller,
        budget=budget,
        host="127.0.0.1",
        role="scope",
        run_query_fn=mock_run_query_fn,
        scope_query_agent_id=None,
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    try:
        headers = {"Authorization": "Bearer tok"}
        resp = await client.post(
            "/sandbox/simulate",
            headers=headers,
            json={
                "query_agent_id": "q1",
                "prompt": "test",
                "scope_fn_source": "def scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}",
            },
        )
        assert resp.status_code == 400
    finally:
        await client.aclose()
        await server.stop()


@pytest.mark.asyncio
async def test_simulate_not_available_for_query_role():
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
            json={
                "query_agent_id": "q1",
                "prompt": "test",
                "scope_fn_source": "def scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}",
            },
        )
        assert resp.status_code in (404, 405, 422)
    finally:
        await client.aclose()
        await server.stop()


@pytest.mark.asyncio
async def test_simulate_passes_scope_fn_to_query_fn():
    """Verify the scope_fn_source from the request is passed to run_query_fn."""
    captured = {}

    async def capturing_run_query_fn(
        query_agent_id, prompt, scope_fn_source, max_calls, max_tokens, **kwargs
    ):
        captured["scope_fn_source"] = scope_fn_source
        captured["query_agent_id"] = query_agent_id
        captured["prompt"] = prompt
        return ("output", {})

    budget = Budget(max_calls=30, max_tokens=100_000)
    server = BridgeServer(
        session_token="tok",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=_mock_llm_caller,
        budget=budget,
        host="127.0.0.1",
        role="scope",
        run_query_fn=capturing_run_query_fn,
        scope_query_agent_id="qa-1",
    )
    port = await server.start()
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

    try:
        headers = {"Authorization": "Bearer tok"}
        scope_src = "def scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}"
        await client.post(
            "/sandbox/simulate",
            headers=headers,
            json={
                "query_agent_id": "qa-1",
                "prompt": "What about X?",
                "scope_fn_source": scope_src,
            },
        )
        assert captured["scope_fn_source"] == scope_src
        assert captured["query_agent_id"] == "qa-1"
        assert captured["prompt"] == "What about X?"
    finally:
        await client.aclose()
        await server.stop()


@pytest.mark.asyncio
async def test_agent_files_endpoint_on_scope_bridge():
    """Scope bridge should expose /sandbox/agents/{id}/files."""
    import os
    from hivemind.db import Database
    from hivemind.sandbox.agents import AgentStore
    from hivemind.sandbox.models import AgentConfig

    test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
    if not test_dsn:
        pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")

    db = Database(test_dsn)
    agent_store = AgentStore(db)
    try:
        agent_store.create(AgentConfig(
            agent_id="qa-1", name="Query Agent", image="img:v1",
        ))
        agent_store.create(AgentConfig(
            agent_id="qa-2", name="Other Query Agent", image="img:v2",
        ))
        agent_store.save_files("qa-1", {
            "agent.py": "print('hello')",
            "lib/utils.py": "def helper(): pass",
        })

        budget = Budget(max_calls=10, max_tokens=100_000)
        server = BridgeServer(
            session_token="tok",
            tools=_make_tools(),
            on_tool_call=_mock_on_tool_call,
            llm_caller=_mock_llm_caller,
            budget=budget,
            host="127.0.0.1",
            role="scope",
            agent_store=agent_store,
            scope_query_agent_id="qa-1",
        )
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            headers = {"Authorization": "Bearer tok"}

            # List files
            resp = await client.get(
                "/sandbox/agents/qa-1/files", headers=headers
            )
            assert resp.status_code == 200
            files = resp.json()["files"]
            assert len(files) == 2

            # Read file
            resp = await client.get(
                "/sandbox/agents/qa-1/files/agent.py", headers=headers
            )
            assert resp.status_code == 200
            assert "hello" in resp.json()["content"]

            # Nonexistent file
            resp = await client.get(
                "/sandbox/agents/qa-1/files/nope.py", headers=headers
            )
            assert resp.status_code == 404

            # Accessing any other agent id is forbidden
            resp = await client.get("/sandbox/agents/qa-2/files", headers=headers)
            assert resp.status_code == 403
        finally:
            await client.aclose()
            await server.stop()
    finally:
        # Cleanup test agents
        agent_store.delete("qa-1")
        agent_store.delete("qa-2")
        db.close()

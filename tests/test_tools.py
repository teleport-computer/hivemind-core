"""Tests for agent tools — verifying data exposed to agents is correct."""
import json
import sqlite3
import time

import pytest

from hivemind.models import Scope
from hivemind.sandbox.agents import AgentStore
from hivemind.tools import build_agent_file_tools, build_tools


def _setup_records(storage):
    """Populate storage with test records."""
    t = time.time()
    storage.write_record("r1", "alice first doc", "team-a", "alice", t, None)
    storage.write_index("r1", "Alice Doc 1", "First doc by alice", "python,ml", "claim1", "{}", t)

    storage.write_record("r2", "alice second doc", "team-a", "alice", t + 1, None)
    storage.write_index("r2", "Alice Doc 2", "Second doc by alice", "python,web", "claim2", "{}", t + 1)

    storage.write_record("r3", "bob's document", "team-b", "bob", t + 2, None)
    storage.write_index("r3", "Bob Doc", "A doc by bob", "java,web", "claim3", "{}", t + 2)

    storage.write_record("r4", "charlie note", "team-a", None, t + 3, None)
    storage.write_index("r4", "Charlie Note", "A note with no user", "notes", "", "{}", t + 3)


class TestSearchIndex:
    def test_search_returns_user_id(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db, Scope())
        search = next(t for t in tools if t.name == "search_index")
        results = json.loads(search.handler(query="python"))
        assert len(results) >= 1
        assert "user_id" in results[0]
        assert "space_id" in results[0]

    def test_search_filter_by_user(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db, Scope())
        search = next(t for t in tools if t.name == "search_index")

        results = json.loads(search.handler(query="python", user_id="alice"))
        assert all(r["user_id"] == "alice" for r in results)

        results = json.loads(search.handler(query="python", user_id="bob"))
        assert len(results) == 0  # bob has no python records


class TestListIndex:
    def test_list_returns_user_id(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db, Scope())
        list_idx = next(t for t in tools if t.name == "list_index")
        results = json.loads(list_idx.handler())
        assert len(results) == 4
        assert "user_id" in results[0]
        assert "space_id" in results[0]


class TestReadRecord:
    def test_read_includes_metadata_header(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db, Scope())
        read = next(t for t in tools if t.name == "read_record")
        result = read.handler(record_id="r1")
        assert "user_id: alice" in result
        assert "space_id: team-a" in result
        assert "alice first doc" in result

    def test_read_no_header_on_subsequent_chunks(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db, Scope())
        read = next(t for t in tools if t.name == "read_record")
        result = read.handler(record_id="r1", offset=5)
        assert "user_id:" not in result  # no header on offset > 0


class TestListByUser:
    def test_list_by_user(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db, Scope())
        lbu = next(t for t in tools if t.name == "list_by_user")

        results = json.loads(lbu.handler(user_id="alice"))
        assert len(results) == 2
        assert all(r["user_id"] == "alice" for r in results)

    def test_list_by_user_empty(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db, Scope())
        lbu = next(t for t in tools if t.name == "list_by_user")

        results = json.loads(lbu.handler(user_id="nobody"))
        assert len(results) == 0

    def test_list_by_user_respects_scope(self, tmp_db):
        _setup_records(tmp_db)
        # Scope only allows r1
        tools = build_tools(tmp_db, Scope(record_ids=["r1"]))
        lbu = next(t for t in tools if t.name == "list_by_user")

        results = json.loads(lbu.handler(user_id="alice"))
        assert len(results) == 1
        assert results[0]["record_id"] == "r1"


class TestListUsers:
    def test_list_users(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db, Scope())
        lu = next(t for t in tools if t.name == "list_users")

        results = json.loads(lu.handler())
        user_ids = [r["user_id"] for r in results]
        assert "alice" in user_ids
        assert "bob" in user_ids
        # r4 has user_id=None, should not appear
        assert None not in user_ids

    def test_list_users_with_counts(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db, Scope())
        lu = next(t for t in tools if t.name == "list_users")

        results = json.loads(lu.handler())
        alice = next(r for r in results if r["user_id"] == "alice")
        assert alice["record_count"] == 2

    def test_list_users_respects_scope(self, tmp_db):
        _setup_records(tmp_db)
        tools = build_tools(tmp_db, Scope(user_ids=["bob"]))
        lu = next(t for t in tools if t.name == "list_users")

        results = json.loads(lu.handler())
        assert len(results) == 1
        assert results[0]["user_id"] == "bob"


# ── Agent file inspection tools ──


@pytest.fixture
def agent_store_with_files():
    """AgentStore with a registered agent and extracted files."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store = AgentStore(conn)

    from hivemind.sandbox.models import AgentConfig

    store.create(AgentConfig(
        agent_id="qa-1",
        name="Query Agent",
        image="myorg/qa:v1",
    ))
    store.save_files("qa-1", {
        "agent.py": "import httpx\nprint('hello')\n",
        "lib/utils.py": "def helper(): pass\n",
    })
    return store


class TestAgentFileTools:
    def test_list_files(self, agent_store_with_files):
        tools = build_agent_file_tools(agent_store_with_files, "qa-1")
        list_tool = next(t for t in tools if t.name == "list_query_agent_files")
        result = json.loads(list_tool.handler())
        assert len(result["files"]) == 2
        paths = [f["path"] for f in result["files"]]
        assert "agent.py" in paths
        assert "lib/utils.py" in paths

    def test_read_file(self, agent_store_with_files):
        tools = build_agent_file_tools(agent_store_with_files, "qa-1")
        read_tool = next(t for t in tools if t.name == "read_query_agent_file")
        content = read_tool.handler(file_path="agent.py")
        assert "import httpx" in content

    def test_read_file_not_found(self, agent_store_with_files):
        tools = build_agent_file_tools(agent_store_with_files, "qa-1")
        read_tool = next(t for t in tools if t.name == "read_query_agent_file")
        result = read_tool.handler(file_path="nonexistent.py")
        assert "not found" in result.lower()

    def test_list_files_no_files(self, agent_store_with_files):
        # Register a second agent with no files
        from hivemind.sandbox.models import AgentConfig

        agent_store_with_files.create(AgentConfig(
            agent_id="qa-2", name="Empty Agent", image="myorg/empty:v1",
        ))
        tools = build_agent_file_tools(agent_store_with_files, "qa-2")
        list_tool = next(t for t in tools if t.name == "list_query_agent_files")
        result = json.loads(list_tool.handler())
        assert result["files"] == []
        assert "note" in result

    def test_tools_are_prescoped(self, agent_store_with_files):
        """Tools for agent qa-1 can't see files from other agents."""
        from hivemind.sandbox.models import AgentConfig

        agent_store_with_files.create(AgentConfig(
            agent_id="qa-other", name="Other", image="myorg/other:v1",
        ))
        agent_store_with_files.save_files("qa-other", {"secret.py": "SECRET"})

        # Tools scoped to qa-1 should not see qa-other's files
        tools = build_agent_file_tools(agent_store_with_files, "qa-1")
        read_tool = next(t for t in tools if t.name == "read_query_agent_file")
        # Can't read qa-other's file through qa-1's tools
        result = read_tool.handler(file_path="secret.py")
        assert "not found" in result.lower()

    def test_tool_schemas(self, agent_store_with_files):
        tools = build_agent_file_tools(agent_store_with_files, "qa-1")
        assert len(tools) == 2
        for tool in tools:
            openai_def = tool.to_openai_def()
            assert openai_def["type"] == "function"
            assert "name" in openai_def["function"]
            assert "parameters" in openai_def["function"]

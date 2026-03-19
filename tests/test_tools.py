"""Tests for SQL tools and access level enforcement."""

import json
import os

import pytest

from hivemind.db import Database
from hivemind.sandbox.agents import AgentStore
from hivemind.sandbox.models import AgentConfig
from hivemind.tools import (
    AccessLevel,
    Tool,
    _is_select_only,
    _references_internal_tables,
    build_agent_file_tools,
    build_sql_tools,
)


# ── Fixtures ──


@pytest.fixture
def pg_db():
    """Create a test Postgres Database, skip if not available."""
    test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
    if not test_dsn:
        pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")
    db = Database(test_dsn)
    yield db
    db.close()


@pytest.fixture
def test_table(pg_db):
    """Create a test table and clean up after."""
    pg_db.execute_commit(
        "CREATE TABLE IF NOT EXISTS test_tools_data "
        "(id SERIAL PRIMARY KEY, name TEXT, team TEXT)"
    )
    pg_db.execute_commit("DELETE FROM test_tools_data")
    pg_db.execute_commit(
        "INSERT INTO test_tools_data (name, team) VALUES (%s, %s)",
        ["alice", "alpha"],
    )
    pg_db.execute_commit(
        "INSERT INTO test_tools_data (name, team) VALUES (%s, %s)",
        ["bob", "beta"],
    )
    yield
    pg_db.execute_commit("DROP TABLE IF EXISTS test_tools_data")


@pytest.fixture
def agent_store(pg_db):
    """AgentStore backed by test Postgres."""
    return AgentStore(pg_db)


# ── _is_select_only ──


class TestIsSelectOnly:
    def test_simple_select(self):
        assert _is_select_only("SELECT 1") is True

    def test_select_from_table(self):
        assert _is_select_only("SELECT * FROM users WHERE id = 1") is True

    def test_select_with_join(self):
        assert _is_select_only(
            "SELECT a.id, b.name FROM a JOIN b ON a.id = b.a_id"
        ) is True

    def test_insert_rejected(self):
        assert _is_select_only("INSERT INTO t (x) VALUES (1)") is False

    def test_update_rejected(self):
        assert _is_select_only("UPDATE t SET x = 1") is False

    def test_delete_rejected(self):
        assert _is_select_only("DELETE FROM t") is False

    def test_drop_rejected(self):
        assert _is_select_only("DROP TABLE t") is False

    def test_create_table_rejected(self):
        assert _is_select_only("CREATE TABLE t (id INT)") is False

    def test_multiple_statements_rejected(self):
        assert _is_select_only("SELECT 1; DROP TABLE t") is False

    def test_invalid_sql_rejected(self):
        assert _is_select_only("NOT VALID SQL AT ALL ???") is False

    def test_empty_string_rejected(self):
        assert _is_select_only("") is False

    def test_cte_select(self):
        sql = "WITH cte AS (SELECT 1 AS v) SELECT * FROM cte"
        # CTE wrapping a SELECT is still a SELECT
        assert _is_select_only(sql) is True

    def test_subquery(self):
        assert _is_select_only("SELECT * FROM (SELECT 1 AS v) sub") is True


# ── _references_internal_tables ──


class TestReferencesInternalTables:
    def test_detects_hivemind_agents(self):
        assert _references_internal_tables("SELECT * FROM _hivemind_agents") is True

    def test_case_insensitive(self):
        assert _references_internal_tables("SELECT * FROM _HIVEMIND_AGENTS") is True

    def test_mixed_case(self):
        assert _references_internal_tables("SELECT * FROM _Hivemind_Agent_Files") is True

    def test_normal_table_allowed(self):
        assert _references_internal_tables("SELECT * FROM users") is False

    def test_in_where_clause(self):
        assert (
            _references_internal_tables(
                "SELECT * FROM t WHERE table_name = '_hivemind_agents'"
            )
            is True
        )


# ── build_sql_tools: AccessLevel.NONE ──


class TestAccessLevelNone:
    def test_returns_empty_list(self, pg_db):
        tools = build_sql_tools(pg_db, AccessLevel.NONE)
        assert tools == []


# ── build_sql_tools: AccessLevel.FULL_READ (scope agent) ──


class TestAccessLevelFullRead:
    def _get_tools(self, pg_db):
        tools = build_sql_tools(pg_db, AccessLevel.FULL_READ)
        return {t.name: t for t in tools}

    def test_returns_two_tools(self, pg_db):
        tools = build_sql_tools(pg_db, AccessLevel.FULL_READ)
        assert len(tools) == 2
        names = {t.name for t in tools}
        assert names == {"execute_sql", "get_schema"}

    def test_select_works(self, pg_db, test_table):
        tools = self._get_tools(pg_db)
        result = json.loads(tools["execute_sql"].handler("SELECT name FROM test_tools_data ORDER BY name"))
        assert len(result) == 2
        assert result[0]["name"] == "alice"

    def test_insert_blocked(self, pg_db, test_table):
        tools = self._get_tools(pg_db)
        result = json.loads(
            tools["execute_sql"].handler("INSERT INTO test_tools_data (name, team) VALUES ('eve', 'gamma')")
        )
        assert "error" in result
        assert "SELECT" in result["error"]

    def test_internal_table_blocked(self, pg_db):
        tools = self._get_tools(pg_db)
        result = json.loads(
            tools["execute_sql"].handler("SELECT * FROM _hivemind_agents")
        )
        assert "error" in result
        assert "internal" in result["error"].lower() or "denied" in result["error"].lower()

    def test_get_schema_excludes_internal(self, pg_db, test_table):
        tools = self._get_tools(pg_db)
        schema = json.loads(tools["get_schema"].handler())
        table_names = {r["table_name"] for r in schema}
        assert "test_tools_data" in table_names
        assert "_hivemind_agents" not in table_names
        assert "_hivemind_agent_files" not in table_names


# ── build_sql_tools: AccessLevel.SCOPED (query agent) ──


class TestAccessLevelScoped:
    def _allow_all_scope(self, sql, params, rows):
        return {"allow": True, "rows": rows}

    def _filter_team_scope(self, sql, params, rows):
        filtered = [r for r in rows if r.get("team") == "alpha"]
        return {"allow": True, "rows": filtered}

    def _deny_scope(self, sql, params, rows):
        return {"allow": False, "error": "Access denied by policy"}

    def _error_scope(self, sql, params, rows):
        raise RuntimeError("Scope function crashed")

    def test_select_with_passthrough_scope(self, pg_db, test_table):
        tools = build_sql_tools(pg_db, AccessLevel.SCOPED, scope_fn=self._allow_all_scope)
        t = {t.name: t for t in tools}
        result = json.loads(t["execute_sql"].handler("SELECT * FROM test_tools_data ORDER BY name"))
        assert len(result) == 2

    def test_scope_filters_rows(self, pg_db, test_table):
        tools = build_sql_tools(pg_db, AccessLevel.SCOPED, scope_fn=self._filter_team_scope)
        t = {t.name: t for t in tools}
        result = json.loads(t["execute_sql"].handler("SELECT * FROM test_tools_data"))
        assert len(result) == 1
        assert result[0]["team"] == "alpha"

    def test_scope_denies(self, pg_db, test_table):
        tools = build_sql_tools(pg_db, AccessLevel.SCOPED, scope_fn=self._deny_scope)
        t = {t.name: t for t in tools}
        result = json.loads(t["execute_sql"].handler("SELECT * FROM test_tools_data"))
        assert "error" in result
        assert "denied" in result["error"].lower()

    def test_scope_exception_fail_closed(self, pg_db, test_table):
        tools = build_sql_tools(pg_db, AccessLevel.SCOPED, scope_fn=self._error_scope)
        t = {t.name: t for t in tools}
        result = json.loads(t["execute_sql"].handler("SELECT * FROM test_tools_data"))
        assert "error" in result
        assert "denied" in result["error"].lower()

    def test_insert_blocked(self, pg_db, test_table):
        tools = build_sql_tools(pg_db, AccessLevel.SCOPED, scope_fn=self._allow_all_scope)
        t = {t.name: t for t in tools}
        result = json.loads(
            t["execute_sql"].handler("INSERT INTO test_tools_data (name, team) VALUES ('x', 'y')")
        )
        assert "error" in result

    def test_internal_table_blocked(self, pg_db):
        tools = build_sql_tools(pg_db, AccessLevel.SCOPED, scope_fn=self._allow_all_scope)
        t = {t.name: t for t in tools}
        result = json.loads(t["execute_sql"].handler("SELECT * FROM _hivemind_agents"))
        assert "error" in result


# ── build_sql_tools: AccessLevel.FULL_READWRITE (index agent) ──


class TestAccessLevelFullReadwrite:
    def _get_tools(self, pg_db):
        tools = build_sql_tools(pg_db, AccessLevel.FULL_READWRITE)
        return {t.name: t for t in tools}

    def test_select_works(self, pg_db, test_table):
        tools = self._get_tools(pg_db)
        result = json.loads(tools["execute_sql"].handler("SELECT name FROM test_tools_data ORDER BY name"))
        assert len(result) == 2

    def test_insert_works(self, pg_db, test_table):
        tools = self._get_tools(pg_db)
        result = json.loads(
            tools["execute_sql"].handler(
                "INSERT INTO test_tools_data (name, team) VALUES (%s, %s)",
                ["charlie", "gamma"],
            )
        )
        assert result["rowcount"] == 1

    def test_update_works(self, pg_db, test_table):
        tools = self._get_tools(pg_db)
        result = json.loads(
            tools["execute_sql"].handler(
                "UPDATE test_tools_data SET team = %s WHERE name = %s",
                ["omega", "alice"],
            )
        )
        assert result["rowcount"] == 1

    def test_delete_works(self, pg_db, test_table):
        tools = self._get_tools(pg_db)
        result = json.loads(
            tools["execute_sql"].handler(
                "DELETE FROM test_tools_data WHERE name = %s",
                ["bob"],
            )
        )
        assert result["rowcount"] == 1

    def test_write_to_internal_table_blocked(self, pg_db):
        tools = self._get_tools(pg_db)
        result = json.loads(
            tools["execute_sql"].handler(
                "INSERT INTO _hivemind_agents (agent_id, name, image, created_at) "
                "VALUES ('evil', 'evil', 'evil', 0)"
            )
        )
        assert "error" in result
        assert "denied" in result["error"].lower()

    def test_select_from_internal_table_allowed(self, pg_db):
        tools = self._get_tools(pg_db)
        result = json.loads(
            tools["execute_sql"].handler("SELECT agent_id FROM _hivemind_agents LIMIT 1")
        )
        # Should succeed (returns list, not error dict)
        assert isinstance(result, list)


# ── build_agent_file_tools ──


class TestAgentFileTools:
    def test_list_files_with_files(self, pg_db, agent_store):
        agent_store.create(AgentConfig(
            agent_id="file-test-agent",
            name="File Test",
            image="img:test",
        ))
        agent_store.save_files("file-test-agent", {
            "agent.py": "print('hello')",
            "Dockerfile": "FROM python:3.12",
        })
        try:
            tools = build_agent_file_tools(agent_store, "file-test-agent")
            t = {t.name: t for t in tools}
            result = json.loads(t["list_query_agent_files"].handler())
            assert "files" in result
            assert len(result["files"]) == 2
        finally:
            agent_store.delete("file-test-agent")

    def test_list_files_empty(self, pg_db, agent_store):
        agent_store.create(AgentConfig(
            agent_id="empty-file-agent",
            name="Empty",
            image="img:test",
        ))
        try:
            tools = build_agent_file_tools(agent_store, "empty-file-agent")
            t = {t.name: t for t in tools}
            result = json.loads(t["list_query_agent_files"].handler())
            assert result["files"] == []
        finally:
            agent_store.delete("empty-file-agent")

    def test_read_file_exists(self, pg_db, agent_store):
        agent_store.create(AgentConfig(
            agent_id="read-test-agent",
            name="Read Test",
            image="img:test",
        ))
        agent_store.save_files("read-test-agent", {"agent.py": "print('hello')"})
        try:
            tools = build_agent_file_tools(agent_store, "read-test-agent")
            t = {t.name: t for t in tools}
            content = t["read_query_agent_file"].handler("agent.py")
            assert content == "print('hello')"
        finally:
            agent_store.delete("read-test-agent")

    def test_read_file_not_found(self, pg_db, agent_store):
        agent_store.create(AgentConfig(
            agent_id="read-miss-agent",
            name="Read Miss",
            image="img:test",
        ))
        try:
            tools = build_agent_file_tools(agent_store, "read-miss-agent")
            t = {t.name: t for t in tools}
            content = t["read_query_agent_file"].handler("nonexistent.py")
            assert "not found" in content.lower()
        finally:
            agent_store.delete("read-miss-agent")


# ── Tool dataclass ──


class TestToolDataclass:
    def test_to_openai_def(self):
        tool = Tool(
            name="test_tool",
            description="A test tool",
            parameters={"type": "object", "properties": {}},
            handler=lambda: "ok",
        )
        defn = tool.to_openai_def()
        assert defn["type"] == "function"
        assert defn["function"]["name"] == "test_tool"
        assert defn["function"]["description"] == "A test tool"

    def test_sql_error_returns_json_error(self, pg_db, test_table):
        tools = build_sql_tools(pg_db, AccessLevel.FULL_READ)
        t = {t.name: t for t in tools}
        result = json.loads(t["execute_sql"].handler("SELECT * FROM nonexistent_table_xyz"))
        assert "error" in result

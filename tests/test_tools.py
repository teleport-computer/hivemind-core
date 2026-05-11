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
    _validate_table_allowlist,
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
        "CREATE TABLE IF NOT EXISTS test_tools_data (id SERIAL PRIMARY KEY, name TEXT, team TEXT)"
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
        assert _is_select_only("SELECT a.id, b.name FROM a JOIN b ON a.id = b.a_id") is True

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

    def test_select_with_percent_s_param(self):
        assert _is_select_only("SELECT * FROM t WHERE id = %s") is True

    def test_select_with_multiple_params(self):
        assert _is_select_only("SELECT * FROM t WHERE a = %s AND b = %s") is True

    def test_insert_with_params_still_rejected(self):
        assert _is_select_only("INSERT INTO t (x) VALUES (%s)") is False


class TestIsSelectOnlyForbiddenFuncs:
    """H1/H2 regression: SELECT calls that mutate session state, sleep, or
    reach outside the row layer must fail _is_select_only. The connection is
    shared across requests for a tenant; one ``SELECT set_config('search_path',
    'evil', false)`` reroutes every subsequent table lookup."""

    def test_set_config_blocked(self):
        assert _is_select_only("SELECT set_config('search_path', 'public_shadow', false)") is False

    def test_set_role_blocked(self):
        assert _is_select_only("SELECT set_role('admin')") is False

    def test_pg_sleep_blocked(self):
        assert _is_select_only("SELECT pg_sleep(3600)") is False

    def test_pg_sleep_inside_where_blocked(self):
        assert _is_select_only("SELECT id FROM users WHERE pg_sleep(10) IS NULL") is False

    def test_pg_sleep_inside_cte_blocked(self):
        assert _is_select_only("WITH s AS (SELECT pg_sleep(1)) SELECT * FROM users") is False

    def test_dblink_blocked(self):
        assert _is_select_only("SELECT * FROM dblink('host=evil', 'SELECT 1') AS t(c int)") is False

    def test_pg_read_file_blocked(self):
        assert _is_select_only("SELECT pg_read_file('/etc/passwd')") is False

    def test_lo_export_blocked(self):
        assert _is_select_only("SELECT lo_export(1, '/tmp/x')") is False

    def test_current_setting_blocked(self):
        # Pairs with set_config — leaking session state is reconnaissance
        # for a chained search_path attack. Block it together.
        assert _is_select_only("SELECT current_setting('foo')") is False


class TestScopedRequiresScopeFn:
    """M2 contract: SCOPED tools must refuse construction without a scope_fn.
    The earlier behavior (silent passthrough when scope_fn is None) was a
    privacy footgun; pipeline.py also fails-closed at the orchestration layer
    (see C1) but the tool builder is the second line of defense."""

    def test_scoped_with_no_scope_fn_raises(self):
        # FakeDB sufficient — error must surface before any DB interaction.
        class FakeDB:
            def execute(self, sql, params=None):
                return []

            def execute_commit(self, sql, params=None):
                return 0

        with pytest.raises(ValueError, match="SCOPED"):
            build_sql_tools(FakeDB(), AccessLevel.SCOPED, scope_fn=None)

    def test_scoped_with_scope_fn_constructs(self):
        class FakeDB:
            def execute(self, sql, params=None):
                return []

            def execute_commit(self, sql, params=None):
                return 0

        fn = lambda sql, params, rows: {"allow": True, "rows": rows}
        tools = build_sql_tools(
            FakeDB(),
            AccessLevel.SCOPED,
            scope_fn=fn,
            allowed_tables=[],
        )
        assert {t.name for t in tools} == {"execute_sql", "get_schema"}

    def test_full_read_with_no_scope_fn_constructs(self):
        # FULL_READ never invokes scope_fn; missing one is fine.
        class FakeDB:
            def execute(self, sql, params=None):
                return []

            def execute_commit(self, sql, params=None):
                return 0

        tools = build_sql_tools(FakeDB(), AccessLevel.FULL_READ, scope_fn=None)
        assert len(tools) == 2

    def test_missing_allowed_tables_fails_closed_on_execution(self):
        class FakeDB:
            def execute(self, sql, params=None):
                return [{"leak": True}]

            def get_schema(self, exclude_internal=True):
                return [{"table_name": "secret_table", "column_name": "leak"}]

        tools = build_sql_tools(FakeDB(), AccessLevel.FULL_READ)
        t = {t.name: t for t in tools}

        assert json.loads(t["execute_sql"].handler("SELECT * FROM secret_table")) == {
            "error": "query rejected (room missing table allowlist)"
        }
        assert json.loads(t["get_schema"].handler()) == []


class TestValidateTableAllowlist:
    def test_allows_cte_wrapping_allowed_table(self):
        sql = """
        WITH tags AS (
            SELECT lower(tag) AS hashtag
            FROM watch_history
            CROSS JOIN LATERAL jsonb_array_elements_text(
                CASE
                    WHEN jsonb_typeof(hashtags::jsonb) = 'array'
                    THEN hashtags::jsonb
                    ELSE '[]'::jsonb
                END
            ) AS tag
        )
        SELECT hashtag, COUNT(*) AS watches
        FROM tags
        GROUP BY hashtag
        ORDER BY watches DESC
        LIMIT 30
        """

        assert _validate_table_allowlist(sql, ["watch_history"]) is None

    def test_rejects_disallowed_table_inside_cte(self):
        sql = """
        WITH safe AS (
            SELECT *
            FROM private_watch_history
        )
        SELECT *
        FROM safe
        """

        assert _validate_table_allowlist(sql, ["watch_history"]) == "query rejected"

    def test_nested_cte_alias_does_not_hide_outer_base_table(self):
        sql = """
        SELECT *
        FROM private_watch_history
        WHERE EXISTS (
            WITH private_watch_history AS (SELECT 1 AS ok)
            SELECT 1
            FROM private_watch_history
        )
        """

        assert _validate_table_allowlist(sql, ["watch_history"]) == "query rejected"


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

    def test_in_where_clause_string_literal(self):
        # String literal mentioning internal table is NOT an actual table reference
        assert (
            _references_internal_tables("SELECT * FROM t WHERE table_name = '_hivemind_agents'")
            is False
        )

    def test_join_with_internal_table(self):
        assert (
            _references_internal_tables(
                "SELECT * FROM t JOIN _hivemind_agents ON t.id = _hivemind_agents.id"
            )
            is True
        )

    def test_information_schema_introspect_passes(self):
        # The website's database page uses queries like this. Older code
        # fail-closed when sqlglot tripped on the LIKE/ESCAPE clause and
        # returned 400 to the owner. Now the fall-through regex check kicks
        # in and lets it through (information_schema isn't internal).
        sql = (
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' "
            "AND table_name NOT LIKE '\\_%' ESCAPE '\\' "
            "ORDER BY table_name"
        )
        assert _references_internal_tables(sql) is False

    def test_unparseable_sql_with_internal_prefix_still_blocked(self):
        # Even if sqlglot can't parse the SQL, the literal "_hivemind_"
        # substring trips the regex fallback. Owners can't sneak an
        # internal-table read through by appending bogus syntax.
        sql = "SELECT * FROM _hivemind_agents WHERE bogus !@#$ syntax"
        assert _references_internal_tables(sql) is True

    def test_unparseable_sql_without_internal_prefix_passes(self):
        # Truly unparseable AND no _hivemind_ token: now allowed (was
        # fail-closed). The schema-prefix block in
        # _validate_table_allowlist is what keeps agents from enumerating
        # internals via this path; this function is a belt-and-suspenders
        # check on the owner-only run_store path.
        sql = "this is !@#$ not sql at all"
        assert _references_internal_tables(sql) is False


# ── build_sql_tools: AccessLevel.NONE ──


class TestAccessLevelNone:
    def test_returns_empty_list(self, pg_db):
        tools = build_sql_tools(pg_db, AccessLevel.NONE)
        assert tools == []


# ── build_sql_tools: AccessLevel.FULL_READ (scope agent) ──


class TestAccessLevelFullRead:
    def _get_tools(self, pg_db):
        tools = build_sql_tools(
            pg_db,
            AccessLevel.FULL_READ,
            allowed_tables=["test_tools_data"],
        )
        return {t.name: t for t in tools}

    def test_returns_two_tools(self, pg_db):
        tools = build_sql_tools(
            pg_db,
            AccessLevel.FULL_READ,
            allowed_tables=["test_tools_data"],
        )
        assert len(tools) == 2
        names = {t.name for t in tools}
        assert names == {"execute_sql", "get_schema"}

    def test_select_works(self, pg_db, test_table):
        tools = self._get_tools(pg_db)
        result = json.loads(
            tools["execute_sql"].handler("SELECT name FROM test_tools_data ORDER BY name")
        )
        assert len(result) == 2
        assert result[0]["name"] == "alice"

    def test_stray_params_are_dropped_when_sql_has_no_placeholders(self):
        class FakeDB:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                return [{"ok": True}]

            def get_schema(self, exclude_internal=True):
                return []

        db = FakeDB()
        tools = build_sql_tools(db, AccessLevel.FULL_READ, allowed_tables=[])
        t = {t.name: t for t in tools}

        result = json.loads(t["execute_sql"].handler("SELECT 1", ["unused"]))

        assert result == [{"ok": True}]
        assert db.calls == [("SELECT 1", [])]

    def test_params_are_preserved_when_sql_has_placeholders(self):
        class FakeDB:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                return [{"ok": True}]

            def get_schema(self, exclude_internal=True):
                return []

        db = FakeDB()
        tools = build_sql_tools(db, AccessLevel.FULL_READ, allowed_tables=[])
        t = {t.name: t for t in tools}

        result = json.loads(t["execute_sql"].handler("SELECT %s", ["kept"]))

        assert result == [{"ok": True}]
        assert db.calls == [("SELECT %s", ["kept"])]

    def test_insert_blocked(self, pg_db, test_table):
        tools = self._get_tools(pg_db)
        result = json.loads(
            tools["execute_sql"].handler(
                "INSERT INTO test_tools_data (name, team) VALUES ('eve', 'gamma')"
            )
        )
        assert "error" in result
        assert "SELECT" in result["error"]

    def test_internal_table_blocked(self, pg_db):
        tools = self._get_tools(pg_db)
        result = json.loads(tools["execute_sql"].handler("SELECT * FROM _hivemind_agents"))
        assert "error" in result
        # Opaque rejection — _validate_table_allowlist returns "query rejected"
        # regardless of why, so an attacker can't probe what's behind the wall.
        assert "rejected" in result["error"].lower()

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
        tools = build_sql_tools(
            pg_db,
            AccessLevel.SCOPED,
            scope_fn=self._allow_all_scope,
            allowed_tables=["test_tools_data"],
        )
        t = {t.name: t for t in tools}
        result = json.loads(t["execute_sql"].handler("SELECT * FROM test_tools_data ORDER BY name"))
        assert len(result) == 2

    def test_scope_filters_rows(self, pg_db, test_table):
        tools = build_sql_tools(
            pg_db,
            AccessLevel.SCOPED,
            scope_fn=self._filter_team_scope,
            allowed_tables=["test_tools_data"],
        )
        t = {t.name: t for t in tools}
        result = json.loads(t["execute_sql"].handler("SELECT * FROM test_tools_data"))
        assert len(result) == 1
        assert result[0]["team"] == "alpha"

    def test_scope_denies(self, pg_db, test_table):
        tools = build_sql_tools(
            pg_db,
            AccessLevel.SCOPED,
            scope_fn=self._deny_scope,
            allowed_tables=["test_tools_data"],
        )
        t = {t.name: t for t in tools}
        result = json.loads(t["execute_sql"].handler("SELECT * FROM test_tools_data"))
        assert "error" in result
        assert "denied" in result["error"].lower()

    def test_scope_exception_fail_closed(self, pg_db, test_table):
        tools = build_sql_tools(
            pg_db,
            AccessLevel.SCOPED,
            scope_fn=self._error_scope,
            allowed_tables=["test_tools_data"],
        )
        t = {t.name: t for t in tools}
        result = json.loads(t["execute_sql"].handler("SELECT * FROM test_tools_data"))
        # Fail-closed: scope_fn raising must surface an error to the caller
        # rather than letting the rows through. The message wording comes
        # from apply_scope_fn ("Scope function error: ..."); we only assert
        # the contract (error returned, no rows leaked).
        assert "error" in result
        assert isinstance(result["error"], str) and result["error"]
        assert "rows" not in result

    def test_insert_blocked(self, pg_db, test_table):
        tools = build_sql_tools(
            pg_db,
            AccessLevel.SCOPED,
            scope_fn=self._allow_all_scope,
            allowed_tables=["test_tools_data"],
        )
        t = {t.name: t for t in tools}
        result = json.loads(
            t["execute_sql"].handler("INSERT INTO test_tools_data (name, team) VALUES ('x', 'y')")
        )
        assert "error" in result

    def test_internal_table_blocked(self, pg_db):
        tools = build_sql_tools(
            pg_db,
            AccessLevel.SCOPED,
            scope_fn=self._allow_all_scope,
            allowed_tables=["test_tools_data"],
        )
        t = {t.name: t for t in tools}
        result = json.loads(t["execute_sql"].handler("SELECT * FROM _hivemind_agents"))
        assert "error" in result


# ── build_agent_file_tools ──


class TestAgentFileTools:
    def test_list_files_with_files(self, pg_db, agent_store):
        agent_store.create(
            AgentConfig(
                agent_id="file-test-agent",
                name="File Test",
                image="img:test",
            )
        )
        agent_store.save_files(
            "file-test-agent",
            {
                "agent.py": "print('hello')",
                "Dockerfile": "FROM python:3.12",
            },
        )
        try:
            tools = build_agent_file_tools(agent_store, "file-test-agent")
            t = {t.name: t for t in tools}
            result = json.loads(t["list_query_agent_files"].handler())
            assert "files" in result
            assert len(result["files"]) == 2
        finally:
            agent_store.delete("file-test-agent")

    def test_list_files_empty(self, pg_db, agent_store):
        agent_store.create(
            AgentConfig(
                agent_id="empty-file-agent",
                name="Empty",
                image="img:test",
            )
        )
        try:
            tools = build_agent_file_tools(agent_store, "empty-file-agent")
            t = {t.name: t for t in tools}
            result = json.loads(t["list_query_agent_files"].handler())
            assert result["files"] == []
        finally:
            agent_store.delete("empty-file-agent")

    def test_read_file_exists(self, pg_db, agent_store):
        agent_store.create(
            AgentConfig(
                agent_id="read-test-agent",
                name="Read Test",
                image="img:test",
            )
        )
        agent_store.save_files("read-test-agent", {"agent.py": "print('hello')"})
        try:
            tools = build_agent_file_tools(agent_store, "read-test-agent")
            t = {t.name: t for t in tools}
            content = t["read_query_agent_file"].handler("agent.py")
            assert content == "print('hello')"
        finally:
            agent_store.delete("read-test-agent")

    def test_read_file_not_found(self, pg_db, agent_store):
        agent_store.create(
            AgentConfig(
                agent_id="read-miss-agent",
                name="Read Miss",
                image="img:test",
            )
        )
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
        tools = build_sql_tools(
            pg_db,
            AccessLevel.FULL_READ,
            allowed_tables=["test_tools_data"],
        )
        t = {t.name: t for t in tools}
        result = json.loads(t["execute_sql"].handler("SELECT * FROM nonexistent_table_xyz"))
        assert "error" in result

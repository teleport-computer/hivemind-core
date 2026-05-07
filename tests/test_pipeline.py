"""Tests for the Pipeline orchestrator with mocked Docker."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import hivemind.pipeline as pipeline_module
from hivemind.config import Settings
from hivemind.db import Database
from hivemind.models import QueryRequest, StoreRequest
from hivemind.pipeline import Pipeline
from hivemind.sandbox.agents import AgentStore
from hivemind.sandbox.models import AgentConfig


def _make_pipeline(db: Database) -> Pipeline:
    """Create a Pipeline with minimal settings."""
    settings = Settings(
        database_url="unused",  # db already created
        llm_api_key="test",
    )
    agent_store = AgentStore(db)
    return Pipeline(settings, db, agent_store)


def _mock_default_scope(pipeline: Pipeline) -> None:
    """Give run_query tests the required scope stage without spending Docker/LLM."""
    pipeline.settings.default_scope_agent = "scope-default"
    scope_fn = lambda sql, params, rows: {"allow": True, "rows": rows}
    pipeline._run_scope_agent = AsyncMock(
        return_value=(
            scope_fn,
            "def scope(sql, params, rows): return {'allow': True, 'rows': rows}",
            {"total_tokens": 0},
        )
    )


@pytest.fixture
def pg_db(tmp_path):
    """Create a test Postgres Database.

    Falls back to mocking if no Postgres is available.
    """
    import os
    test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
    if not test_dsn:
        pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")
    db = Database(test_dsn)
    yield db
    db.close()


class TestRunStore:
    @pytest.mark.asyncio
    async def test_store_select(self, pg_db):
        pipeline = _make_pipeline(pg_db)
        req = StoreRequest(sql="SELECT 1 AS val")
        resp = await pipeline.run_store(req)
        assert resp.rows == [{"val": 1}]

    @pytest.mark.asyncio
    async def test_store_create_and_insert(self, pg_db):
        pipeline = _make_pipeline(pg_db)

        # Create table
        req = StoreRequest(
            sql="CREATE TABLE IF NOT EXISTS test_store_pipeline (id SERIAL PRIMARY KEY, name TEXT)"
        )
        resp = await pipeline.run_store(req)

        # Insert
        req = StoreRequest(
            sql="INSERT INTO test_store_pipeline (name) VALUES (%s)",
            params=["alice"],
        )
        resp = await pipeline.run_store(req)
        assert resp.rowcount == 1

        # Read back
        req = StoreRequest(sql="SELECT name FROM test_store_pipeline")
        resp = await pipeline.run_store(req)
        assert resp.rows == [{"name": "alice"}]

        # Cleanup
        await pipeline.run_store(StoreRequest(sql="DROP TABLE test_store_pipeline"))


class TestRunQuery:
    @pytest.mark.asyncio
    async def test_query_requires_agent(self, pg_db):
        pipeline = _make_pipeline(pg_db)
        req = QueryRequest(query="What happened?")
        with pytest.raises(ValueError, match="No query agent"):
            await pipeline.run_query(req)

    @pytest.mark.asyncio
    async def test_query_agent_not_found(self, pg_db):
        pipeline = _make_pipeline(pg_db)
        _mock_default_scope(pipeline)
        req = QueryRequest(query="What?", query_agent_id="nonexistent")
        with pytest.raises(ValueError, match="not found"):
            await pipeline.run_query(req)

    @pytest.mark.asyncio
    async def test_query_requires_scope_agent(self, pg_db):
        pipeline = _make_pipeline(pg_db)
        req = QueryRequest(query="What?", query_agent_id="q1")
        with pytest.raises(ValueError, match="scope_agent_id is required"):
            await pipeline.run_query(req)

    @pytest.mark.asyncio
    async def test_scope_agent_not_found(self, pg_db):
        pipeline = _make_pipeline(pg_db)
        req = QueryRequest(
            query="What?",
            query_agent_id="q1",
            scope_agent_id="nonexistent",
        )
        with pytest.raises(ValueError, match="not found"):
            await pipeline.run_query(req)

    @pytest.mark.asyncio
    async def test_scope_agent_returns_scope_fn(self, pg_db, monkeypatch):
        """Scope agent can return a scope_fn (new format)."""
        pipeline = _make_pipeline(pg_db)
        pipeline.agent_store.create(AgentConfig(
            agent_id="scope-returns",
            name="Scope Agent",
            image="img:scope",
        ))

        scope_fn_source = (
            "def scope(sql, params, rows):\n"
            "    return {'allow': True, 'rows': [r for r in rows if r.get('team') == 'alpha']}"
        )

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                return json.dumps({"scope_fn": scope_fn_source}), {
                    "total_tokens": 10
                }

        monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)

        req = QueryRequest(
            query="What?",
            query_agent_id="q1",
            scope_agent_id="scope-returns",
        )
        fn, source, usage = await pipeline._run_scope_agent(req, max_tokens=1000)

        # Verify the compiled function works
        rows = [{"team": "alpha", "val": 1}, {"team": "beta", "val": 2}]
        result = fn("SELECT * FROM t", [], rows)
        assert result["allow"] is True
        assert len(result["rows"]) == 1
        assert result["rows"][0]["team"] == "alpha"
        assert usage["total_tokens"] == 10

    @pytest.mark.asyncio
    async def test_scope_agent_preserves_emitted_scope_fn_for_policy(
        self, monkeypatch
    ):
        settings = Settings(database_url="unused", llm_api_key="test")
        agent_store = MagicMock(spec=AgentStore)
        agent_store.get.return_value = AgentConfig(
            agent_id="scope-aggregate-repair",
            name="Scope Agent",
            image="img:scope",
        )
        agent_store.list_file_paths.return_value = []
        pipeline = Pipeline(settings, MagicMock(spec=Database), agent_store)

        scope_fn_source = (
            "def scope(sql, params, rows):\n"
            "    return {'allow': True, 'rows': [{'match_count': len(rows)}]}\n"
        )

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                return json.dumps({"scope_fn": scope_fn_source}), {
                    "total_tokens": 10
                }

        monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)

        req = QueryRequest(
            query="Which day has the highest count?",
            query_agent_id="q1",
            scope_agent_id="scope-aggregate-repair",
            policy="Allowed: aggregate statistics and counts. Not allowed: raw rows.",
        )
        fn, source, usage = await pipeline._run_scope_agent(req, max_tokens=1000)

        result = fn(
            "SELECT DATE(created_at) AS bucket_day, COUNT(*) AS items "
            "FROM records GROUP BY DATE(created_at)",
            [],
            [{"bucket_day": "2026-01-01", "items": 42}],
        )
        assert result == {"allow": True, "rows": [{"match_count": 1}]}
        role_result = fn(
            "SELECT role, COUNT(*) AS total FROM messages GROUP BY role",
            [],
            [{"role": "assistant", "total": 7}],
        )
        assert role_result == {"allow": True, "rows": [{"match_count": 1}]}
        sensitive_alias_result = fn(
            "SELECT content AS topic, COUNT(*) AS items "
            "FROM messages GROUP BY content",
            [],
            [{"topic": "private message text", "items": 1}],
        )
        assert sensitive_alias_result == {
            "allow": True,
            "rows": [{"match_count": 1}],
        }
        raw_result = fn(
            "SELECT id, title FROM records LIMIT 1",
            [],
            [{"id": "row-1", "title": "private"}],
        )
        assert raw_result == {"allow": True, "rows": [{"match_count": 1}]}
        assert source == scope_fn_source
        assert usage["total_tokens"] == 10

    @pytest.mark.asyncio
    async def test_scope_agent_does_not_repair_without_aggregate_policy(
        self, monkeypatch
    ):
        settings = Settings(database_url="unused", llm_api_key="test")
        agent_store = MagicMock(spec=AgentStore)
        agent_store.get.return_value = AgentConfig(
            agent_id="scope-no-aggregate-repair",
            name="Scope Agent",
            image="img:scope",
        )
        agent_store.list_file_paths.return_value = []
        pipeline = Pipeline(settings, MagicMock(spec=Database), agent_store)

        scope_fn_source = (
            "def scope(sql, params, rows):\n"
            "    return {'allow': True, 'rows': [{'match_count': len(rows)}]}\n"
        )

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                return json.dumps({"scope_fn": scope_fn_source}), {
                    "total_tokens": 10
                }

        monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)

        req = QueryRequest(
            query="Which day has the highest count?",
            query_agent_id="q1",
            scope_agent_id="scope-no-aggregate-repair",
            policy="No raw row dumps.",
        )
        fn, source, _usage = await pipeline._run_scope_agent(req, max_tokens=1000)

        result = fn(
            "SELECT DATE(created_at) AS bucket_day, COUNT(*) AS items "
            "FROM records GROUP BY DATE(created_at)",
            [],
            [{"bucket_day": "2026-01-01", "items": 42}],
        )
        assert result == {"allow": True, "rows": [{"match_count": 1}]}
        assert source == scope_fn_source

    @pytest.mark.asyncio
    async def test_scope_agent_accepts_noisy_stdout_before_scope_json(
        self, monkeypatch
    ):
        settings = Settings(database_url="unused", llm_api_key="test")
        agent_store = MagicMock(spec=AgentStore)
        agent_store.get.return_value = AgentConfig(
            agent_id="scope-noisy",
            name="Scope Agent",
            image="img:scope",
        )
        agent_store.list_file_paths.return_value = []
        pipeline = Pipeline(settings, MagicMock(spec=Database), agent_store)

        scope_fn_source = (
            "def scope(sql, params, rows):\n"
            "    return {'allow': True, 'rows': rows}\n"
        )

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                return (
                    "Hermes retry log on stdout\n"
                    + json.dumps({"scope_fn": scope_fn_source}),
                    {"total_tokens": 0},
                )

        monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)

        req = QueryRequest(
            query="What?",
            query_agent_id="q1",
            scope_agent_id="scope-noisy",
        )
        fn, source, usage = await pipeline._run_scope_agent(req, max_tokens=1000)

        assert source == scope_fn_source
        assert fn("SELECT 1", [], [{"x": 1}]) == {
            "allow": True,
            "rows": [{"x": 1}],
        }
        assert usage["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_hermes_scope_agent_skips_default_query_source_bind_mount(
        self, monkeypatch
    ):
        settings = Settings(database_url="unused", llm_api_key="test")
        agent_store = MagicMock(spec=AgentStore)
        agent_store.get.return_value = AgentConfig(
            agent_id="scope-hermes",
            name="Scope Hermes",
            image="img:scope-hermes",
            harness="hermes",
        )
        agent_store.list_file_paths.return_value = [
            {"path": "agent.py", "size_bytes": 12, "attestable": True}
        ]
        pipeline = Pipeline(settings, MagicMock(spec=Database), agent_store)

        captured: dict = {}

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                captured["extra_volumes"] = kwargs.get("extra_volumes")
                return (
                    json.dumps(
                        {
                            "scope_fn": (
                                "def scope(sql, params, rows):\n"
                                "    return {'allow': True, 'rows': rows}\n"
                            )
                        }
                    ),
                    {"total_tokens": 0},
                )

        monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)

        req = QueryRequest(
            query="What?",
            query_agent_id="default-query-hermes",
            scope_agent_id="scope-hermes",
        )
        await pipeline._run_scope_agent(req, max_tokens=1000)

        assert captured["extra_volumes"] is None

    @pytest.mark.asyncio
    async def test_scope_agent_rejects_invalid_scope_fn(self, pg_db, monkeypatch):
        """Scope agent returning invalid scope_fn source should fail."""
        pipeline = _make_pipeline(pg_db)
        pipeline.agent_store.create(AgentConfig(
            agent_id="scope-rejects",
            name="Scope Agent",
            image="img:scope",
        ))

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                return json.dumps({"scope_fn": "import os\ndef scope(sql, params, rows): return True"}), {
                    "total_tokens": 0
                }

        monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)

        req = QueryRequest(
            query="What?",
            query_agent_id="q1",
            scope_agent_id="scope-rejects",
        )
        with pytest.raises(ValueError, match="imports"):
            await pipeline._run_scope_agent(req, max_tokens=1000)

    @pytest.mark.asyncio
    async def test_default_scope_agent_used_when_scope_omitted(self, pg_db):
        settings = Settings(
            database_url="unused",
            llm_api_key="test",
            default_scope_agent="scope-default",
        )
        pipeline = Pipeline(settings, pg_db, AgentStore(pg_db))

        mock_scope_fn = lambda sql, params, rows: {"allow": True, "rows": rows}
        pipeline._run_scope_agent = AsyncMock(
            return_value=(mock_scope_fn, "def scope(sql, params, rows): return {'allow': True, 'rows': rows}", {"total_tokens": 0})
        )
        pipeline._run_query_agent = AsyncMock(
            return_value=("output", {"total_tokens": 0})
        )

        req = QueryRequest(query="What?", query_agent_id="q1")
        await pipeline.run_query(req)

        pipeline._run_scope_agent.assert_awaited_once()
        _, kwargs = pipeline._run_query_agent.await_args
        assert kwargs["scope_fn"] is not None

    @pytest.mark.asyncio
    async def test_mediator_budget_is_reserved_for_query_agent(self, pg_db):
        pipeline = _make_pipeline(pg_db)
        _mock_default_scope(pipeline)
        pipeline._run_query_agent = AsyncMock(
            return_value=("output", {"total_tokens": 0})
        )
        pipeline._run_mediator_agent = AsyncMock(
            return_value=("mediated", {"total_tokens": 0})
        )

        req = QueryRequest(
            query="What?",
            query_agent_id="q1",
            mediator_agent_id="med1",
            max_tokens=1000,
        )
        await pipeline.run_query(req)

        _, kwargs = pipeline._run_query_agent.await_args
        assert kwargs["max_tokens"] == 700
        pipeline._run_mediator_agent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mediator_failure_fails_closed(self, pg_db):
        pipeline = _make_pipeline(pg_db)
        _mock_default_scope(pipeline)
        pipeline._run_query_agent = AsyncMock(
            return_value=("raw output", {"total_tokens": 42})
        )
        pipeline._run_mediator_agent = AsyncMock(
            side_effect=ValueError("mediator failed")
        )

        req = QueryRequest(
            query="What?",
            query_agent_id="q1",
            mediator_agent_id="med1",
        )

        with pytest.raises(ValueError, match="refusing to return unmediated output"):
            await pipeline.run_query(req)

    @pytest.mark.asyncio
    async def test_mediator_agent_not_found_still_raises(self, pg_db):
        pipeline = _make_pipeline(pg_db)
        _mock_default_scope(pipeline)
        pipeline._run_query_agent = AsyncMock(
            return_value=("raw output", {"total_tokens": 0})
        )
        req = QueryRequest(
            query="What?",
            query_agent_id="q1",
            mediator_agent_id="missing-mediator",
        )
        with pytest.raises(ValueError, match="Mediator agent 'missing-mediator' not found"):
            await pipeline.run_query(req)

    @pytest.mark.asyncio
    async def test_mediator_is_skipped_when_remaining_budget_is_too_low(self, pg_db):
        pipeline = _make_pipeline(pg_db)
        _mock_default_scope(pipeline)
        pipeline._run_query_agent = AsyncMock(
            return_value=("raw output", {"total_tokens": 49})
        )
        pipeline._run_mediator_agent = AsyncMock(
            return_value=("mediated", {"total_tokens": 1})
        )

        req = QueryRequest(
            query="What?",
            query_agent_id="q1",
            mediator_agent_id="med1",
            max_tokens=50,
        )

        with pytest.raises(ValueError, match="insufficient remaining budget"):
            await pipeline.run_query(req)
        pipeline._run_mediator_agent.assert_not_called()


class TestQueryRequestModel:
    def test_query_field(self):
        req = QueryRequest(query="What happened?")
        assert req.query == "What happened?"

    def test_missing_query_raises(self):
        with pytest.raises(ValueError, match="query"):
            QueryRequest()

    def test_max_tokens(self):
        req = QueryRequest(query="test", max_tokens=50000)
        assert req.max_tokens == 50000

    def test_max_tokens_defaults_to_none(self):
        req = QueryRequest(query="test")
        assert req.max_tokens is None

    def test_max_tokens_must_be_positive(self):
        with pytest.raises(Exception):
            QueryRequest(query="test", max_tokens=0)

    def test_mediator_agent_id(self):
        req = QueryRequest(query="test", mediator_agent_id="med1")
        assert req.mediator_agent_id == "med1"


class TestProviderRouting:
    """Pipeline.``_client_for`` decides which AsyncOpenAI client a request uses.

    These tests exercise the routing without needing a Postgres DB by
    monkey-patching ``Pipeline.__init__`` to skip the bootstrap; we just
    poke the resolved client dict directly.
    """

    def _bare_pipeline(self, *, tinfoil: str = "") -> Pipeline:
        """Build a Pipeline without touching Postgres."""
        settings = Settings(
            database_url="unused",
            llm_api_key="test",
            tinfoil_api_key=tinfoil,
        )
        return Pipeline(settings, MagicMock(spec=Database), MagicMock(spec=AgentStore))

    def test_default_provider_routes_to_openrouter(self):
        pipeline = self._bare_pipeline()
        assert pipeline._client_for(None) is pipeline.llm_clients["openrouter"]
        assert pipeline._client_for("") is pipeline.llm_clients["openrouter"]
        assert pipeline._client_for("openrouter") is pipeline.llm_clients["openrouter"]

    def test_provider_case_insensitive(self):
        pipeline = self._bare_pipeline(tinfoil="tk_test")
        assert pipeline._client_for("OpenRouter") is pipeline.llm_clients["openrouter"]
        assert pipeline._client_for("TINFOIL") is pipeline.llm_clients["tinfoil"]

    def test_tinfoil_without_key_raises(self):
        pipeline = self._bare_pipeline()  # no tinfoil key
        with pytest.raises(ValueError, match="HIVEMIND_TINFOIL_API_KEY"):
            pipeline._client_for("tinfoil")

    def test_tinfoil_with_key_routes_to_tinfoil_client(self):
        pipeline = self._bare_pipeline(tinfoil="tk_test")
        assert "tinfoil" in pipeline.llm_clients
        assert pipeline._client_for("tinfoil") is pipeline.llm_clients["tinfoil"]
        assert pipeline._client_for("tinfoil") is not pipeline.llm_clients["openrouter"]

    def test_unknown_provider_raises(self):
        pipeline = self._bare_pipeline()
        with pytest.raises(ValueError, match="Unknown provider"):
            pipeline._client_for("anthropic-direct")

    def test_run_query_eager_validates_unknown_provider(self):
        """An unknown provider must fail before any agent runs (no scope/query spend)."""
        pipeline = self._bare_pipeline()
        pipeline._run_scope_agent = AsyncMock()
        pipeline._run_query_agent = AsyncMock()

        req = QueryRequest(query="hi", query_agent_id="q1", provider="bogus")

        async def _run():
            with pytest.raises(ValueError, match="Unknown provider"):
                await pipeline.run_query(req)
            pipeline._run_scope_agent.assert_not_awaited()
            pipeline._run_query_agent.assert_not_awaited()

        import asyncio
        asyncio.run(_run())

    def test_tinfoil_client_uses_configured_base_url(self):
        pipeline = self._bare_pipeline(tinfoil="tk_test")
        tinfoil_client = pipeline.llm_clients["tinfoil"]
        # AsyncOpenAI exposes ``base_url`` (httpx URL) — compare as string.
        assert "tinfoil.sh" in str(tinfoil_client.base_url)


class TestTrackedRunFailsClosedOnScopeError:
    """C1 regression: when a scope agent is configured for a tracked run,
    a scope failure must mark the run failed — not silently fall through to
    SCOPED tools with scope_fn=None (which passes rows through unfiltered).
    """

    @pytest.mark.asyncio
    async def test_scope_agent_failure_fails_the_run(self, monkeypatch):
        """Scope agent raising ValueError must surface as run status=failed,
        and the query agent must never be invoked (no LLM spend, no SQL)."""
        from hivemind.sandbox.agents import AgentStore
        from hivemind.config import Settings

        settings = Settings(database_url="unused", llm_api_key="test")
        # Stand up a Pipeline without touching Postgres.
        pipeline = Pipeline(settings, MagicMock(spec=Database), MagicMock(spec=AgentStore))

        pipeline._run_scope_agent = AsyncMock(
            side_effect=ValueError("scope agent produced unparseable output"),
        )
        # If C1 regresses, this gets awaited — assert below that it never is.
        pipeline._run_query_agent = AsyncMock(
            return_value=("output", {"total_tokens": 0}),
        )
        pipeline._build_run_attestation = MagicMock(return_value=None)

        captured: dict = {}

        class FakeRunStore:
            def update_status(self, run_id, status, **kwargs):
                captured.setdefault("statuses", []).append(status)
                captured["last_kwargs"] = kwargs

            def update_stage(self, *args, **kwargs):
                pass

        await pipeline.run_query_agent_tracked(
            agent_id="q1",
            run_id="run-c1",
            run_store=FakeRunStore(),
            prompt="anything",
            scope_agent_id="scope-broken",
        )

        # Run must end in failed (not completed). The query agent must never
        # have been called — the whole point of fail-closed is that the
        # downstream stage doesn't run with unscoped tools.
        assert captured["statuses"][-1] == "failed"
        pipeline._run_query_agent.assert_not_awaited()
        # Error message must mention scope so operators can diagnose.
        assert "scope" in (captured["last_kwargs"].get("error") or "").lower()

    @pytest.mark.asyncio
    async def test_default_scope_agent_failure_fails_the_run(self, monkeypatch):
        """Same fail-closed semantics when scope is the configured default,
        not an explicit per-run override."""
        from hivemind.sandbox.agents import AgentStore
        from hivemind.config import Settings

        settings = Settings(
            database_url="unused",
            llm_api_key="test",
            default_scope_agent="scope-default",
        )
        pipeline = Pipeline(settings, MagicMock(spec=Database), MagicMock(spec=AgentStore))

        pipeline._run_scope_agent = AsyncMock(
            side_effect=ValueError("budget exhausted"),
        )
        pipeline._run_query_agent = AsyncMock(
            return_value=("output", {"total_tokens": 0}),
        )
        pipeline._build_run_attestation = MagicMock(return_value=None)

        captured: dict = {}

        class FakeRunStore:
            def update_status(self, run_id, status, **kwargs):
                captured.setdefault("statuses", []).append(status)
                captured["last_kwargs"] = kwargs

            def update_stage(self, *args, **kwargs):
                pass

        await pipeline.run_query_agent_tracked(
            agent_id="q1",
            run_id="run-c1-default",
            run_store=FakeRunStore(),
            prompt="anything",
            # No scope_agent_id — relies on default_scope_agent.
        )

        assert captured["statuses"][-1] == "failed"
        pipeline._run_query_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tracked_run_passes_policy_to_scope_agent(self):
        """Tracked async runs must preserve the per-request policy context."""
        from hivemind.sandbox.agents import AgentStore
        from hivemind.config import Settings

        settings = Settings(database_url="unused", llm_api_key="test")
        pipeline = Pipeline(settings, MagicMock(spec=Database), MagicMock(spec=AgentStore))
        pipeline._run_scope_agent = AsyncMock(
            side_effect=ValueError("stop after scope request capture"),
        )
        pipeline._run_query_agent = AsyncMock(
            return_value=("output", {"total_tokens": 0}),
        )
        pipeline._build_run_attestation = MagicMock(return_value=None)

        class FakeRunStore:
            def update_status(self, *args, **kwargs):
                pass

            def update_stage(self, *args, **kwargs):
                pass

        await pipeline.run_query_agent_tracked(
            agent_id="q1",
            run_id="run-policy",
            run_store=FakeRunStore(),
            prompt="anything",
            scope_agent_id="scope-policy",
            policy="Only use rows from the last 30 days.",
        )

        req_for_scope = pipeline._run_scope_agent.await_args.args[0]
        assert req_for_scope.policy == "Only use rows from the last 30 days."
        pipeline._run_query_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tracked_query_stage_passes_agent_store_for_image_rebuild(
        self, monkeypatch
    ):
        """Room runs must let SandboxBackend rebuild uploaded images after redeploy."""
        from hivemind.config import Settings

        settings = Settings(
            database_url="unused",
            llm_api_key="test",
            default_mediator_agent="",
        )
        agent_store = MagicMock(spec=AgentStore)
        agent_store.get.return_value = AgentConfig(
            agent_id="query-agent",
            name="query",
            description="",
            agent_type="query",
            image="hivemind-agent-tenant-query:latest",
        )
        pipeline = Pipeline(settings, MagicMock(spec=Database), agent_store)
        pipeline._run_scope_agent = AsyncMock(
            return_value=(
                lambda sql, params, rows: {"allow": True, "rows": rows},
                "def scope(sql, params, rows): return {'allow': True, 'rows': rows}",
                {"total_tokens": 0},
            )
        )
        pipeline._build_run_attestation = MagicMock(return_value={"signed": True})

        captured: dict = {}

        class FakeBackend:
            def __init__(
                self,
                llm_client,
                llm_model,
                settings,
                agent,
                agent_store=None,
            ):
                captured["agent_store"] = agent_store

            async def run(self, **kwargs):
                return "ok", {"total_tokens": 0}

        class FakeRunStore:
            def update_status(self, *args, **kwargs):
                captured.setdefault("statuses", []).append(args[1])

            def update_stage(self, *args, **kwargs):
                pass

        monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)

        await pipeline.run_query_agent_tracked(
            agent_id="query-agent",
            run_id="run-rebuild",
            run_store=FakeRunStore(),
            prompt="top hashtags",
            scope_agent_id="scope-agent",
            allowed_llm_providers=[],
        )

        assert captured["agent_store"] is agent_store
        assert captured["statuses"][-1] == "completed"


class TestQueryRequestProvider:
    def test_provider_field_default_none(self):
        req = QueryRequest(query="hi")
        assert req.provider is None

    def test_provider_field_round_trip(self):
        req = QueryRequest(query="hi", provider="tinfoil")
        assert req.provider == "tinfoil"

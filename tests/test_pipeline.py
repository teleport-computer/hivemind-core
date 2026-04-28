"""Tests for the Pipeline orchestrator with mocked Docker."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import hivemind.pipeline as pipeline_module
from hivemind.config import Settings
from hivemind.db import Database
from hivemind.models import IndexRequest, QueryRequest, StoreRequest
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
        req = QueryRequest(query="What?", query_agent_id="nonexistent")
        with pytest.raises(ValueError, match="not found"):
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
    async def test_mediator_failure_does_not_fail_query(self, pg_db):
        pipeline = _make_pipeline(pg_db)
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
        resp = await pipeline.run_query(req)

        assert resp.output == "raw output"
        assert resp.mediated is False

    @pytest.mark.asyncio
    async def test_mediator_agent_not_found_still_raises(self, pg_db):
        pipeline = _make_pipeline(pg_db)
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
        resp = await pipeline.run_query(req)

        pipeline._run_mediator_agent.assert_not_called()
        assert resp.output == "raw output"
        assert resp.mediated is False
        assert resp.usage["total_tokens"] == 49
        assert resp.usage["max_tokens"] == 50


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


class TestRunIndex:
    @pytest.mark.asyncio
    async def test_index_requires_agent(self, pg_db):
        pipeline = _make_pipeline(pg_db)
        req = IndexRequest(data="Some document text")
        with pytest.raises(ValueError, match="No index agent"):
            await pipeline.run_index(req)

    @pytest.mark.asyncio
    async def test_index_agent_not_found(self, pg_db):
        pipeline = _make_pipeline(pg_db)
        req = IndexRequest(data="Some document text", index_agent_id="nonexistent")
        with pytest.raises(ValueError, match="not found"):
            await pipeline.run_index(req)

    @pytest.mark.asyncio
    async def test_index_basic(self, pg_db, monkeypatch):
        """Index pipeline runs agent and parses JSON output."""
        pipeline = _make_pipeline(pg_db)
        pipeline.agent_store.create(AgentConfig(
            agent_id="idx1",
            name="Index Agent",
            image="img:index",
        ))

        expected_output = json.dumps({
            "index_text": "Title: Test Doc\nSummary: A test document.",
            "metadata": {"title": "Test Doc", "tags": ["test"]},
        })

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                return expected_output, {"total_tokens": 50}

        monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)

        req = IndexRequest(
            data="This is a test document.",
            metadata={"source": "test"},
            index_agent_id="idx1",
        )
        resp = await pipeline.run_index(req)

        assert resp.index_text == "Title: Test Doc\nSummary: A test document."
        assert resp.metadata == {"title": "Test Doc", "tags": ["test"]}
        assert resp.usage["total_tokens"] == 50

    @pytest.mark.asyncio
    async def test_index_invalid_output(self, pg_db, monkeypatch):
        """Index agent returning invalid JSON should raise ValueError."""
        pipeline = _make_pipeline(pg_db)
        pipeline.agent_store.create(AgentConfig(
            agent_id="idx2",
            name="Index Agent",
            image="img:index",
        ))

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                return "not valid json", {"total_tokens": 10}

        monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)

        req = IndexRequest(data="doc text", index_agent_id="idx2")
        with pytest.raises(ValueError, match="Index agent failed"):
            await pipeline.run_index(req)

    @pytest.mark.asyncio
    async def test_index_missing_index_text(self, pg_db, monkeypatch):
        """Index agent returning JSON without index_text should raise ValueError."""
        pipeline = _make_pipeline(pg_db)
        pipeline.agent_store.create(AgentConfig(
            agent_id="idx3",
            name="Index Agent",
            image="img:index",
        ))

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                return json.dumps({"metadata": {}}), {"total_tokens": 10}

        monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)

        req = IndexRequest(data="doc text", index_agent_id="idx3")
        with pytest.raises(ValueError, match="index_text must be a non-empty string"):
            await pipeline.run_index(req)

    @pytest.mark.asyncio
    async def test_index_uses_default_agent(self, pg_db, monkeypatch):
        """Index pipeline uses default agent when none specified."""
        settings = Settings(
            database_url="unused",
            llm_api_key="test",
            default_index_agent="default-idx",
        )
        pipeline = Pipeline(settings, pg_db, AgentStore(pg_db))
        pipeline.agent_store.create(AgentConfig(
            agent_id="default-idx",
            name="Default Index",
            image="img:index",
        ))

        expected_output = json.dumps({
            "index_text": "indexed",
            "metadata": {},
        })

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                return expected_output, {"total_tokens": 5}

        monkeypatch.setattr(pipeline_module, "SandboxBackend", FakeBackend)

        req = IndexRequest(data="doc text")
        resp = await pipeline.run_index(req)
        assert resp.index_text == "indexed"


class TestProviderRouting:
    """Pipeline.``_client_for`` decides which AsyncOpenAI client a request uses.

    These tests exercise the routing without needing a Postgres DB by
    monkey-patching ``Pipeline.__init__`` to skip the bootstrap; we just
    poke the resolved client dict directly.
    """

    def _bare_pipeline(self, *, tinfoil: str = "") -> Pipeline:
        """Build a Pipeline without touching Postgres."""
        from unittest.mock import MagicMock
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
        from unittest.mock import MagicMock
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

    def test_run_index_eager_validates_unknown_provider(self):
        pipeline = self._bare_pipeline()
        pipeline._run_index_agent = AsyncMock()

        req = IndexRequest(data="x", index_agent_id="i1", provider="bogus")

        async def _run():
            with pytest.raises(ValueError, match="Unknown provider"):
                await pipeline.run_index(req)
            pipeline._run_index_agent.assert_not_awaited()

        import asyncio
        asyncio.run(_run())

    def test_tinfoil_client_uses_configured_base_url(self):
        pipeline = self._bare_pipeline(tinfoil="tk_test")
        tinfoil_client = pipeline.llm_clients["tinfoil"]
        # AsyncOpenAI exposes ``base_url`` (httpx URL) — compare as string.
        assert "tinfoil.sh" in str(tinfoil_client.base_url)


class TestQueryRequestProvider:
    def test_provider_field_default_none(self):
        req = QueryRequest(query="hi")
        assert req.provider is None

    def test_provider_field_round_trip(self):
        req = QueryRequest(query="hi", provider="tinfoil")
        assert req.provider == "tinfoil"


class TestIndexRequestProvider:
    def test_provider_field_default_none(self):
        req = IndexRequest(data="x")
        assert req.provider is None

    def test_provider_field_round_trip(self):
        req = IndexRequest(data="x", provider="tinfoil")
        assert req.provider == "tinfoil"

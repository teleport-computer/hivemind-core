"""Tests for Hivemind core integration with Database and pipeline."""
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import hivemind.core as core_module
from hivemind.config import Settings
from hivemind.core import Hivemind
from hivemind.db import Database
from hivemind.version import APP_VERSION


@pytest.fixture
def pg_db():
    """Create a test Postgres Database."""
    test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
    if not test_dsn:
        pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")
    db = Database(test_dsn)
    yield db
    db.close()


@pytest.fixture
def hivemind_instance():
    """Create a Hivemind instance with test Postgres."""
    test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
    if not test_dsn:
        pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")
    settings = Settings(
        database_url=test_dsn,
        llm_api_key="test",
    )
    hm = Hivemind(settings)
    yield hm
    hm.db.close()


class TestDatabaseInit:
    def test_bootstrap_creates_internal_tables(self, pg_db):
        # Internal tables should exist after Database.__init__
        rows = pg_db.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name LIKE '_hivemind_%%'"
        )
        table_names = {r["table_name"] for r in rows}
        assert "_hivemind_agents" in table_names
        assert "_hivemind_agent_files" in table_names

    def test_get_schema_excludes_internal(self, pg_db):
        schema = pg_db.get_schema(exclude_internal=True)
        for row in schema:
            assert not row["table_name"].startswith("_hivemind_")

    def test_get_schema_includes_internal_when_requested(self, pg_db):
        schema = pg_db.get_schema(exclude_internal=False)
        table_names = {r["table_name"] for r in schema}
        assert "_hivemind_agents" in table_names


class TestHivemindHealth:
    def test_health_returns_status(self, hivemind_instance):
        health = hivemind_instance.health()
        assert health["status"] == "ok"
        assert health["version"] == APP_VERSION
        assert isinstance(health["table_count"], int)


class TestHivemindComponents:
    def test_db_is_accessible(self, hivemind_instance):
        assert hivemind_instance.db is not None

    def test_agent_store_is_accessible(self, hivemind_instance):
        assert hivemind_instance.agent_store is not None

    def test_pipeline_is_accessible(self, hivemind_instance):
        assert hivemind_instance.pipeline is not None


class TestDefaultAgentAutoload:
    def test_autoload_registers_stable_defaults(self, monkeypatch):
        test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
        if not test_dsn:
            pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")

        calls: list[str] = []

        class FakeRunner:
            def __init__(self, settings):
                self.settings = settings

            def cleanup_stale_containers(self):
                return None

            def image_exists(self, image):
                return True

            def extract_image_files(self, image, **kwargs):
                calls.append(image)
                return {"agent.py": f"# {image}"}

        monkeypatch.setattr("hivemind.core._create_runner", lambda settings: FakeRunner(settings))

        settings = Settings(
            database_url=test_dsn,
            llm_api_key="test",
            autoload_default_agents=True,
            default_index_image="img/default-index:v1",
            default_scope_image="img/default-scope:v1",
            default_query_image="img/default-query:v1",
            max_llm_calls=77,
            max_tokens=222_222,
            agent_timeout=456,
        )
        hm = Hivemind(settings)
        try:
            assert settings.default_index_agent == "default-index"
            assert settings.default_scope_agent == "default-scope"
            assert settings.default_query_agent == "default-query"

            assert hm.agent_store.get("default-index").image == "img/default-index:v1"
            assert hm.agent_store.get("default-scope").image == "img/default-scope:v1"
            assert hm.agent_store.get("default-query").image == "img/default-query:v1"
            assert hm.agent_store.get("default-index").max_llm_calls == 77
            assert hm.agent_store.get("default-index").max_tokens == 222_222
            assert hm.agent_store.get("default-index").timeout_seconds == 456

            assert len(hm.agent_store.list_file_paths("default-index")) == 1
            assert len(hm.agent_store.list_file_paths("default-scope")) == 1
            assert len(hm.agent_store.list_file_paths("default-query")) == 1
        finally:
            for aid in ("default-index", "default-scope", "default-query"):
                hm.agent_store.delete(aid)
            hm.db.close()

    def test_autoload_disabled_does_not_register(self, monkeypatch):
        test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
        if not test_dsn:
            pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")

        runner = MagicMock()
        monkeypatch.setattr("hivemind.core._create_runner", lambda settings: runner)

        settings = Settings(
            database_url=test_dsn,
            llm_api_key="test",
            autoload_default_agents=False,
            default_index_agent="default-index",
            default_index_image="img/default-index:v1",
        )
        hm = Hivemind(settings)
        try:
            assert hm.agent_store.get("default-index") is None
        finally:
            hm.db.close()


@pytest.mark.asyncio
async def test_hivemind_close_closes_llm_client_and_db():
    test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
    if not test_dsn:
        pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")

    settings = Settings(
        database_url=test_dsn,
        llm_api_key="test",
    )
    hm = Hivemind(settings)

    original_db_close = hm.db.close
    hm.db.close = MagicMock()
    hm.pipeline.llm_client = AsyncMock()

    try:
        await hm.close()
    finally:
        original_db_close()

    hm.pipeline.llm_client.close.assert_awaited_once()
    hm.db.close.assert_called_once()

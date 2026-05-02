"""Tests for Hivemind core integration with Database and pipeline."""
import os
import secrets
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from psycopg import conninfo as pg_conninfo

from hivemind.config import Settings
from hivemind.core import Hivemind
from hivemind.db import Database
from hivemind.version import APP_VERSION


DEFAULT_AGENT_IDS = (
    "default-scope",
    "default-query",
    "default-mediator",
)


def _dsn_for_db(dsn: str, db_name: str) -> str:
    parsed = pg_conninfo.conninfo_to_dict(dsn)
    parsed["dbname"] = db_name
    return pg_conninfo.make_conninfo(**parsed)


def _drop_db(dsn: str, db_name: str) -> None:
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    except Exception:
        pass


def _clear_default_agents(dsn: str) -> None:
    db = Database(dsn)
    try:
        for agent_id in DEFAULT_AGENT_IDS:
            db.execute_commit(
                "DELETE FROM _hivemind_agent_files WHERE agent_id = %s",
                [agent_id],
            )
            db.execute_commit(
                "DELETE FROM _hivemind_agents WHERE agent_id = %s",
                [agent_id],
            )
    finally:
        db.close()


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
            "WHERE table_schema = 'public' AND left(table_name, 10) = %s",
            ["_hivemind_"],
        )
        table_names = {r["table_name"] for r in rows}
        assert "_hivemind_agents" in table_names
        assert "_hivemind_agent_files" in table_names

    def test_bootstrap_migrates_legacy_query_runs_before_indexes(self):
        test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
        if not test_dsn:
            pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")

        db_name = f"hm_legacy_{secrets.token_hex(4)}"
        with psycopg.connect(test_dsn, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{db_name}"')

        legacy_dsn = _dsn_for_db(test_dsn, db_name)
        try:
            with psycopg.connect(legacy_dsn, autocommit=True) as conn:
                conn.execute(
                    """
                    CREATE TABLE _hivemind_query_runs (
                        run_id TEXT PRIMARY KEY,
                        agent_id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at DOUBLE PRECISION NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL
                    )
                    """
                )

            db = Database(legacy_dsn)
            try:
                columns = {
                    row["column_name"]
                    for row in db.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = '_hivemind_query_runs'"
                    )
                }
                assert "room_id" in columns
                assert "room_manifest_hash" in columns
                assert "prompt" in columns
                assert "issuer_token_id" in columns
                assert "payer_tenant_id" in columns
                assert "billing_cost_micro_usd" in columns
                assert "usage_json" in columns
                assert "output_visibility" in columns
                assert "artifacts_enabled" in columns

                indexes = {
                    row["indexname"]
                    for row in db.execute(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE tablename = '_hivemind_query_runs'"
                    )
                }
                assert "_hivemind_query_runs_room_idx" in indexes
            finally:
                db.close()
        finally:
            _drop_db(test_dsn, db_name)

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
        _clear_default_agents(test_dsn)

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

        monkeypatch.setattr(
            "hivemind.core._create_runner",
            lambda settings: FakeRunner(settings),
        )

        settings = Settings(
            database_url=test_dsn,
            llm_api_key="test",
            autoload_default_agents=True,
            default_scope_image="img/default-scope:v1",
            default_query_image="img/default-query:v1",
            max_llm_calls=77,
            max_tokens=222_222,
            agent_timeout=456,
        )
        hm = Hivemind(settings)
        try:
            assert settings.default_scope_agent == "default-scope"
            assert settings.default_query_agent == "default-query"

            assert hm.agent_store.get("default-scope").image == "img/default-scope:v1"
            assert hm.agent_store.get("default-query").image == "img/default-query:v1"
            assert hm.agent_store.get("default-scope").max_llm_calls == 77
            assert hm.agent_store.get("default-scope").max_tokens == 222_222
            assert hm.agent_store.get("default-scope").timeout_seconds == 456

            assert len(hm.agent_store.list_file_paths("default-scope")) == 1
            assert len(hm.agent_store.list_file_paths("default-query")) == 1
        finally:
            hm.db.close()
            _clear_default_agents(test_dsn)

    def test_autoload_disabled_does_not_register(self, monkeypatch):
        test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
        if not test_dsn:
            pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")
        _clear_default_agents(test_dsn)

        runner = MagicMock()
        runner.cleanup_stale_containers.return_value = None
        monkeypatch.setattr(
            "hivemind.core._create_runner",
            lambda _settings: runner,
        )

        settings = Settings(
            database_url=test_dsn,
            llm_api_key="test",
            autoload_default_agents=False,
            default_scope_agent="default-scope",
            default_scope_image="img/default-scope:v1",
        )
        hm = Hivemind(settings)
        try:
            assert hm.agent_store.get("default-scope") is None
            runner.image_exists.assert_not_called()
            runner.extract_image_files.assert_not_called()
        finally:
            hm.db.close()
            _clear_default_agents(test_dsn)


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
    mock_client = AsyncMock()
    hm.pipeline.llm_clients = {"openrouter": mock_client}

    try:
        await hm.close()
    finally:
        original_db_close()

    mock_client.close.assert_awaited_once()
    hm.db.close.assert_called_once()

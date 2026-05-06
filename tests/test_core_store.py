"""Tests for Hivemind core integration with Database and pipeline."""
import os
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from psycopg import conninfo as pg_conninfo

from hivemind.config import Settings
from hivemind.core import Hivemind
from hivemind.db import Database
from hivemind.version import APP_VERSION


DEFAULT_AGENT_IDS = (
    "default-index",
    "default-scope",
    "default-query",
    "default-mediator",
    "default-index-hermes",
    "default-scope-hermes",
    "default-query-hermes",
    "default-mediator-hermes",
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

    def test_bootstrap_normalizes_legacy_agent_harness_values(self):
        test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
        if not test_dsn:
            pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")

        db_name = f"hm_legacy_agents_{secrets.token_hex(4)}"
        with psycopg.connect(test_dsn, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{db_name}"')

        legacy_dsn = _dsn_for_db(test_dsn, db_name)
        try:
            with psycopg.connect(legacy_dsn, autocommit=True) as conn:
                conn.execute(
                    """
                    CREATE TABLE _hivemind_agents (
                        agent_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        agent_type TEXT NOT NULL DEFAULT 'query',
                        image TEXT NOT NULL,
                        entrypoint TEXT,
                        memory_mb INTEGER NOT NULL DEFAULT 256,
                        max_llm_calls INTEGER NOT NULL DEFAULT 20,
                        max_tokens INTEGER NOT NULL DEFAULT 100000,
                        timeout_seconds INTEGER NOT NULL DEFAULT 120,
                        inspection_mode TEXT NOT NULL DEFAULT 'full',
                        harness TEXT,
                        created_at DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO _hivemind_agents
                    (agent_id, name, image, harness, created_at)
                    VALUES
                    ('legacy-null', 'legacy null', 'img:test', NULL, 1),
                    ('legacy-bogus', 'legacy bogus', 'img:test', 'bogus', 1)
                    """
                )

            db = Database(legacy_dsn)
            try:
                rows = db.execute(
                    "SELECT agent_id, harness FROM _hivemind_agents "
                    "ORDER BY agent_id"
                )
                assert rows == [
                    {"agent_id": "legacy-bogus", "harness": "claude_code"},
                    {"agent_id": "legacy-null", "harness": "claude_code"},
                ]
                with pytest.raises(psycopg.Error):
                    db.execute_commit(
                        """
                        INSERT INTO _hivemind_agents
                        (agent_id, name, description, image, harness, created_at)
                        VALUES ('legacy-bad-2', 'bad', '', 'img:test', 'bad', 1)
                        """
                    )
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

    def test_agent_harness_constraint_rejects_invalid_values(self, pg_db):
        with pytest.raises(psycopg.Error):
            pg_db.execute_commit(
                """
                INSERT INTO _hivemind_agents
                (agent_id, name, description, image, harness, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [
                    f"bad-harness-{secrets.token_hex(4)}",
                    "bad harness",
                    "",
                    "img:test",
                    "bogus",
                    1.0,
                ],
            )


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
    def test_bundled_default_agent_files_reads_local_context(self, tmp_path):
        source_dir = tmp_path / "default-query-hermes"
        source_dir.mkdir()
        (source_dir / "Dockerfile").write_text(
            "FROM hivemind-agent-base-hermes:latest\nCOPY agent.py .\n"
        )
        (source_dir / "agent.py").write_text("print('hello')\n")

        hm = object.__new__(Hivemind)
        hm.settings = Settings(
            llm_api_key="test",
            bundled_agents_dir=str(tmp_path),
        )

        files = hm._bundled_default_agent_files(
            image="hivemind-default-query-hermes:latest",
            source_name="default-query-hermes",
        )

        assert files == {
            "Dockerfile": "FROM hivemind-agent-base-hermes:latest\nCOPY agent.py .\n",
            "agent.py": "print('hello')\n",
        }

    def test_bundled_default_agent_files_ignores_registry_refs(self, tmp_path):
        source_dir = tmp_path / "default-query-hermes"
        source_dir.mkdir()
        (source_dir / "Dockerfile").write_text(
            "FROM hivemind-agent-base-hermes:latest\n"
        )

        hm = object.__new__(Hivemind)
        hm.settings = Settings(
            llm_api_key="test",
            bundled_agents_dir=str(tmp_path),
        )

        assert (
            hm._bundled_default_agent_files(
                image="ghcr.io/example/default-query-hermes:latest",
                source_name="default-query-hermes",
            )
            is None
        )

    def test_autoload_refreshes_bundled_context_with_stable_tag(self, tmp_path):
        source_dir = tmp_path / "default-query-hermes"
        source_dir.mkdir()
        (source_dir / "Dockerfile").write_text(
            "FROM hivemind-agent-base-hermes:latest\nCOPY agent.py .\n"
        )
        (source_dir / "agent.py").write_text("print('current')\n")

        class FakeStore:
            def __init__(self):
                self.upserts = []
                self.replacements = []

            def get(self, agent_id):
                assert agent_id == "default-query-hermes"
                return SimpleNamespace(
                    image="hivemind-default-query-hermes:latest"
                )

            def list_file_paths(self, agent_id):
                assert agent_id == "default-query-hermes"
                return [{"path": "agent.py", "size_bytes": 20, "attestable": True}]

            def upsert(self, config):
                self.upserts.append(config)

            def replace_files(self, agent_id, files):
                self.replacements.append((agent_id, files))

        store = FakeStore()
        hm = object.__new__(Hivemind)
        hm.settings = Settings(
            llm_api_key="test",
            autoload_default_agents=True,
            bundled_agents_dir=str(tmp_path),
            default_query_hermes_image="hivemind-default-query-hermes:latest",
        )
        hm.agent_store = store

        hm._bootstrap_default_agents()

        assert len(store.upserts) == 1
        assert store.upserts[0].harness == "hermes"
        assert store.replacements == [
            (
                "default-query-hermes",
                {
                    "Dockerfile": (
                        "FROM hivemind-agent-base-hermes:latest\nCOPY agent.py .\n"
                    ),
                    "agent.py": "print('current')\n",
                },
            )
        ]

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
            assert hm.agent_store.get("default-scope").max_llm_calls == 77
            assert hm.agent_store.get("default-scope").max_tokens == 222_222
            assert hm.agent_store.get("default-scope").timeout_seconds == 456

            assert len(hm.agent_store.list_file_paths("default-index")) == 1
            assert len(hm.agent_store.list_file_paths("default-scope")) == 1
            assert len(hm.agent_store.list_file_paths("default-query")) == 1
        finally:
            hm.db.close()
            _clear_default_agents(test_dsn)

    def test_hermes_defaults_autoload_with_harness(self, monkeypatch):
        test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
        if not test_dsn:
            pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")
        _clear_default_agents(test_dsn)

        class FakeRunner:
            def __init__(self, settings):
                self.settings = settings

            def cleanup_stale_containers(self):
                return None

            def image_exists(self, image):
                return True

            def extract_image_files(self, image, **kwargs):
                return {"agent.py": f"# {image}"}

        monkeypatch.setattr(
            "hivemind.core._create_runner",
            lambda settings: FakeRunner(settings),
        )

        settings = Settings(
            database_url=test_dsn,
            llm_api_key="test",
            autoload_default_agents=True,
            default_index_hermes_image="img/default-index-hermes:v1",
            default_scope_hermes_image="img/default-scope-hermes:v1",
            default_query_hermes_image="img/default-query-hermes:v1",
            default_mediator_hermes_image="img/default-mediator-hermes:v1",
        )
        hm = Hivemind(settings)
        try:
            assert settings.default_index_hermes_agent == "default-index-hermes"
            assert settings.default_scope_hermes_agent == "default-scope-hermes"
            assert settings.default_query_hermes_agent == "default-query-hermes"
            assert (
                settings.default_mediator_hermes_agent
                == "default-mediator-hermes"
            )

            for agent_id in (
                "default-index-hermes",
                "default-scope-hermes",
                "default-query-hermes",
                "default-mediator-hermes",
            ):
                agent = hm.agent_store.get(agent_id)
                assert agent is not None
                assert agent.harness == "hermes"
                assert len(hm.agent_store.list_file_paths(agent_id)) == 1
        finally:
            hm.db.close()
            _clear_default_agents(test_dsn)

    def test_autoload_pulls_missing_configured_image(self, monkeypatch):
        test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
        if not test_dsn:
            pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")
        _clear_default_agents(test_dsn)

        calls = {"pull": [], "extract": []}
        present: set[str] = set()

        class FakeRunner:
            def __init__(self, settings):
                self.settings = settings

            def cleanup_stale_containers(self):
                return None

            def image_exists(self, image):
                return image in present

            def pull_image(self, image):
                calls["pull"].append(image)
                present.add(image)
                return True

            def extract_image_files(self, image, **kwargs):
                calls["extract"].append(image)
                return {"agent.py": f"# {image}"}

        monkeypatch.setattr(
            "hivemind.core._create_runner",
            lambda settings: FakeRunner(settings),
        )

        settings = Settings(
            database_url=test_dsn,
            llm_api_key="test",
            autoload_default_agents=True,
            default_query_hermes_image="ghcr.io/example/custom-query-hermes:latest",
        )
        hm = Hivemind(settings)
        try:
            assert calls["pull"] == ["ghcr.io/example/custom-query-hermes:latest"]
            assert calls["extract"] == calls["pull"]
            agent = hm.agent_store.get("default-query-hermes")
            assert agent is not None
            assert agent.harness == "hermes"
        finally:
            hm.db.close()
            _clear_default_agents(test_dsn)

    def test_autoload_stores_bundled_default_context_without_docker(
        self, monkeypatch, tmp_path
    ):
        test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
        if not test_dsn:
            pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")
        _clear_default_agents(test_dsn)

        source_dir = tmp_path / "default-query-hermes"
        source_dir.mkdir()
        (source_dir / "Dockerfile").write_text(
            "FROM hivemind-agent-base-hermes:latest\nCOPY agent.py .\n"
        )
        (source_dir / "agent.py").write_text("print('hello')\n")

        calls = {"build": [], "pull": [], "extract": []}
        present: set[str] = set()

        class FakeRunner:
            def __init__(self, settings):
                self.settings = settings

            def cleanup_stale_containers(self):
                return None

            def image_exists(self, image):
                return image in present

            def build_image(self, build_path, tag):
                calls["build"].append((build_path, tag))
                present.add(tag)
                return tag

            def pull_image(self, image):
                calls["pull"].append(image)
                return False

            def extract_image_files(self, image, **kwargs):
                calls["extract"].append(image)
                return {"agent.py": f"# {image}"}

        monkeypatch.setattr(
            "hivemind.core._create_runner",
            lambda settings: FakeRunner(settings),
        )

        settings = Settings(
            database_url=test_dsn,
            llm_api_key="test",
            autoload_default_agents=True,
            bundled_agents_dir=str(tmp_path),
            default_query_hermes_image="hivemind-default-query-hermes:latest",
        )
        hm = Hivemind(settings)
        try:
            assert calls["build"] == []
            assert calls["pull"] == []
            assert calls["extract"] == []
            agent = hm.agent_store.get("default-query-hermes")
            assert agent is not None
            assert agent.harness == "hermes"
            assert sorted(hm.agent_store.list_file_paths(agent.agent_id)) == [
                "Dockerfile",
                "agent.py",
            ]
        finally:
            hm.db.close()
            _clear_default_agents(test_dsn)

    def test_bundled_default_build_failure_is_fatal(self, tmp_path):
        source_dir = tmp_path / "default-query-hermes"
        source_dir.mkdir()
        (source_dir / "Dockerfile").write_text(
            "FROM hivemind-agent-base-hermes:latest\nCOPY agent.py .\n"
        )
        (source_dir / "agent.py").write_text("print('hello')\n")

        class FakeRunner:
            def build_image(self, build_path, tag):
                raise RuntimeError("base image missing")

        settings = Settings(
            llm_api_key="test",
            bundled_agents_dir=str(tmp_path),
        )
        hm = object.__new__(Hivemind)
        hm.settings = settings

        with pytest.raises(RuntimeError, match="Bundled default agent build failed"):
            hm._build_bundled_default_agent_image(
                FakeRunner(),
                image="hivemind-default-query-hermes:latest",
                source_name="default-query-hermes",
            )

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

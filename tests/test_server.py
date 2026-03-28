"""Tests for FastAPI server endpoints."""

import os
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

from hivemind.config import Settings
from hivemind.core import Hivemind
from hivemind.server import create_app


# ── Fixtures ──


@pytest.fixture
def test_dsn():
    dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")
    return dsn


@pytest.fixture
def settings(test_dsn):
    """Settings pointing at test Postgres, no auth, no autoload."""
    return Settings(
        database_url=test_dsn,
        api_key="",
        autoload_default_agents=False,
    )


@pytest.fixture
def settings_with_auth(test_dsn):
    """Settings with API key auth enabled."""
    return Settings(
        database_url=test_dsn,
        api_key="test-secret-key",
        autoload_default_agents=False,
    )


@pytest_asyncio.fixture
async def client(settings):
    app = create_app(settings)
    hm = Hivemind(settings)
    app.state.hivemind = hm
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await hm.close()


@pytest_asyncio.fixture
async def auth_client(settings_with_auth):
    app = create_app(settings_with_auth)
    hm = Hivemind(settings_with_auth)
    app.state.hivemind = hm
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await hm.close()


# ── Health ──


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_200(self, client):
        resp = await client.get("/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "table_count" in data
        assert "version" in data

    @pytest.mark.asyncio
    async def test_health_version_format(self, client):
        resp = await client.get("/v1/health")
        data = resp.json()
        assert isinstance(data["version"], str)
        assert len(data["version"]) > 0


# ── Auth ──


class TestAuth:
    @pytest.mark.asyncio
    async def test_no_auth_required_when_no_key(self, client):
        resp = await client.get("/v1/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_auth_required_missing_header(self, auth_client):
        resp = await auth_client.post(
            "/v1/store", json={"sql": "SELECT 1"}
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_required_wrong_key(self, auth_client):
        resp = await auth_client.post(
            "/v1/store",
            json={"sql": "SELECT 1"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_passes_with_correct_key(self, auth_client):
        resp = await auth_client.post(
            "/v1/store",
            json={"sql": "SELECT 1 AS val"},
            headers={"Authorization": "Bearer test-secret-key"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_health_no_auth_even_when_key_set(self, auth_client):
        # Health endpoint has no auth dependency — but actually it does
        # depend on Hivemind being initialized. Check if it requires auth.
        resp = await auth_client.get("/v1/health")
        # Health uses Depends(get_hivemind) but not Depends(check_auth)
        # so it should work without auth
        assert resp.status_code == 200


# ── Store ──


class TestStore:
    @pytest.mark.asyncio
    async def test_store_select(self, client):
        resp = await client.post("/v1/store", json={"sql": "SELECT 1 AS val"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["rows"] == [{"val": 1}]

    @pytest.mark.asyncio
    async def test_store_create_insert_read(self, client):
        # Create table
        resp = await client.post(
            "/v1/store",
            json={"sql": "CREATE TABLE IF NOT EXISTS test_server_data (id SERIAL PRIMARY KEY, val TEXT)"},
        )
        assert resp.status_code == 200

        try:
            # Insert
            resp = await client.post(
                "/v1/store",
                json={"sql": "INSERT INTO test_server_data (val) VALUES (%s)", "params": ["hello"]},
            )
            assert resp.status_code == 200
            assert resp.json()["rowcount"] == 1

            # Read back
            resp = await client.post(
                "/v1/store",
                json={"sql": "SELECT val FROM test_server_data"},
            )
            assert resp.status_code == 200
            assert resp.json()["rows"] == [{"val": "hello"}]
        finally:
            await client.post("/v1/store", json={"sql": "DROP TABLE IF EXISTS test_server_data"})

    @pytest.mark.asyncio
    async def test_store_empty_body_422(self, client):
        resp = await client.post("/v1/store", json={})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_store_missing_sql_422(self, client):
        resp = await client.post("/v1/store", json={"params": [1]})
        assert resp.status_code == 422


# ── Query ──


class TestQuery:
    @pytest.mark.asyncio
    async def test_query_empty_body_422(self, client):
        resp = await client.post("/v1/query", json={})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_query_no_agent_400(self, client):
        resp = await client.post(
            "/v1/query", json={"query": "What happened?"}
        )
        # No default query agent configured → 400
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_query_prompt_alias(self, client):
        resp = await client.post(
            "/v1/query", json={"prompt": "What happened?"}
        )
        # Should accept prompt as alias for query, still fail on no agent
        assert resp.status_code == 400


# ── Admin Schema ──


class TestAdminSchema:
    @pytest.mark.asyncio
    async def test_get_schema(self, client):
        resp = await client.get("/v1/admin/schema")
        assert resp.status_code == 200
        data = resp.json()
        assert "schema" in data
        assert isinstance(data["schema"], list)


# ── Agent CRUD ──


# ── Index ──


class TestIndex:
    @pytest.mark.asyncio
    async def test_index_empty_body_422(self, client):
        resp = await client.post("/v1/index", json={})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_index_no_agent_400(self, client):
        resp = await client.post(
            "/v1/index", json={"data": "Some document text"}
        )
        # No default index agent configured → 400
        assert resp.status_code == 400


# ── Agent CRUD ──


class TestAgentCRUD:
    @pytest.mark.asyncio
    async def test_list_agents(self, client):
        resp = await client.get("/v1/agents")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_get_nonexistent_agent_404(self, client):
        resp = await client.get("/v1/agents/nonexistent-agent-id")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_nonexistent_agent_404(self, client):
        resp = await client.delete("/v1/agents/nonexistent-agent-id")
        assert resp.status_code == 404

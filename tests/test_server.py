"""Tests for FastAPI server endpoints."""

from __future__ import annotations

import os
import secrets

import httpx
import psycopg
import pytest
import pytest_asyncio
from psycopg import sql as psql

from hivemind.config import Settings
from hivemind.server import create_app
from hivemind.tenants import TenantRegistry


@pytest.fixture
def test_dsn():
    dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")
    return dsn


def _unique_db(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def _drop_db(dsn: str, db_name: str) -> None:
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                psql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    psql.Identifier(db_name)
                )
            )
    except Exception:
        pass


def _owner_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


@pytest_asyncio.fixture
async def server_env(test_dsn):
    """FastAPI client backed by a real tenant registry and tenant DB."""
    control_db = _unique_db("hm_ctrl")
    settings = Settings(
        database_url=test_dsn,
        control_database=control_db,
        admin_key="admin-test-key",
        sql_proxy_admin_key="",
        autoload_default_agents=False,
        artifact_sweep_interval_seconds=9999,
    )

    registry = TenantRegistry(settings)
    tenant = registry.provision("server-tests")
    created_dbs = [control_db, tenant["db_name"]]

    app = create_app(settings)
    app.state.registry = registry
    app.state.background_tasks = set()
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            yield client, tenant["api_key"]
    finally:
        try:
            registry.close()
        except Exception:
            pass
        for db_name in created_dbs:
            _drop_db(test_dsn, db_name)


async def _self_serve_test_env(test_dsn, **settings_overrides):
    control_db = _unique_db("hm_selfserve")
    settings_kwargs = dict(
        database_url=test_dsn,
        control_database=control_db,
        admin_key="admin-test-key",
        sql_proxy_admin_key="",
        autoload_default_agents=False,
        artifact_sweep_interval_seconds=9999,
        self_serve_signup_enabled=True,
    )
    settings_kwargs.update(settings_overrides)
    settings = Settings(**settings_kwargs)

    registry = TenantRegistry(settings)
    created_dbs = [control_db]

    app = create_app(settings)
    app.state.registry = registry
    app.state.background_tasks = set()
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            yield client, registry
    finally:
        try:
            for tenant in registry.list_tenants():
                created_dbs.append(tenant["db_name"])
        except Exception:
            pass
        try:
            registry.close()
        except Exception:
            pass
        for db_name in created_dbs:
            _drop_db(test_dsn, db_name)


@pytest_asyncio.fixture
async def self_serve_env(test_dsn):
    """FastAPI client with public signup enabled and zero starting credit."""
    async for env in _self_serve_test_env(test_dsn):
        yield env


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_200(self, server_env):
        client, api_key = server_env
        resp = await client.get("/v1/health", headers=_owner_headers(api_key))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "table_count" in data
        assert "version" in data

    @pytest.mark.asyncio
    async def test_health_version_format(self, server_env):
        client, api_key = server_env
        resp = await client.get("/v1/health", headers=_owner_headers(api_key))
        data = resp.json()
        assert isinstance(data["version"], str)
        assert len(data["version"]) > 0


class TestAuth:
    @pytest.mark.asyncio
    async def test_owner_endpoint_requires_auth(self, server_env):
        client, _api_key = server_env
        resp = await client.get("/v1/room-agents")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_owner_endpoint_rejects_wrong_key(self, server_env):
        client, _api_key = server_env
        resp = await client.get(
            "/v1/room-agents", headers={"Authorization": "Bearer wrong-key"}
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_owner_endpoint_accepts_correct_key(self, server_env):
        client, api_key = server_env
        resp = await client.get("/v1/room-agents", headers=_owner_headers(api_key))
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_health_requires_auth(self, server_env):
        client, _api_key = server_env
        resp = await client.get("/v1/health")
        assert resp.status_code == 401


class TestRoomAgentUpload:
    @pytest.mark.asyncio
    async def test_upload_rejects_retired_index_agent_type(self, server_env):
        client, api_key = server_env
        resp = await client.post(
            "/v1/room-agents",
            headers=_owner_headers(api_key),
            data={"name": "old-index", "agent_type": "index"},
            files={"archive": ("agent.tar.gz", b"not-read", "application/gzip")},
        )

        assert resp.status_code == 400
        assert resp.json()["detail"] == "agent_type must be one of: mediator, query, scope"


class TestAdminSchema:
    @pytest.mark.asyncio
    async def test_get_schema(self, server_env):
        client, api_key = server_env
        resp = await client.get(
            "/v1/admin/schema",
            headers=_owner_headers(api_key),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "schema" in data
        assert isinstance(data["schema"], list)


class TestAdminTenants:
    @pytest.mark.asyncio
    async def test_create_rejects_duplicate_name(self, server_env):
        client, _api_key = server_env
        resp = await client.post(
            "/v1/admin/tenants",
            headers={"Authorization": "Bearer admin-test-key"},
            json={"name": "server-tests"},
        )

        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]


class TestSelfServeSignup:
    @pytest.mark.asyncio
    async def test_signup_disabled_by_default(self, server_env):
        client, _api_key = server_env
        resp = await client.post("/v1/signup", json={"name": "new-user"})

        assert resp.status_code == 503
        assert "disabled" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_signup_creates_tenant_without_free_credit_by_default(
        self,
        self_serve_env,
    ):
        client, registry = self_serve_env

        resp = await client.post("/v1/signup", json={"name": "alice"})

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["tenant_id"].startswith("t_")
        assert data["name"] == "alice"
        assert data["api_key"].startswith("hmk_")
        assert "db_name" not in data
        assert data["starter_credit_micro_usd"] == 0
        assert data["balance_micro_usd"] == 0
        assert registry.resolve(data["api_key"]) is not None

        billing = await client.get(
            "/v1/billing",
            headers=_owner_headers(data["api_key"]),
        )
        assert billing.status_code == 200, billing.text
        account = billing.json()
        assert account["tenant_id"] == data["tenant_id"]
        assert account["balance_micro_usd"] == 0
        assert account["ledger"] == []

        with_code = await client.post(
            "/v1/signup",
            json={"name": "bob", "credit_code": "hmcc_unused"},
        )
        assert with_code.status_code == 400
        assert "after signup" in with_code.json()["detail"]

    @pytest.mark.asyncio
    async def test_credit_code_recharges_existing_self_serve_user(
        self,
        self_serve_env,
    ):
        client, registry = self_serve_env

        signup = await client.post("/v1/signup", json={"name": "alice"})
        assert signup.status_code == 200, signup.text
        data = signup.json()
        assert data["balance_micro_usd"] == 0

        create = await client.post(
            "/v1/admin/credit-codes",
            headers={"Authorization": "Bearer admin-test-key"},
            json={
                "credit_usd": "3.00",
                "max_redemptions": 1,
                "label": "starter",
            },
        )
        assert create.status_code == 200, create.text
        code = create.json()
        assert code["code"].startswith("hmcc_")
        assert registry.resolve(data["api_key"]) is not None

        wrong = await client.post(
            "/v1/billing/credit-codes/redeem",
            headers=_owner_headers(data["api_key"]),
            json={"credit_code": "wrong"},
        )
        assert wrong.status_code == 403

        first_redeem = await client.post(
            "/v1/billing/credit-codes/redeem",
            headers=_owner_headers(data["api_key"]),
            json={"credit_code": code["code"]},
        )
        assert first_redeem.status_code == 200, first_redeem.text
        assert first_redeem.json()["credit_micro_usd"] == 3_000_000
        assert first_redeem.json()["balance_micro_usd"] == 3_000_000

        exhausted = await client.post(
            "/v1/billing/credit-codes/redeem",
            headers=_owner_headers(data["api_key"]),
            json={"credit_code": code["code"]},
        )
        assert exhausted.status_code == 403

        billing = await client.get(
            "/v1/billing",
            headers=_owner_headers(data["api_key"]),
        )
        assert billing.status_code == 200, billing.text
        account = billing.json()
        assert account["ledger"][0]["kind"] == "credit_grant"
        assert account["ledger"][0]["metadata"]["actor"] == "credit_code"
        assert account["ledger"][0]["metadata"]["code_id"] == code["code_id"]

        recharge_create = await client.post(
            "/v1/admin/credit-codes",
            headers={"Authorization": "Bearer admin-test-key"},
            json={
                "credit_usd": "2.00",
                "max_redemptions": 1,
                "label": "recharge",
            },
        )
        assert recharge_create.status_code == 200, recharge_create.text
        recharge = await client.post(
            "/v1/billing/credit-codes/redeem",
            headers=_owner_headers(data["api_key"]),
            json={"credit_code": recharge_create.json()["code"]},
        )
        assert recharge.status_code == 200, recharge.text
        assert recharge.json()["credit_micro_usd"] == 2_000_000
        assert recharge.json()["balance_micro_usd"] == 5_000_000

        accounts = await client.get(
            "/v1/admin/billing",
            headers={"Authorization": "Bearer admin-test-key"},
        )
        assert accounts.status_code == 200, accounts.text
        summary = accounts.json()["accounts"][0]
        assert summary["tenant_id"] == data["tenant_id"]
        assert summary["balance_micro_usd"] == 5_000_000
        assert summary["total_credit_micro_usd"] == 5_000_000
        assert summary["total_spent_micro_usd"] == 0

        ledger = await client.get(
            "/v1/admin/billing/ledger",
            headers={"Authorization": "Bearer admin-test-key"},
        )
        assert ledger.status_code == 200, ledger.text
        assert len(ledger.json()["ledger"]) == 2

        codes = await client.get(
            "/v1/admin/credit-codes",
            headers={"Authorization": "Bearer admin-test-key"},
        )
        assert codes.status_code == 200, codes.text
        assert "code" not in codes.json()["credit_codes"][0]

    @pytest.mark.asyncio
    async def test_signup_allows_duplicate_display_names(self, self_serve_env):
        client, _registry = self_serve_env

        first = await client.post("/v1/signup", json={"name": "same-name"})
        second = await client.post("/v1/signup", json={"name": "same-name"})

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["tenant_id"] != second.json()["tenant_id"]

    @pytest.mark.asyncio
    async def test_owner_billing_requires_auth(self, self_serve_env):
        client, _registry = self_serve_env

        resp = await client.get("/v1/billing")

        assert resp.status_code == 401


class TestAdminBilling:
    @pytest.mark.asyncio
    async def test_prices_route_is_not_shadowed_by_tenant_route(self, server_env):
        client, _api_key = server_env
        resp = await client.get(
            "/v1/admin/billing/prices",
            headers={"Authorization": "Bearer admin-test-key"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["prices"], list)
        assert any(p["model"] == "openai/gpt-5-mini" for p in data["prices"])


class TestAgentCRUD:
    @pytest.mark.asyncio
    async def test_list_agents(self, server_env):
        client, api_key = server_env
        resp = await client.get("/v1/room-agents", headers=_owner_headers(api_key))
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_get_nonexistent_agent_404(self, server_env):
        client, api_key = server_env
        resp = await client.get(
            "/v1/room-agents/nonexistent-agent-id",
            headers=_owner_headers(api_key),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_nonexistent_agent_404(self, server_env):
        client, api_key = server_env
        resp = await client.delete(
            "/v1/room-agents/nonexistent-agent-id",
            headers=_owner_headers(api_key),
        )
        assert resp.status_code == 404

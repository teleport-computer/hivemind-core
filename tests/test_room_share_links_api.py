"""End-to-end HTTP tests for /v1/rooms/:id/share-link + share-role asks.

Exercises the full FastAPI surface: enable → idempotent re-mint →
re-fetch → outsider tenant submits a run with the share token (paying
from their own tenant) → owner sees the run with the asker's
``payer_tenant_id`` → rotate invalidates the old token → disable kills
the link.

Postgres-backed; skips when ``HIVEMIND_TEST_DATABASE_URL`` is offline.
Mirrors tests/test_rooms.py so the fixture shape is recognizable.
"""

from __future__ import annotations

import os
import secrets

import psycopg
import pytest
from fastapi.testclient import TestClient

from hivemind.config import Settings
from hivemind.sandbox.models import AgentConfig
from hivemind.server import create_app
from hivemind.tenants import TenantRegistry


TEST_DSN = os.environ.get(
    "HIVEMIND_TEST_DATABASE_URL",
    "postgresql://hivemind:dev@localhost:5432/postgres",
)


def _pg_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(TEST_DSN),
    reason=f"Postgres not reachable at {TEST_DSN}",
)


def _unique(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def _drop_db(dsn: str, db_name: str) -> None:
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    except Exception:
        pass


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _share_headers(share_token: str, payer_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {share_token}",
        "X-Hivemind-Api-Key": payer_key,
    }


@pytest.fixture
def env():
    """Two tenants (alice, bob) and a fixed-query room owned by alice."""
    control_db = _unique("hm_share_api")
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{control_db}"')

    settings = Settings(
        database_url=TEST_DSN,
        control_database=control_db,
        admin_key="admin-test-key",
        sql_proxy_admin_key="",
        autoload_default_agents=False,
        tinfoil_api_key="tk_test",
        artifact_sweep_interval_seconds=9999,
        billing_enforce_credits=False,
    )
    registry = TenantRegistry(settings)
    from hivemind.admin_proxy import LocalPgAdmin

    registry._pg_admin = LocalPgAdmin(TEST_DSN)
    created_dbs = [control_db]
    alice = registry.provision("alice")
    bob = registry.provision("bob")
    created_dbs.append(alice["db_name"])
    created_dbs.append(bob["db_name"])

    alice_hive = registry.for_tenant(alice["tenant_id"])
    assert alice_hive is not None
    for agent_id, agent_type in (
        ("scope-a", "scope"),
        ("query-a", "query"),
        ("mediator-a", "mediator"),
    ):
        alice_hive.agent_store.create(
            AgentConfig(
                agent_id=agent_id,
                name=agent_id,
                description="fixture",
                agent_type=agent_type,
                image="hivemind-test:latest",
                entrypoint=None,
                memory_mb=256,
                max_llm_calls=10,
                max_tokens=10_000,
                timeout_seconds=60,
                inspection_mode="full",
            )
        )
        alice_hive.agent_store.save_files(
            agent_id,
            {
                "Dockerfile": "FROM python:3.12-slim\n",
                "agent.py": "print('x')\n",
            },
            inspection_mode="full",
        )

    app = create_app(settings)
    app.state.registry = registry
    app.state.background_tasks = set()
    client = TestClient(app, base_url="http://share")

    # alice creates a fixed-query room
    create_resp = client.post(
        "/v1/rooms",
        json={
            "name": "negotiation",
            "rules": "be terse",
            "scope_agent_id": "scope-a",
            "query_mode": "fixed",
            "query_agent_id": "query-a",
            "mediator_agent_id": "mediator-a",
            "output_visibility": "owner_and_querier",
            "egress": {"llm_providers": ["tinfoil"], "allow_artifacts": False},
        },
        headers=_headers(alice["api_key"]),
    )
    assert create_resp.status_code == 200, create_resp.text
    room_id = create_resp.json()["room_id"]

    yield client, alice, bob, room_id

    try:
        client.close()
    except Exception:
        pass
    try:
        registry.close()
    except Exception:
        pass
    for name in created_dbs:
        _drop_db(TEST_DSN, name)


# ── owner-only share-link CRUD ──────────────────────────────────────


def test_get_returns_disabled_before_enable(env):
    client, alice, _bob, room_id = env
    r = client.get(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(alice["api_key"]),
    )
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "room_id": room_id}


def test_enable_returns_link_and_is_idempotent(env):
    client, alice, _bob, room_id = env
    a = client.post(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(alice["api_key"]),
    )
    assert a.status_code == 200
    body = a.json()
    assert body["enabled"] is True
    assert body["room_id"] == room_id
    assert body["share_token"].startswith("hms_")
    assert body["link"].startswith("hmroom://")
    assert "share=" in body["link"]
    assert "owner_pubkey=" in body["link"]
    # idempotent — re-POST returns the same plaintext
    b = client.post(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(alice["api_key"]),
    )
    assert b.status_code == 200
    assert b.json()["share_token"] == body["share_token"]
    # GET re-fetches the same plaintext (Google-Docs URL UX)
    g = client.get(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(alice["api_key"]),
    )
    assert g.status_code == 200
    assert g.json()["share_token"] == body["share_token"]


def test_non_owner_cannot_read_or_mint(env):
    client, _alice, bob, room_id = env
    # bob is a different tenant — his hmk_ resolves to his own tenant DB
    # which doesn't have this room. Expect 404 (room not found in bob's
    # tenant). The endpoint deliberately doesn't leak whether the room
    # exists somewhere else.
    r = client.get(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(bob["api_key"]),
    )
    assert r.status_code in (403, 404)
    p = client.post(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(bob["api_key"]),
    )
    assert p.status_code in (403, 404)


def test_rotate_replaces_link_and_invalidates_old(env):
    client, alice, _bob, room_id = env
    a = client.post(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(alice["api_key"]),
    )
    old = a.json()["share_token"]
    b = client.post(
        f"/v1/rooms/{room_id}/share-link/rotate",
        headers=_headers(alice["api_key"]),
    )
    assert b.status_code == 200
    new = b.json()["share_token"]
    assert new != old
    assert b.json()["rotated_at"] is not None


def test_rotate_without_enable_404s(env):
    client, alice, _bob, room_id = env
    r = client.post(
        f"/v1/rooms/{room_id}/share-link/rotate",
        headers=_headers(alice["api_key"]),
    )
    assert r.status_code == 404


def test_disable_then_enable_yields_new_token(env):
    client, alice, _bob, room_id = env
    a = client.post(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(alice["api_key"]),
    )
    old = a.json()["share_token"]
    d = client.delete(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(alice["api_key"]),
    )
    assert d.status_code == 200
    assert d.json() == {"enabled": False, "room_id": room_id}
    g = client.get(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(alice["api_key"]),
    )
    assert g.json() == {"enabled": False, "room_id": room_id}
    # re-enable yields a brand-new plaintext
    b = client.post(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(alice["api_key"]),
    )
    assert b.json()["share_token"] != old


# ── share-role auth: outsider asks, owner pays vs. asker pays ────────


def test_share_token_grants_room_read_to_outsider(env):
    client, alice, bob, room_id = env
    enabled = client.post(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(alice["api_key"]),
    ).json()
    share_token = enabled["share_token"]

    # bob can fetch the room manifest by presenting the share token,
    # even though the room is in alice's tenant DB
    r = client.get(
        f"/v1/rooms/{room_id}",
        headers=_headers(share_token),
    )
    assert r.status_code == 200
    assert r.json()["room_id"] == room_id
    assert r.json()["manifest"]["scope"]["agent_id"] == "scope-a"


def test_share_role_run_requires_x_hivemind_api_key(env):
    client, alice, _bob, room_id = env
    enabled = client.post(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(alice["api_key"]),
    ).json()
    share_token = enabled["share_token"]

    # missing X-Hivemind-Api-Key → 402 with a clear hint
    r = client.post(
        f"/v1/rooms/{room_id}/runs",
        json={"query": "what is 2+2?"},
        headers=_headers(share_token),
    )
    assert r.status_code == 402
    assert "X-Hivemind-Api-Key" in r.text


def test_share_role_run_charges_asker_tenant(env):
    client, alice, bob, room_id = env
    enabled = client.post(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(alice["api_key"]),
    ).json()
    share_token = enabled["share_token"]

    # bob asks via the share link, paying with his own hmk_
    r = client.post(
        f"/v1/rooms/{room_id}/runs",
        json={"query": "anything"},
        headers=_share_headers(share_token, bob["api_key"]),
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    # alice (owner) sees the run row with bob's tenant_id as payer
    detail = client.get(
        f"/v1/runs/{run_id}",
        headers=_headers(alice["api_key"]),
    )
    assert detail.status_code == 200
    assert detail.json()["payer_tenant_id"] == bob["tenant_id"]
    # issuer_token_id non-null → owner UI knows to render an asker pill
    assert detail.json()["issuer_token_id"]


def test_rotated_share_token_no_longer_resolves(env):
    client, alice, bob, room_id = env
    enabled = client.post(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(alice["api_key"]),
    ).json()
    old = enabled["share_token"]
    client.post(
        f"/v1/rooms/{room_id}/share-link/rotate",
        headers=_headers(alice["api_key"]),
    )
    # presenting the OLD token now → 401 (resolve_any returns None)
    r = client.post(
        f"/v1/rooms/{room_id}/runs",
        json={"query": "anything"},
        headers=_share_headers(old, bob["api_key"]),
    )
    assert r.status_code == 401


def test_disabled_share_token_no_longer_resolves(env):
    client, alice, bob, room_id = env
    enabled = client.post(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(alice["api_key"]),
    ).json()
    share_token = enabled["share_token"]
    client.delete(
        f"/v1/rooms/{room_id}/share-link",
        headers=_headers(alice["api_key"]),
    )
    r = client.post(
        f"/v1/rooms/{room_id}/runs",
        json={"query": "anything"},
        headers=_share_headers(share_token, bob["api_key"]),
    )
    assert r.status_code == 401

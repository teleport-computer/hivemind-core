"""Tests for two related changes:

  1. ``inspection_mode`` is per-scope-agent and inherited by query
     agents uploaded via /v1/query-agents/submit. B does not pick.
  2. Each run carries an ``issuer_token_id`` linking it back to the
     capability token (``hmq_``) that initiated it; ``GET
     /v1/agent-runs?token_id=…`` surfaces it for owner audit.

Postgres-backed; skips when ``HIVEMIND_TEST_DATABASE_URL`` is
unreachable. Modeled after tests/test_query_agent_upload_gate.py.
"""

from __future__ import annotations

import io
import os
import secrets
import tarfile

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from hivemind import agent_seal
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


def _tiny_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in (
            ("Dockerfile", "FROM python:3.12-slim\n"),
            ("agent.py", "print('audit')\n"),
        ):
            data = body.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _stub_agent_seal_key():
    """Sealed inheritance writes ciphertext via agent_seal — bypass KMS."""
    agent_seal.reset_for_tests()
    with agent_seal._state["lock"]:
        agent_seal._state["key"] = b"\x42" * 32
        agent_seal._state["key_path"] = "test-key"
    yield
    agent_seal.reset_for_tests()


@pytest.fixture
def env_factory():
    """Builds an app+tenant. Returns a callable so individual tests can
    seed scope agents in either ``full`` or ``sealed`` mode."""
    control_db = _unique("hm_inspect_audit")
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{control_db}"')

    settings = Settings(
        database_url=TEST_DSN,
        control_database=control_db,
        admin_key="admin-test-key",
        sql_proxy_admin_key="",
        autoload_default_agents=False,
        artifact_sweep_interval_seconds=9999,
    )
    registry = TenantRegistry(settings)
    from hivemind.admin_proxy import LocalPgAdmin
    registry._pg_admin = LocalPgAdmin(TEST_DSN)
    created_dbs: list[str] = [control_db]

    app = create_app(settings)
    app.state.registry = registry

    t = registry.provision("inspect-audit")
    created_dbs.append(t["db_name"])

    def _make(scope_mode: str, *, can_upload: bool = True) -> dict:
        scope_id = f"scope_{scope_mode}_{secrets.token_hex(2)}"
        hive = registry.for_tenant(t["tenant_id"])
        assert hive is not None
        hive.agent_store.create(
            AgentConfig(
                agent_id=scope_id,
                name=f"scope-{scope_mode}",
                description="fixture",
                agent_type="scope",
                image="hivemind-test:latest",
                entrypoint=None,
                memory_mb=256,
                max_llm_calls=10,
                max_tokens=10_000,
                timeout_seconds=60,
                inspection_mode=scope_mode,
            )
        )
        hive.agent_store.save_files(
            scope_id,
            {"Dockerfile": "FROM python:3.12-slim\n", "agent.py": "x\n"},
            inspection_mode=scope_mode,
        )
        cap = registry.mint_capability(
            t["tenant_id"], "query", f"label-{scope_mode}",
            {
                "scope_agent_id": scope_id,
                "can_upload_query_agent": can_upload,
            },
        )
        return {
            "scope_id": scope_id,
            "token": cap["token"],
            "token_id": cap["token_id"],
            "hive": hive,
        }

    client = TestClient(app, base_url="http://inspect-audit")
    yield client, t["api_key"], _make

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


def _submit(client: TestClient, token: str) -> "httpx.Response":
    return client.post(
        "/v1/query-agents/submit",
        headers={"Authorization": f"Bearer {token}"},
        files={"archive": ("agent.tar.gz", _tiny_tar(), "application/gzip")},
        data={"name": "qa-test", "prompt": "hi"},
    )


# ── inheritance ──


def test_query_agent_inherits_full_when_scope_is_full(env_factory):
    """Default-mode scope → response echoes inspection_mode=full.

    The agent row + files are written by the background build task, so
    we can't synchronously assert on agent_store here. The contract
    check is that the synchronous response correctly reflects the
    inherited mode (which is what B's CLI relies on)."""
    client, _owner, make_room = env_factory
    room = make_room("full")
    r = _submit(client, room["token"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inspection_mode"] == "full"


def test_query_agent_inherits_sealed_when_scope_is_sealed(env_factory):
    """Sealed scope → response echoes inspection_mode=sealed.

    B does not pass an inspection_mode form field; the server reads
    it off the bound scope agent. This test would catch a regression
    where /v1/query-agents/submit silently defaults to 'full' when
    scope is 'sealed'."""
    client, _owner, make_room = env_factory
    room = make_room("sealed")
    r = _submit(client, room["token"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inspection_mode"] == "sealed"


# ── audit: issuer_token_id linkage + filter ──


def test_run_record_carries_issuer_token_id(env_factory):
    client, _owner, make_room = env_factory
    room = make_room("full")
    r = _submit(client, room["token"])
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    rows = room["hive"].db.execute(
        "SELECT issuer_token_id FROM _hivemind_query_runs "
        "WHERE run_id = %s",
        [run_id],
    )
    assert rows
    assert rows[0]["issuer_token_id"] == room["token_id"]


def test_owner_can_filter_runs_by_token_id(env_factory):
    """Owner audit: GET /v1/agent-runs?token_id=… returns only runs
    initiated by that token. Two tokens → two non-overlapping result
    sets, plus the unfiltered list contains both."""
    client, owner_key, make_room = env_factory
    room_a = make_room("full")
    room_b = make_room("full")

    ra = _submit(client, room_a["token"])
    rb = _submit(client, room_b["token"])
    assert ra.status_code == 200 and rb.status_code == 200
    rid_a = ra.json()["run_id"]
    rid_b = rb.json()["run_id"]

    headers_owner = {"Authorization": f"Bearer {owner_key}"}

    # Filter to A's token.
    resp = client.get(
        f"/v1/agent-runs?token_id={room_a['token_id']}",
        headers=headers_owner,
    )
    assert resp.status_code == 200
    rows_a = resp.json()
    rids_a = {r["run_id"] for r in rows_a}
    assert rid_a in rids_a
    assert rid_b not in rids_a
    for row in rows_a:
        assert row["issuer_token_id"] == room_a["token_id"]

    # Unfiltered listing has both.
    resp_all = client.get("/v1/agent-runs", headers=headers_owner)
    assert resp_all.status_code == 200
    rids_all = {r["run_id"] for r in resp_all.json()}
    assert {rid_a, rid_b} <= rids_all


def test_query_token_lists_only_its_own_runs(env_factory):
    """A query token must not see other hmq_ tokens' run history."""
    client, _owner, make_room = env_factory
    room_a = make_room("full")
    room_b = make_room("full")

    ra = _submit(client, room_a["token"])
    rb = _submit(client, room_b["token"])
    assert ra.status_code == 200 and rb.status_code == 200
    rid_a = ra.json()["run_id"]
    rid_b = rb.json()["run_id"]

    resp = client.get(
        "/v1/agent-runs",
        headers={"Authorization": f"Bearer {room_a['token']}"},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    run_ids = {r["run_id"] for r in rows}
    assert rid_a in run_ids
    assert rid_b not in run_ids
    assert {r["issuer_token_id"] for r in rows} == {room_a["token_id"]}


def test_query_token_cannot_fetch_other_token_run(env_factory):
    client, _owner, make_room = env_factory
    room_a = make_room("full")
    room_b = make_room("full")

    ra = _submit(client, room_a["token"])
    rb = _submit(client, room_b["token"])
    assert ra.status_code == 200 and rb.status_code == 200
    rid_a = ra.json()["run_id"]
    rid_b = rb.json()["run_id"]

    headers_a = {"Authorization": f"Bearer {room_a['token']}"}
    own = client.get(f"/v1/agent-runs/{rid_a}", headers=headers_a)
    assert own.status_code == 200, own.text

    other = client.get(f"/v1/agent-runs/{rid_b}", headers=headers_a)
    assert other.status_code == 404, other.text


def test_query_token_cannot_fetch_other_token_artifact(env_factory):
    client, owner_key, make_room = env_factory
    room_a = make_room("full")
    room_b = make_room("full")

    rb = _submit(client, room_b["token"])
    assert rb.status_code == 200, rb.text
    rid_b = rb.json()["run_id"]
    room_b["hive"].artifact_store.put(
        rid_b,
        "report.txt",
        b"token-b secret artifact",
        "text/plain",
    )

    other = client.get(
        f"/v1/query/runs/{rid_b}/artifacts/report.txt",
        headers={"Authorization": f"Bearer {room_a['token']}"},
    )
    assert other.status_code == 404, other.text

    owner = client.get(
        f"/v1/query/runs/{rid_b}/artifacts/report.txt",
        headers={"Authorization": f"Bearer {owner_key}"},
    )
    assert owner.status_code == 200, owner.text
    assert owner.content == b"token-b secret artifact"


def test_query_token_cannot_filter_by_token_id(env_factory):
    """Audit filter is owner-only. A query token holder asking for
    `?token_id=` is 403'd."""
    client, _owner, make_room = env_factory
    room = make_room("full")
    headers = {"Authorization": f"Bearer {room['token']}"}
    resp = client.get(
        f"/v1/agent-runs?token_id={room['token_id']}",
        headers=headers,
    )
    assert resp.status_code == 403, resp.text


def test_owner_filter_rejects_short_token_id(env_factory):
    client, owner_key, _ = env_factory
    headers = {"Authorization": f"Bearer {owner_key}"}
    resp = client.get(
        "/v1/agent-runs?token_id=ab",
        headers=headers,
    )
    assert resp.status_code == 400, resp.text

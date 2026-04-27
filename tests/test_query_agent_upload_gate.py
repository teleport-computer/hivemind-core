"""Phase 4: server-side gate on /v1/query-agents/submit for hmq_ tokens.

Owner tokens (hmk_) are unaffected; query tokens must opt-in via
``constraints.can_upload_query_agent=true`` to upload their own code.
The default (false) keeps existing recipients prompt-only.
"""

from __future__ import annotations

import io
import os
import secrets
import tarfile

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


def _tiny_tar() -> bytes:
    """Smallest valid agent tarball — Dockerfile + agent.py."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in (
            ("Dockerfile", "FROM python:3.12-slim\n"),
            ("agent.py", "print('phase4')\n"),
        ):
            data = body.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def gated_env():
    """Spin up app + tenant + scope agent + two hmq_ tokens."""
    control_db = _unique("hm_p4_gate")
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

    t = registry.provision("p4-gate")
    created_dbs.append(t["db_name"])

    scope_id = "scope_p4_gate"
    hive = registry.for_tenant(t["tenant_id"])
    assert hive is not None
    hive.agent_store.create(
        AgentConfig(
            agent_id=scope_id,
            name="scope-p4-gate",
            description="fixture",
            agent_type="scope",
            image="hivemind-p4-gate:latest",
            entrypoint=None,
            memory_mb=256,
            max_llm_calls=10,
            max_tokens=10_000,
            timeout_seconds=60,
        )
    )
    hive.agent_store.save_files(
        scope_id, {"Dockerfile": "FROM python:3.12-slim\n", "agent.py": "x\n"},
    )

    locked = registry.mint_capability(
        t["tenant_id"], "query", "no-upload",
        {"scope_agent_id": scope_id},
    )
    open_ = registry.mint_capability(
        t["tenant_id"], "query", "uploader",
        {"scope_agent_id": scope_id, "can_upload_query_agent": True},
    )

    client = TestClient(app, base_url="http://p4-gate")
    yield client, t["api_key"], locked["token"], open_["token"]

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


def _post_upload(client: TestClient, token: str) -> "httpx.Response":
    return client.post(
        "/v1/query-agents/submit",
        headers={"Authorization": f"Bearer {token}"},
        files={"archive": ("agent.tar.gz", _tiny_tar(), "application/gzip")},
        data={"name": "phase4-test", "prompt": "hi"},
    )


def test_query_token_without_flag_is_rejected(gated_env):
    client, _owner, locked_tok, _open_tok = gated_env
    r = _post_upload(client, locked_tok)
    assert r.status_code == 403, r.text
    body = r.json()
    detail = body.get("detail", "")
    assert "can_upload_query_agent" in detail


def test_query_token_with_flag_is_accepted(gated_env):
    """Token with the flag may upload. Run record is created
    immediately; we don't wait for the actual build."""
    client, _owner, _locked_tok, open_tok = gated_env
    r = _post_upload(client, open_tok)
    # 200 = accepted, run_id assigned. The build runs in a
    # background task we don't await.
    assert r.status_code == 200, r.text
    body = r.json()
    assert "run_id" in body
    assert "agent_id" in body


def test_owner_token_can_always_upload(gated_env):
    """hmk_ owners are not gated by the constraint flag."""
    client, owner_key, _locked, _open = gated_env
    r = _post_upload(client, owner_key)
    assert r.status_code == 200, r.text

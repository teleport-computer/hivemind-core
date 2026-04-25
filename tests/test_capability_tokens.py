"""Capability-token tests — query (hmq_) and write (hmw_) delegations.

Verifies registry storage, the ``resolve_any`` dispatcher, the INSERT-only
SQL gate used by /v1/store, and the constraint validation rules around
both kinds. Postgres-backed (re-uses the live DB the rest of the
tenants suite needs); skips when ``HIVEMIND_TEST_DATABASE_URL`` is
unreachable.
"""

from __future__ import annotations

import os
import secrets

import psycopg
import pytest

from hivemind.config import Settings
from hivemind.tenants import (
    Caller,
    TenantRegistry,
    _hash_api_key,
    _QUERY_TOKEN_PREFIX,
    _WRITE_TOKEN_PREFIX,
)
from hivemind.tools import is_insert_only_into


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


def _make_settings(control_db: str) -> Settings:
    return Settings(
        database_url=TEST_DSN,
        control_database=control_db,
        admin_key="admin-test-key",
        sql_proxy_admin_key="",
        autoload_default_agents=False,
        artifact_sweep_interval_seconds=9999,
    )


@pytest.fixture
def registry():
    control_db = _unique("hm_caps")
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{control_db}"')

    settings = _make_settings(control_db)
    reg = TenantRegistry(settings)
    from hivemind.admin_proxy import LocalPgAdmin
    reg._pg_admin = LocalPgAdmin(TEST_DSN)
    created_dbs: list[str] = [control_db]
    reg._test_created_dbs = created_dbs  # type: ignore[attr-defined]

    yield reg

    try:
        reg.close()
    except Exception:
        pass
    for name in created_dbs:
        _drop_db(TEST_DSN, name)


# ── mint / list / revoke ──────────────────────────────────────────────


def test_mint_query_token_requires_scope_agent_id(registry):
    t = registry.provision("alpha")
    registry._test_created_dbs.append(t["db_name"])

    with pytest.raises(ValueError):
        registry.mint_capability(t["tenant_id"], "query", "no-scope", {})

    out = registry.mint_capability(
        t["tenant_id"], "query", "scoped", {"scope_agent_id": "abc123"},
    )
    assert out["token"].startswith(_QUERY_TOKEN_PREFIX)
    assert out["kind"] == "query"
    assert out["constraints"] == {"scope_agent_id": "abc123"}
    assert len(out["token_id"]) == 12


def test_mint_write_token_requires_allowed_tables(registry):
    t = registry.provision("beta")
    registry._test_created_dbs.append(t["db_name"])

    with pytest.raises(ValueError):
        registry.mint_capability(t["tenant_id"], "write", "no-tables", {})
    with pytest.raises(ValueError):
        registry.mint_capability(
            t["tenant_id"], "write", "empty",
            {"allowed_tables": []},
        )
    with pytest.raises(ValueError):
        registry.mint_capability(
            t["tenant_id"], "write", "internal",
            {"allowed_tables": ["_hivemind_query_runs"]},
        )

    out = registry.mint_capability(
        t["tenant_id"], "write", "ok",
        {"allowed_tables": ["watch_history", "events"]},
    )
    assert out["token"].startswith(_WRITE_TOKEN_PREFIX)
    assert out["constraints"]["allowed_tables"] == ["watch_history", "events"]


def test_mint_invalid_kind(registry):
    t = registry.provision("gamma")
    registry._test_created_dbs.append(t["db_name"])
    with pytest.raises(ValueError):
        registry.mint_capability(t["tenant_id"], "admin", "x", {})


def test_mint_unknown_tenant_raises(registry):
    with pytest.raises(KeyError):
        registry.mint_capability(
            "t_doesnotexist", "query", "x", {"scope_agent_id": "y"},
        )


def test_token_plaintext_not_persisted(registry):
    t = registry.provision("delta")
    registry._test_created_dbs.append(t["db_name"])
    out = registry.mint_capability(
        t["tenant_id"], "query", "", {"scope_agent_id": "s"},
    )
    rows = registry._control_db.execute(
        "SELECT token_hash FROM _capability_tokens WHERE tenant_id = %s",
        [t["tenant_id"]],
    )
    assert rows[0]["token_hash"] == _hash_api_key(out["token"])
    # The plaintext never appears in the row.
    assert out["token"] not in rows[0]["token_hash"]


def test_list_capabilities_returns_metadata_only(registry):
    t = registry.provision("epsilon")
    registry._test_created_dbs.append(t["db_name"])
    a = registry.mint_capability(
        t["tenant_id"], "query", "viewer", {"scope_agent_id": "s1"},
    )
    b = registry.mint_capability(
        t["tenant_id"], "write", "ingest",
        {"allowed_tables": ["watch_history"]},
    )
    rows = registry.list_capabilities(t["tenant_id"])
    ids = {r["token_id"] for r in rows}
    assert {a["token_id"], b["token_id"]} <= ids
    for r in rows:
        # Hash + plaintext must not leak into the listing.
        assert "token" not in r
        assert "token_hash" not in r


def test_revoke_capability_blocks_resolution(registry):
    t = registry.provision("zeta")
    registry._test_created_dbs.append(t["db_name"])
    out = registry.mint_capability(
        t["tenant_id"], "query", "x", {"scope_agent_id": "s"},
    )
    caller = registry.resolve_any(out["token"])
    assert caller is not None and caller.role == "query"

    assert registry.revoke_capability(t["tenant_id"], out["token_id"]) is True
    assert registry.resolve_any(out["token"]) is None


def test_revoke_capability_requires_min_prefix_length(registry):
    t = registry.provision("eta")
    registry._test_created_dbs.append(t["db_name"])
    with pytest.raises(ValueError):
        registry.revoke_capability(t["tenant_id"], "abc")


def test_revoke_capability_idempotent_on_unknown(registry):
    t = registry.provision("theta")
    registry._test_created_dbs.append(t["db_name"])
    assert (
        registry.revoke_capability(t["tenant_id"], "deadbeefcafe") is False
    )


# ── resolve_any ───────────────────────────────────────────────────────


def test_resolve_any_owner_token(registry):
    t = registry.provision("iota")
    registry._test_created_dbs.append(t["db_name"])
    caller = registry.resolve_any(t["api_key"])
    assert isinstance(caller, Caller)
    assert caller.role == "owner"
    assert caller.tenant_id == t["tenant_id"]
    assert caller.constraints == {}


def test_resolve_any_query_token(registry):
    t = registry.provision("kappa")
    registry._test_created_dbs.append(t["db_name"])
    out = registry.mint_capability(
        t["tenant_id"], "query", "shared",
        {"scope_agent_id": "abc123"},
    )
    caller = registry.resolve_any(out["token"])
    assert caller is not None
    assert caller.role == "query"
    assert caller.tenant_id == t["tenant_id"]
    assert caller.constraints == {"scope_agent_id": "abc123"}
    assert caller.token_id == out["token_id"]


def test_resolve_any_write_token(registry):
    t = registry.provision("lambda")
    registry._test_created_dbs.append(t["db_name"])
    out = registry.mint_capability(
        t["tenant_id"], "write", "stream",
        {"allowed_tables": ["watch_history"]},
    )
    caller = registry.resolve_any(out["token"])
    assert caller is not None
    assert caller.role == "write"
    assert caller.constraints == {"allowed_tables": ["watch_history"]}


def test_resolve_any_rejects_unknown_prefix(registry):
    assert registry.resolve_any("hmx_garbage") is None
    assert registry.resolve_any("") is None
    assert registry.resolve_any("nope") is None


def test_resolve_any_rejects_revoked(registry):
    t = registry.provision("mu")
    registry._test_created_dbs.append(t["db_name"])
    out = registry.mint_capability(
        t["tenant_id"], "query", "", {"scope_agent_id": "s"},
    )
    registry.revoke_capability(t["tenant_id"], out["token_id"])
    assert registry.resolve_any(out["token"]) is None


def test_resolve_any_rejects_suspended_tenant(registry):
    t = registry.provision("nu")
    registry._test_created_dbs.append(t["db_name"])
    out = registry.mint_capability(
        t["tenant_id"], "query", "", {"scope_agent_id": "s"},
    )
    registry._control_db.execute_commit(
        "UPDATE _tenants SET suspended = TRUE WHERE id = %s",
        [t["tenant_id"]],
    )
    assert registry.resolve_any(out["token"]) is None


def test_capability_tokens_cascade_on_tenant_delete(registry):
    t = registry.provision("xi")
    registry._test_created_dbs.append(t["db_name"])
    out = registry.mint_capability(
        t["tenant_id"], "write", "",
        {"allowed_tables": ["watch_history"]},
    )
    registry.delete(t["tenant_id"])
    rows = registry._control_db.execute(
        "SELECT token_hash FROM _capability_tokens WHERE tenant_id = %s",
        [t["tenant_id"]],
    )
    assert rows == []
    assert registry.resolve_any(out["token"]) is None


# ── INSERT-only gate (used by /v1/store for write tokens) ─────────────


@pytest.mark.parametrize("sql", [
    "INSERT INTO watch_history (id, body) VALUES (1, 'a')",
    "insert into Watch_History(id) values (1)",
    """
    INSERT INTO watch_history (id, body)
    SELECT 1, 'x'
    """,
])
def test_is_insert_only_into_accepts_valid(sql):
    is_insert_only_into(sql, ["watch_history"])


@pytest.mark.parametrize("sql,reason", [
    ("SELECT * FROM watch_history", "select"),
    ("UPDATE watch_history SET body = 'x'", "update"),
    ("DELETE FROM watch_history", "delete"),
    ("INSERT INTO other (id) VALUES (1)", "wrong-table"),
    ("INSERT INTO _hivemind_query_runs (id) VALUES (1)", "internal-table"),
    ("INSERT INTO watch_history VALUES(1); INSERT INTO watch_history VALUES(2)", "compound"),
    ("DROP TABLE watch_history", "drop"),
])
def test_is_insert_only_into_rejects(sql, reason):
    with pytest.raises(ValueError):
        is_insert_only_into(sql, ["watch_history"])


def test_is_insert_only_into_rejects_empty_allowlist():
    with pytest.raises(ValueError):
        is_insert_only_into(
            "INSERT INTO watch_history (id) VALUES (1)", [],
        )

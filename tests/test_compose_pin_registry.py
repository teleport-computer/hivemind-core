"""Postgres-backed tests for ``TenantRegistry`` compose-pin storage.

Covers the DB-shaped half of Phase 2: store / list / get / latest /
revoke. The signature math itself is tested in
``test_compose_pin.py`` (no DB).

Skips when ``HIVEMIND_TEST_DATABASE_URL`` is unreachable (same pattern
as ``test_capability_tokens.py``).
"""

from __future__ import annotations

import os
import secrets

import psycopg
import pytest

from hivemind.compose_pin import make_unsigned_pin
from hivemind.config import Settings
from hivemind.tenant_signing import derive_signing_keypair
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


@pytest.fixture
def registry():
    control_db = _unique("hm_pins")
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


def _signed_envelope(token: str, tenant_id: str, **overrides) -> tuple[str, str]:
    """Sign a fresh pin and return ``(envelope_json, signer_pubkey_b64)``."""
    priv, _pub = derive_signing_keypair(token, tenant_id)
    base = dict(
        tenant_id=tenant_id,
        allowed_composes=["a" * 64],
        scope_agent_id="agent_scope",
        attested_files_digest="b" * 64,
        ttl_seconds=0,
    )
    base.update(overrides)
    pin = make_unsigned_pin(**base).sign(priv)
    return pin.to_json(), pin.signer_pubkey


def test_store_and_get_pin(registry):
    t = registry.provision("alpha")
    registry._test_created_dbs.append(t["db_name"])
    env, pub = _signed_envelope(t["api_key"], t["tenant_id"])

    res = registry.store_compose_pin(t["tenant_id"], env, pub)
    assert res["tenant_id"] == t["tenant_id"]
    assert len(res["pin_id"]) == 12

    row = registry.get_compose_pin(t["tenant_id"], res["pin_id"])
    assert row is not None
    assert row["envelope"] == env
    assert row["pubkey_b64"] == pub
    assert row["revoked_at"] is None


def test_store_is_idempotent_on_same_envelope(registry):
    """Same envelope JSON → same pin_id → ON CONFLICT no-op."""
    t = registry.provision("alpha")
    registry._test_created_dbs.append(t["db_name"])
    env, pub = _signed_envelope(t["api_key"], t["tenant_id"])

    r1 = registry.store_compose_pin(t["tenant_id"], env, pub)
    r2 = registry.store_compose_pin(t["tenant_id"], env, pub)
    assert r1["pin_id"] == r2["pin_id"]
    assert len(registry.list_compose_pins(t["tenant_id"])) == 1


def test_latest_returns_most_recent_unrevoked(registry):
    import time as _t

    t = registry.provision("alpha")
    registry._test_created_dbs.append(t["db_name"])
    env1, pub = _signed_envelope(
        t["api_key"], t["tenant_id"], allowed_composes=["1" * 64],
    )
    r1 = registry.store_compose_pin(t["tenant_id"], env1, pub)
    _t.sleep(0.01)
    env2, _ = _signed_envelope(
        t["api_key"], t["tenant_id"], allowed_composes=["2" * 64],
    )
    r2 = registry.store_compose_pin(t["tenant_id"], env2, pub)

    latest = registry.latest_compose_pin(t["tenant_id"])
    assert latest is not None
    assert latest["pin_id"] == r2["pin_id"]

    # Revoking the latest should fall back to the older one.
    assert registry.revoke_compose_pin(t["tenant_id"], r2["pin_id"]) is True
    latest2 = registry.latest_compose_pin(t["tenant_id"])
    assert latest2 is not None
    assert latest2["pin_id"] == r1["pin_id"]


def test_latest_returns_none_for_fresh_tenant(registry):
    t = registry.provision("empty")
    registry._test_created_dbs.append(t["db_name"])
    assert registry.latest_compose_pin(t["tenant_id"]) is None


def test_revoke_unknown_pin_returns_false(registry):
    t = registry.provision("alpha")
    registry._test_created_dbs.append(t["db_name"])
    assert registry.revoke_compose_pin(t["tenant_id"], "nopepin") is False


def test_revoke_twice_idempotent_false_second_time(registry):
    t = registry.provision("alpha")
    registry._test_created_dbs.append(t["db_name"])
    env, pub = _signed_envelope(t["api_key"], t["tenant_id"])
    r = registry.store_compose_pin(t["tenant_id"], env, pub)

    assert registry.revoke_compose_pin(t["tenant_id"], r["pin_id"]) is True
    # Second revoke is a no-op (revoked_at is already set).
    assert registry.revoke_compose_pin(t["tenant_id"], r["pin_id"]) is False


def test_pins_isolated_per_tenant(registry):
    t1 = registry.provision("alpha")
    t2 = registry.provision("beta")
    registry._test_created_dbs.extend([t1["db_name"], t2["db_name"]])

    env, pub = _signed_envelope(t1["api_key"], t1["tenant_id"])
    registry.store_compose_pin(t1["tenant_id"], env, pub)

    assert registry.list_compose_pins(t2["tenant_id"]) == []
    assert registry.latest_compose_pin(t2["tenant_id"]) is None


def test_store_unknown_tenant_raises(registry):
    env, pub = _signed_envelope("hmk_demo", "t_nope")
    with pytest.raises(KeyError):
        registry.store_compose_pin("t_nope", env, pub)


def test_pins_cascade_on_tenant_delete(registry):
    """Dropping a tenant should sweep its pins via FK CASCADE."""
    t = registry.provision("alpha")
    db_name = t["db_name"]
    registry._test_created_dbs.append(db_name)
    env, pub = _signed_envelope(t["api_key"], t["tenant_id"])
    registry.store_compose_pin(t["tenant_id"], env, pub)
    assert len(registry.list_compose_pins(t["tenant_id"])) == 1

    registry.delete(t["tenant_id"])
    # tenant_id no longer exists; list should be empty for that id.
    assert registry.list_compose_pins(t["tenant_id"]) == []

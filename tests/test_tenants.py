"""Multi-tenant isolation tests.

Requires a live Postgres reachable at HIVEMIND_TEST_DATABASE_URL (the
default: postgresql://hivemind:dev@localhost:5432/postgres — the `postgres`
maintenance DB so we can CREATE DATABASE). Each test provisions tenants
into ephemeral databases and drops them afterward.

We exercise the TenantRegistry directly (no HTTP) because the server
depends on Docker for pipeline bootstrap; the control plane logic lives
in tenants.py and we want to verify isolation without spinning up
sandbox infrastructure.
"""

from __future__ import annotations

import os
import secrets

import psycopg
import pytest

from hivemind.config import Settings
from hivemind.tenants import TenantRegistry, _hash_api_key


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
    """Control DB is ephemeral — autoload default agents disabled so no Docker."""
    return Settings(
        database_url=TEST_DSN,
        control_database=control_db,
        admin_key="admin-test-key",
        sql_proxy_admin_key="",  # direct psycopg admin, not HTTP
        autoload_default_agents=False,
        artifact_sweep_interval_seconds=9999,
    )


@pytest.fixture
def registry():
    """A TenantRegistry with an ephemeral control DB.

    Uses LocalPgAdmin (direct psycopg) because TEST_DSN is not HTTP.
    """
    control_db = _unique("hm_ctrl")
    # Create the control DB from the maintenance DB.
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{control_db}"')

    settings = _make_settings(control_db)
    reg = TenantRegistry(settings)
    # `provision` path uses make_admin → LocalPgAdmin for non-HTTP DSNs.
    # TenantRegistry only constructs pg_admin if sql_proxy_admin_key is set.
    # For tests with direct psycopg, force it on via the LocalPgAdmin path.
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


# ── Core control-plane tests ──


def test_provision_creates_isolated_database(registry):
    result = registry.provision("alice-corp")
    registry._test_created_dbs.append(result["db_name"])

    assert result["tenant_id"].startswith("t_")
    assert result["api_key"].startswith("hmk_")
    assert result["db_name"] == f"tenant_{result['tenant_id']}"
    assert result["name"] == "alice-corp"

    # The new DB exists and is separate.
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        dbs = [r[0] for r in conn.execute(
            "SELECT datname FROM pg_database"
        ).fetchall()]
    assert result["db_name"] in dbs


def test_api_key_stored_only_as_hash(registry):
    result = registry.provision("bob-inc")
    registry._test_created_dbs.append(result["db_name"])

    rows = registry._control_db.execute(
        "SELECT api_key_hash FROM _tenants WHERE id = %s",
        [result["tenant_id"]],
    )
    stored = bytes(rows[0]["api_key_hash"])
    # Plaintext key is never persisted.
    assert result["api_key"].encode() not in stored
    # Hash matches.
    assert stored == _hash_api_key(result["api_key"])


def test_resolve_invalid_key_returns_none(registry):
    assert registry.resolve("") is None
    assert registry.resolve("hmk_bogus_key_that_does_not_exist") is None


def test_resolve_valid_key_returns_tenant_hivemind(registry):
    t = registry.provision("carol-labs")
    registry._test_created_dbs.append(t["db_name"])

    resolved = registry.resolve(t["api_key"])
    assert resolved is not None
    tenant_id, hm = resolved
    assert tenant_id == t["tenant_id"]
    assert hm.tenant_id == t["tenant_id"]
    assert hm.tenant_db == t["db_name"]


def test_resolve_is_cached(registry):
    t = registry.provision("dan-works")
    registry._test_created_dbs.append(t["db_name"])

    _, hm1 = registry.resolve(t["api_key"])
    _, hm2 = registry.resolve(t["api_key"])
    # Same Hivemind instance returned from LRU cache.
    assert hm1 is hm2


def test_tenants_cannot_see_each_other_data(registry):
    """The real isolation proof: writes in one tenant invisible in another."""
    a = registry.provision("tenant-a")
    registry._test_created_dbs.append(a["db_name"])
    b = registry.provision("tenant-b")
    registry._test_created_dbs.append(b["db_name"])

    _, hm_a = registry.resolve(a["api_key"])
    _, hm_b = registry.resolve(b["api_key"])

    # Create an identically-named table in each tenant DB with different data.
    hm_a.db.execute_commit(
        "CREATE TABLE notes (id SERIAL PRIMARY KEY, body TEXT)"
    )
    hm_a.db.execute_commit(
        "INSERT INTO notes (body) VALUES (%s)", ["alice secret"]
    )

    hm_b.db.execute_commit(
        "CREATE TABLE notes (id SERIAL PRIMARY KEY, body TEXT)"
    )
    hm_b.db.execute_commit(
        "INSERT INTO notes (body) VALUES (%s)", ["bob secret"]
    )

    a_rows = hm_a.db.execute("SELECT body FROM notes")
    b_rows = hm_b.db.execute("SELECT body FROM notes")

    assert [r["body"] for r in a_rows] == ["alice secret"]
    assert [r["body"] for r in b_rows] == ["bob secret"]
    # Not possible for either side to see the other's row, regardless of SQL.


def test_delete_drops_tenant_database(registry):
    t = registry.provision("deletable")
    registry._test_created_dbs.append(t["db_name"])

    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        dbs_before = [r[0] for r in conn.execute(
            "SELECT datname FROM pg_database"
        ).fetchall()]
    assert t["db_name"] in dbs_before

    registry.delete(t["tenant_id"])

    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        dbs_after = [r[0] for r in conn.execute(
            "SELECT datname FROM pg_database"
        ).fetchall()]
    assert t["db_name"] not in dbs_after

    # Key stops resolving.
    assert registry.resolve(t["api_key"]) is None


def test_delete_nonexistent_raises_keyerror(registry):
    with pytest.raises(KeyError):
        registry.delete("t_not_real")


def test_suspended_tenant_cannot_resolve(registry):
    t = registry.provision("suspendable")
    registry._test_created_dbs.append(t["db_name"])

    registry._control_db.execute_commit(
        "UPDATE _tenants SET suspended = TRUE WHERE id = %s",
        [t["tenant_id"]],
    )
    # Evict cache since we bypassed registry.delete.
    with registry._lock:
        hm = registry._cache.pop(t["tenant_id"], None)
    if hm is not None:
        try:
            hm.db.close()
        except Exception:
            pass

    assert registry.resolve(t["api_key"]) is None


def test_list_tenants_includes_provisioned(registry):
    a = registry.provision("li-a")
    registry._test_created_dbs.append(a["db_name"])
    b = registry.provision("li-b")
    registry._test_created_dbs.append(b["db_name"])

    ids = {t["id"] for t in registry.list_tenants()}
    assert a["tenant_id"] in ids
    assert b["tenant_id"] in ids


def test_register_existing_adopts_db_without_touching_it(registry):
    """register_existing should NOT create the DB, only stamp the row."""
    adopted_db = _unique("tenant_legacy")
    # Create the DB outside of registry, pretend it's an old hivemind DB.
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{adopted_db}"')
    registry._test_created_dbs.append(adopted_db)

    result = registry.register_existing("legacy", adopted_db)
    assert result["db_name"] == adopted_db
    assert result["api_key"].startswith("hmk_")

    # Resolve and verify pointed at the adopted DB.
    _, hm = registry.resolve(result["api_key"])
    assert hm.tenant_db == adopted_db


def test_rotate_key_invalidates_old_key_and_returns_new(registry):
    t = registry.provision("rotate-me")
    registry._test_created_dbs.append(t["db_name"])
    old_key = t["api_key"]

    result = registry.rotate_key(t["tenant_id"])
    assert result["tenant_id"] == t["tenant_id"]
    new_key = result["api_key"]
    assert new_key.startswith("hmk_")
    assert new_key != old_key

    # Old key no longer resolves; new one does.
    assert registry.resolve(old_key) is None
    resolved = registry.resolve(new_key)
    assert resolved is not None
    tid, hm = resolved
    assert tid == t["tenant_id"]
    assert hm.tenant_db == t["db_name"]


def test_rotate_key_nonexistent_raises_keyerror(registry):
    with pytest.raises(KeyError):
        registry.rotate_key("t_doesnotexist")


def test_register_existing_with_pinned_tenant_id(registry):
    """Migration path: caller already renamed the DB to tenant_<id>."""
    tenant_id = "t_" + secrets.token_hex(6)
    db_name = f"tenant_{tenant_id}"
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')
    registry._test_created_dbs.append(db_name)

    result = registry.register_existing(
        "pinned", db_name, tenant_id=tenant_id,
    )
    assert result["tenant_id"] == tenant_id
    assert result["db_name"] == db_name


def test_register_existing_rejects_bad_tenant_id(registry):
    with pytest.raises(ValueError):
        registry.register_existing("x", "tenant_whatever", tenant_id="BOGUS")
    with pytest.raises(ValueError):
        registry.register_existing("x", "tenant_whatever", tenant_id="t_NOTHEX")


def test_local_pg_admin_rename_database(registry):
    """LocalPgAdmin.rename_database renames the cluster DB in place."""
    from hivemind.admin_proxy import LocalPgAdmin

    old = _unique("rename_from")
    new = _unique("rename_to")
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{old}"')
    registry._test_created_dbs.append(new)  # whatever survives, we clean up

    admin = LocalPgAdmin(TEST_DSN)
    admin.rename_database(old, new)

    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        dbs = [r[0] for r in conn.execute(
            "SELECT datname FROM pg_database"
        ).fetchall()]
    assert old not in dbs
    assert new in dbs


def test_docker_image_tag_scoping():
    """Image tags must embed tenant_id so shared daemons don't collide."""
    from hivemind.server import _tenant_image_tag

    t1 = _tenant_image_tag("t_abc123", "agent1")
    t2 = _tenant_image_tag("t_xyz789", "agent1")
    assert t1 != t2
    assert "t_abc123" in t1
    assert "t_xyz789" in t2
    # Legacy (no tenant): still works.
    assert _tenant_image_tag(None, "agent1") == "hivemind-agent-agent1:latest"

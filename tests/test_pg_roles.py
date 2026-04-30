"""Unit tests for the per-tenant role derivation (Layer 1 isolation).

These cover the hivemind/_pg_roles.py module and verify that the inlined
copy in deploy/postgres/sql_proxy.py derives identical values — that
parity is the whole point of duplicating the helpers.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import secrets as _secrets
import sys

import psycopg
import pytest

from hivemind._pg_roles import (
    derive_tenant_role_password,
    parse_tenant_id_from_db_name,
    role_name_for_tenant,
)


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


def _pg_can_create_roles(dsn: str) -> bool:
    """Return True if the DSN's user is superuser or has CREATEROLE.

    The Layer 1 admin ops need to issue CREATE ROLE / DROP ROLE, which
    requires one of those privileges. On Phala we connect as superuser;
    local dev boxes sometimes run as a non-privileged user, in which
    case we skip instead of falsely failing.
    """
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rolsuper OR rolcreaterole "
                    "FROM pg_roles WHERE rolname = current_user"
                )
                row = cur.fetchone()
                return bool(row and row[0])
    except Exception:
        return False


_PG = pytest.mark.skipif(
    not _pg_reachable(TEST_DSN),
    reason=f"postgres not reachable at {TEST_DSN}",
)

_PG_SUPERUSER = pytest.mark.skipif(
    not _pg_can_create_roles(TEST_DSN),
    reason=f"postgres user at {TEST_DSN} cannot CREATE ROLE",
)


def _load_sql_proxy_module():
    """Import deploy/postgres/sql_proxy.py without running main().

    The module reads a few env-backed globals at import time; we stub them
    so the import succeeds even with no Postgres configured.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    path = repo_root / "deploy" / "postgres" / "sql_proxy.py"
    spec = importlib.util.spec_from_file_location("sql_proxy_test_mod", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sql_proxy_test_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_role_password_is_deterministic():
    seed = b"test-seed-bytes"
    a = derive_tenant_role_password(seed, "t_abc123")
    b = derive_tenant_role_password(seed, "t_abc123")
    assert a == b


def test_role_password_varies_with_tenant():
    seed = b"test-seed-bytes"
    a = derive_tenant_role_password(seed, "t_abc123")
    b = derive_tenant_role_password(seed, "t_abc124")
    assert a != b


def test_role_password_varies_with_seed():
    a = derive_tenant_role_password(b"seed-a", "t_abc123")
    b = derive_tenant_role_password(b"seed-b", "t_abc123")
    assert a != b


def test_sql_proxy_connection_locks_use_pool_keys():
    """SQL proxy locks must track the same effective DSN keys as the pool."""
    mod = _load_sql_proxy_module()
    mod.DB_DSN = "postgresql://hivemind:dev@localhost:5432/hivemind"
    mod._db_locks.clear()

    lock = mod._lock_for_db("tenant_t_abc123")

    assert mod._lock_for_db("tenant_t_abc123") is lock
    assert list(mod._db_locks) == [mod._dsn_for_db("tenant_t_abc123")]


def test_sql_proxy_execute_enforces_result_row_cap(monkeypatch):
    mod = _load_sql_proxy_module()
    mod._MAX_RESULT_ROWS = 2
    mod._MAX_RESULT_BYTES = 1_000_000

    class DummyLock:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class DummyCursor:
        description = [("n",)]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params):
            pass

        def __iter__(self):
            return iter([{"n": 1}, {"n": 2}, {"n": 3}])

    class DummyConn:
        def cursor(self):
            return DummyCursor()

        def rollback(self):
            pass

    monkeypatch.setattr(mod, "_lock_for_db", lambda db_name: DummyLock())
    monkeypatch.setattr(mod, "_get_conn", lambda db_name: DummyConn())

    with pytest.raises(ValueError, match="row cap exceeded"):
        mod.db_execute("SELECT n FROM big_table", None, None)


def test_role_password_is_urlsafe_base64_no_padding():
    pw = derive_tenant_role_password(b"seed", "t_abc123")
    assert "=" not in pw
    assert "+" not in pw and "/" not in pw
    assert len(pw) == 43  # 32-byte HMAC → 43 base64 chars without padding


def test_role_password_rejects_empty_inputs():
    with pytest.raises(ValueError):
        derive_tenant_role_password(b"", "t_abc123")
    with pytest.raises(ValueError):
        derive_tenant_role_password(b"seed", "")


def test_role_name_within_postgres_limit():
    # role_name = "tenant_" + tenant_id + "_role" → 12 + len(tenant_id)
    # So tenant_id may be at most 51 chars (12 + 51 = 63).
    name = role_name_for_tenant("t_" + "f" * 49)  # 51 chars total
    assert len(name) == 63
    with pytest.raises(ValueError):
        role_name_for_tenant("t_" + "f" * 60)  # well over the limit


def test_parse_tenant_id_round_trip():
    assert parse_tenant_id_from_db_name("tenant_t_abc123") == "t_abc123"
    assert parse_tenant_id_from_db_name("tenant_t_0") == "t_0"


def test_parse_tenant_id_rejects_non_tenant_dbs():
    assert parse_tenant_id_from_db_name(None) is None
    assert parse_tenant_id_from_db_name("") is None
    assert parse_tenant_id_from_db_name("hivemind_control") is None
    assert parse_tenant_id_from_db_name("postgres") is None
    assert parse_tenant_id_from_db_name("tenant_something_else") is None


def test_sql_proxy_inline_copy_matches_pg_roles():
    """sql_proxy.py inlines the derivation helpers; they MUST stay in sync.

    If you change one side, change the other. This test would catch a
    drift between the two before it hits production.
    """
    mod = _load_sql_proxy_module()
    seeds = [b"s1", b"another-seed-bytes"]
    tenants = ["t_abc123", "t_0", "t_" + "a" * 49]
    for seed in seeds:
        for tid in tenants:
            assert mod._derive_role_password(seed, tid) == (
                derive_tenant_role_password(seed, tid)
            )
            assert mod._role_name_for_tenant(tid) == role_name_for_tenant(tid)
    for name in [
        None, "", "hivemind_control", "tenant_t_abc123", "tenant_t_0",
        "tenant_something_else",
    ]:
        assert mod._parse_tenant_id_from_db_name(name) == (
            parse_tenant_id_from_db_name(name)
        )


# ── Integration tests (real Postgres) ──


@_PG_SUPERUSER
def test_create_drop_tenant_with_role_roundtrip():
    """End-to-end: create a tenant DB + role, connect with it, drop both."""
    mod = _load_sql_proxy_module()
    mod.DB_DSN = TEST_DSN
    os.environ["SQL_PROXY_KEY"] = "test-proxy-key-" + _secrets.token_hex(4)
    mod.PROXY_KEY = os.environ["SQL_PROXY_KEY"]

    tenant_id = "t_" + _secrets.token_hex(6)
    db_name = f"tenant_{tenant_id}"
    role_name = role_name_for_tenant(tenant_id)

    try:
        mod.admin_create_tenant_with_role(db_name, tenant_id)

        # Role exists?
        assert mod._role_exists(role_name)

        # Can the tenant role connect to its DB with derived password?
        tenant_dsn = mod._dsn_for_db(db_name)
        with psycopg.connect(tenant_dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user")
                assert cur.fetchone()[0] == role_name
                # public is owned by the tenant role → CREATE TABLE works
                cur.execute("CREATE TABLE t_owned (id int)")
                cur.execute(
                    "SELECT tableowner FROM pg_tables "
                    "WHERE tablename = 't_owned'"
                )
                assert cur.fetchone()[0] == role_name

        # Tenant role is NOT a superuser (the whole point of Layer 1)
        with psycopg.connect(tenant_dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rolsuper FROM pg_roles WHERE rolname = %s",
                    [role_name],
                )
                assert cur.fetchone()[0] is False
    finally:
        try:
            mod.admin_drop_tenant_with_role(db_name, tenant_id)
        except Exception:
            pass

    # Post-drop: role and DB should both be gone
    assert not mod._role_exists(role_name)
    assert not mod._db_exists(db_name)


@_PG_SUPERUSER
def test_migrate_tenant_to_role_is_idempotent():
    """Retrofit a role onto an existing tenant DB without a role. Idempotent."""
    mod = _load_sql_proxy_module()
    mod.DB_DSN = TEST_DSN
    os.environ["SQL_PROXY_KEY"] = "test-proxy-key-" + _secrets.token_hex(4)
    mod.PROXY_KEY = os.environ["SQL_PROXY_KEY"]

    tenant_id = "t_" + _secrets.token_hex(6)
    db_name = f"tenant_{tenant_id}"
    role_name = role_name_for_tenant(tenant_id)

    # Pre-create the DB without a role.
    mod.admin_create_db(db_name)
    try:
        # First migration: creates the role + transfers ownership.
        result1 = mod.admin_migrate_tenant_to_role(db_name)
        assert result1["migrated"] is True
        assert result1["role"] == role_name

        # Tenant role can now connect.
        tenant_dsn = mod._dsn_for_db(db_name)
        with psycopg.connect(tenant_dsn, connect_timeout=5) as conn:
            conn.execute("SELECT 1")

        # Second migration: idempotent, should also succeed.
        result2 = mod.admin_migrate_tenant_to_role(db_name)
        assert result2["migrated"] is True
        with psycopg.connect(tenant_dsn, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
    finally:
        try:
            mod.admin_drop_tenant_with_role(db_name, tenant_id)
        except Exception:
            pass


@_PG_SUPERUSER
def test_migrate_tenant_to_role_skips_non_tenant_dbs():
    mod = _load_sql_proxy_module()
    mod.DB_DSN = TEST_DSN
    os.environ["SQL_PROXY_KEY"] = "test-key"
    mod.PROXY_KEY = "test-key"

    result = mod.admin_migrate_tenant_to_role("hivemind_control")
    assert result.get("skipped")
    assert "not a tenant DB" in result["skipped"]


@_PG_SUPERUSER
def test_tenant_role_cannot_access_other_tenant_db():
    """Cross-tenant access is denied by Postgres role auth."""
    mod = _load_sql_proxy_module()
    mod.DB_DSN = TEST_DSN
    os.environ["SQL_PROXY_KEY"] = "test-proxy-key-" + _secrets.token_hex(4)
    mod.PROXY_KEY = os.environ["SQL_PROXY_KEY"]

    t_a = "t_" + _secrets.token_hex(6)
    t_b = "t_" + _secrets.token_hex(6)
    db_a = f"tenant_{t_a}"
    db_b = f"tenant_{t_b}"

    try:
        mod.admin_create_tenant_with_role(db_a, t_a)
        mod.admin_create_tenant_with_role(db_b, t_b)

        # Build a DSN that points at tenant B's database but with tenant
        # A's role credentials. That's exactly the attack we're blocking.
        parsed = psycopg.conninfo.conninfo_to_dict(TEST_DSN)
        parsed["dbname"] = db_b
        parsed["user"] = role_name_for_tenant(t_a)
        parsed["password"] = derive_tenant_role_password(
            mod.PROXY_KEY.encode(), t_a,
        )
        hostile_dsn = psycopg.conninfo.make_conninfo(**parsed)

        with pytest.raises(psycopg.errors.Error):
            psycopg.connect(hostile_dsn, connect_timeout=5)
    finally:
        for (db, tid) in [(db_a, t_a), (db_b, t_b)]:
            try:
                mod.admin_drop_tenant_with_role(db, tid)
            except Exception:
                pass

"""Lightweight HTTP-to-PostgreSQL proxy.

Runs as a sidecar in the postgres CVM, exposing HTTP endpoints so that
hivemind-core (in a separate CVM) can execute SQL without a raw TCP
postgres connection.

Data-plane endpoints (gated by X-Proxy-Key):
    POST /execute         — SELECT queries, returns {"rows": [...]}
    POST /execute_commit  — write queries, returns {"rowcount": N}
    GET  /schema          — information_schema introspection
    POST /import/sql      — multi-statement SQL dump in one transaction
    POST /import/csv      — COPY CSV data into a table

Any data-plane call MAY include `X-Tenant-DB: <db_name>` to route to a
specific tenant database. Default (header absent) uses DATABASE_URL's
database. The proxy maintains a per-DB connection pool.

Admin endpoints (gated by X-Admin-Key):
    POST   /admin/tenants            — {"db_name": "..."} creates a fresh DB
    DELETE /admin/tenants/<db_name>  — drops the DB and its pool entry
    GET    /admin/tenants            — lists all hivemind-owned DBs
    POST   /admin/rename-database    — {"old_name": "...", "new_name": "..."}
                                       ALTER DATABASE … RENAME TO (one-shot migration)

Public:
    GET  /health          — liveness check

Zero external dependencies beyond psycopg[binary] — uses stdlib http.server.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg import conninfo as pg_conninfo
from psycopg import sql as psql
from psycopg.rows import dict_row

logging.basicConfig(
    level=logging.INFO,
    format="[sql-proxy] %(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("sql_proxy")

# ---------- Config ----------

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://hivemind:hivemind@localhost:5432/hivemind",
)
PROXY_PORT = int(os.environ.get("SQL_PROXY_PORT", "8080"))
PROXY_KEY = os.environ.get("SQL_PROXY_KEY", "")
ADMIN_KEY = os.environ.get("SQL_PROXY_ADMIN_KEY", "")

# Per-statement runtime cap, in milliseconds. Pinned at the connection level
# so a runaway query (`SELECT pg_sleep(3600)`, accidental cross-join) cannot
# starve the per-DB connection-pool slot. Postgres aborts the statement at
# this deadline; the caller surfaces it as a normal SQL error. Mirror the
# value in hivemind/db.py — both connections live behind the same trust
# boundary and there is no reason for them to drift.
_STATEMENT_TIMEOUT_MS = int(os.environ.get("SQL_PROXY_STATEMENT_TIMEOUT_MS", "30000"))
_MAX_RESULT_ROWS = int(os.environ.get("SQL_PROXY_MAX_RESULT_ROWS", "10000"))
_MAX_RESULT_BYTES = int(os.environ.get("SQL_PROXY_MAX_RESULT_BYTES", str(16 * 1024 * 1024)))


def _dsn_with_statement_timeout(dsn: str) -> str:
    """Append ``-c statement_timeout`` to the DSN's options field."""
    try:
        parts = pg_conninfo.conninfo_to_dict(dsn)
    except Exception:
        return dsn
    extra = f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}"
    existing = parts.get("options", "")
    parts["options"] = (existing + " " + extra).strip() if existing else extra
    try:
        return pg_conninfo.make_conninfo(**parts)
    except Exception:
        return dsn

# Tenant DB naming: lowercase letters, digits, underscores; must start with
# a letter; 1–63 chars (Postgres identifier limit).
_DB_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# Reserved DB names the admin API refuses to create/drop.
_RESERVED_DBS = {"postgres", "template0", "template1"}

# Tenant DB naming convention: `tenant_<tenant_id>` where tenant_id is
# `t_<hex>`. This is enforced by hivemind/tenants.py when provisioning.
_TENANT_DB_RE = re.compile(r"^tenant_(t_[0-9a-f]+)$")

# ---------- Per-tenant role derivation (Layer 1 isolation) ----------
#
# Keep in sync with `hivemind/_pg_roles.py` — this file intentionally has
# no hivemind imports (stdlib + psycopg only). If you change the
# derivation here, mirror the change there, and vice versa.


def _derive_role_password(seed: bytes, tenant_id: str) -> str:
    """Deterministic Postgres password for a tenant's role.

    Output: url-safe base64, no padding, 43 chars (256 bits of entropy).
    """
    if not seed:
        raise ValueError("seed must be non-empty")
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    info = f"pg-role-v1:{tenant_id}".encode("utf-8")
    block = hmac.new(seed, info + b"\x01", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(block).rstrip(b"=").decode("ascii")


def _role_name_for_tenant(tenant_id: str) -> str:
    """`tenant_<id>_role`. Must fit in Postgres' 63-char identifier limit."""
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    name = f"tenant_{tenant_id}_role"
    if len(name) > 63:
        raise ValueError(f"role name too long (>{63} chars): {name}")
    return name


def _parse_tenant_id_from_db_name(db_name: str | None) -> str | None:
    """Extract `tenant_id` from a tenant DB name, or None if not tenant-shaped."""
    if not db_name:
        return None
    m = _TENANT_DB_RE.match(db_name)
    return m.group(1) if m else None


def _tenant_role_seed() -> bytes | None:
    """Return the seed bytes for role-password derivation, or None if unset.

    Defaults to the same value as `PROXY_KEY` so operators don't need a
    second secret — the proxy key is already server-held and rotates
    together with the tenant roles (ALTER ROLE would be required on
    rotation).
    """
    seed_str = os.environ.get("SQL_PROXY_ROLE_SEED") or PROXY_KEY
    if not seed_str:
        return None
    return seed_str.encode("utf-8")

# ---------- DB helpers ----------

_pool_lock = threading.RLock()
_pool: dict[str, psycopg.Connection] = {}
_db_locks: dict[str, threading.RLock] = {}
_admin_conn: psycopg.Connection | None = None
_default_db_name: str | None = None


def _dsn_for_db(db_name: str | None) -> str:
    """Return a DSN targeting `db_name`, or DB_DSN unchanged if None.

    For tenant DBs (matching `tenant_t_*`), rewrite `user` and `password`
    to the per-tenant role so the data plane never connects as superuser.
    Control DBs and the proxy's own default DB keep DB_DSN's credentials.
    """
    if db_name is None:
        return DB_DSN
    parsed = pg_conninfo.conninfo_to_dict(DB_DSN)
    parsed["dbname"] = db_name

    tenant_id = _parse_tenant_id_from_db_name(db_name)
    if tenant_id is not None:
        seed = _tenant_role_seed()
        if seed is not None:
            parsed["user"] = _role_name_for_tenant(tenant_id)
            parsed["password"] = _derive_role_password(seed, tenant_id)
    return pg_conninfo.make_conninfo(**parsed)


def _validate_db_name(name: str) -> None:
    if not _DB_NAME_RE.match(name or ""):
        raise ValueError(
            f"invalid db_name '{name}': must match [a-z][a-z0-9_]*, ≤63 chars"
        )
    if name in _RESERVED_DBS:
        raise ValueError(f"db_name '{name}' is reserved")


def _get_conn(db_name: str | None) -> psycopg.Connection:
    """Get (or open) a per-DB connection. Data-plane only; autocommit=False."""
    dsn = _dsn_for_db(db_name)
    with _pool_lock:
        conn = _pool.get(dsn)
        if conn is None or conn.closed:
            conn = psycopg.connect(
                _dsn_with_statement_timeout(dsn),
                row_factory=dict_row,
                autocommit=False,
            )
            _pool[dsn] = conn
        return conn


def _lock_for_db(db_name: str | None) -> threading.RLock:
    """Return the lock protecting one pooled connection."""
    key = _dsn_for_db(db_name)
    with _pool_lock:
        lock = _db_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _db_locks[key] = lock
        return lock


def _get_admin_conn() -> psycopg.Connection:
    """Admin connection for CREATE/DROP DATABASE (autocommit)."""
    global _admin_conn
    with _pool_lock:
        if _admin_conn is None or _admin_conn.closed:
            _admin_conn = psycopg.connect(
                _dsn_with_statement_timeout(DB_DSN),
                row_factory=dict_row,
                autocommit=True,
            )
        return _admin_conn


def _drop_pool_entry(db_name: str) -> None:
    """Close and remove a per-DB pooled connection."""
    dsn = _dsn_for_db(db_name)
    with _pool_lock:
        conn = _pool.pop(dsn, None)
        _db_locks.pop(dsn, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def db_execute(sql: str, params: list | None, db_name: str | None) -> list[dict]:
    with _lock_for_db(db_name):
        conn = _get_conn(db_name)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or [])
                if cur.description is None:
                    conn.rollback()
                    return []
                rows = []
                approx_bytes = 2  # []
                for idx, row in enumerate(cur, start=1):
                    if idx > _MAX_RESULT_ROWS:
                        raise ValueError(
                            "SQL result row cap exceeded "
                            f"({_MAX_RESULT_ROWS}); add LIMIT, aggregate in "
                            "SQL, or narrow the query"
                        )
                    item = dict(row)
                    approx_bytes += (
                        len(json.dumps(item, default=_default_json)) + 1
                    )
                    if approx_bytes > _MAX_RESULT_BYTES:
                        raise ValueError(
                            "SQL result response cap exceeded "
                            f"({_MAX_RESULT_BYTES} bytes); select fewer "
                            "columns, aggregate in SQL, or narrow the query"
                        )
                    rows.append(item)
            conn.rollback()  # read-only, no commit needed
            return rows
        except Exception:
            conn.rollback()
            raise


def db_execute_commit(sql: str, params: list | None, db_name: str | None) -> int:
    with _lock_for_db(db_name):
        conn = _get_conn(db_name)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or [])
                rowcount = cur.rowcount
            conn.commit()
            return rowcount
        except Exception:
            conn.rollback()
            raise


def db_import_sql(sql_dump: str, db_name: str | None) -> dict:
    """Execute a multi-statement SQL dump in a single transaction."""
    with _lock_for_db(db_name):
        conn = _get_conn(db_name)
        total = 0
        try:
            with conn.cursor() as cur:
                statements = _split_sql(sql_dump)
                for stmt in statements:
                    stmt = stmt.strip()
                    if not stmt:
                        continue
                    cur.execute(stmt)
                    total += 1
            conn.commit()
            return {"statements_executed": total}
        except Exception as e:
            conn.rollback()
            raise RuntimeError(f"Import failed at statement {total + 1}: {e}")


def db_import_csv(
    table: str,
    csv_data: str,
    delimiter: str = ",",
    header: bool = True,
    columns: list[str] | None = None,
    db_name: str | None = None,
) -> int:
    """COPY CSV data into a table using Postgres COPY protocol."""
    if len(delimiter) != 1:
        raise ValueError("Delimiter must be a single character")

    with _lock_for_db(db_name):
        conn = _get_conn(db_name)
        try:
            with conn.cursor() as cur:
                table_id = psql.Identifier(table)
                if columns:
                    col_ids = psql.SQL(", ").join(
                        psql.Identifier(c) for c in columns
                    )
                    col_spec = psql.SQL(" ({})").format(col_ids)
                else:
                    col_spec = psql.SQL("")
                header_opt = psql.SQL(", HEADER") if header else psql.SQL("")
                copy_sql = psql.SQL(
                    "COPY {} {} FROM STDIN WITH (FORMAT CSV, DELIMITER {}{})"
                ).format(
                    table_id, col_spec, psql.Literal(delimiter), header_opt,
                )
                with cur.copy(copy_sql) as copy:
                    copy.write(csv_data.encode("utf-8"))
                rowcount = cur.rowcount
            conn.commit()
            return rowcount
        except Exception as e:
            conn.rollback()
            raise RuntimeError(f"CSV import into '{table}' failed: {e}")


def _split_sql(sql: str) -> list[str]:
    """Split SQL on top-level semicolons, respecting $$ dollar-quoting and strings."""
    statements = []
    current: list[str] = []
    in_dollar = False
    in_single_quote = False
    i = 0
    while i < len(sql):
        ch = sql[i]

        if in_single_quote:
            current.append(ch)
            if ch == "'" and (i + 1 >= len(sql) or sql[i + 1] != "'"):
                in_single_quote = False
            elif ch == "'" and i + 1 < len(sql) and sql[i + 1] == "'":
                current.append("'")
                i += 1
            i += 1
            continue

        if ch == "'" and not in_dollar:
            in_single_quote = True
            current.append(ch)
            i += 1
            continue

        if ch == "$" and not in_dollar:
            end = sql.find("$", i + 1)
            if end != -1:
                tag = sql[i:end + 1]
                current.append(tag)
                i = end + 1
                in_dollar = tag
                continue

        if in_dollar and ch == "$":
            tag = in_dollar
            if sql[i:i + len(tag)] == tag:
                current.append(tag)
                i += len(tag)
                in_dollar = False
                continue

        if ch == ";" and not in_dollar:
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
            i += 1
            continue

        current.append(ch)
        i += 1

    stmt = "".join(current).strip()
    if stmt:
        statements.append(stmt)

    return statements


def db_schema(exclude_internal: bool, db_name: str | None) -> list[dict]:
    rows = db_execute(
        """
        SELECT table_name, column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
        """,
        None, db_name,
    )
    if exclude_internal:
        rows = [r for r in rows if not r["table_name"].startswith("_hivemind_")]
    return rows


# ---------- Admin operations ----------


def admin_create_db(db_name: str) -> None:
    """CREATE DATABASE <name>. Raises if it already exists or name invalid."""
    _validate_db_name(db_name)
    conn = _get_admin_conn()
    with conn.cursor() as cur:
        cur.execute(
            psql.SQL("CREATE DATABASE {}").format(psql.Identifier(db_name))
        )


def admin_drop_db(db_name: str) -> None:
    """DROP DATABASE <name> WITH (FORCE). Closes any pool entry first."""
    _validate_db_name(db_name)
    if _default_db_name and db_name == _default_db_name:
        raise ValueError("cannot drop the proxy's own default database")
    _drop_pool_entry(db_name)
    conn = _get_admin_conn()
    with conn.cursor() as cur:
        cur.execute(
            psql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                psql.Identifier(db_name)
            )
        )


def admin_rename_db(old_name: str, new_name: str) -> None:
    """ALTER DATABASE <old> RENAME TO <new>.

    Requires the admin (autocommit) connection and that nobody else is
    connected to the source DB. We proactively close our own pooled entries
    for both names. If another CVM still holds a connection, Postgres will
    raise ObjectInUse — caller must retry after that client disconnects.
    """
    _validate_db_name(old_name)
    _validate_db_name(new_name)
    if _default_db_name and old_name == _default_db_name:
        raise ValueError("cannot rename the proxy's own default database")
    if _default_db_name and new_name == _default_db_name:
        raise ValueError("cannot rename to the proxy's own default database")
    _drop_pool_entry(old_name)
    _drop_pool_entry(new_name)
    conn = _get_admin_conn()
    with conn.cursor() as cur:
        cur.execute(
            psql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                psql.Identifier(old_name), psql.Identifier(new_name),
            )
        )


def admin_list_dbs() -> list[str]:
    """List all non-template databases on the cluster."""
    conn = _get_admin_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT datname FROM pg_database "
            "WHERE datistemplate = false AND datname NOT IN ('postgres') "
            "ORDER BY datname"
        )
        return [r["datname"] for r in cur.fetchall()]


# ---------- Per-tenant role admin (Layer 1 isolation) ----------


def _superuser_conn_to(db_name: str) -> psycopg.Connection:
    """Open a one-shot superuser connection to `db_name` (autocommit).

    Used by admin ops that must issue DDL inside the tenant DB — e.g.
    `ALTER SCHEMA public OWNER`, `REASSIGN OWNED`. Caller is responsible
    for closing.
    """
    parsed = pg_conninfo.conninfo_to_dict(DB_DSN)
    parsed["dbname"] = db_name
    return psycopg.connect(
        _dsn_with_statement_timeout(pg_conninfo.make_conninfo(**parsed)),
        row_factory=dict_row,
        autocommit=True,
    )


def _role_exists(role_name: str) -> bool:
    conn = _get_admin_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role_name])
        return cur.fetchone() is not None


def _db_exists(db_name: str) -> bool:
    conn = _get_admin_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", [db_name])
        return cur.fetchone() is not None


def _ensure_role(tenant_id: str) -> str:
    """CREATE ROLE or ALTER ROLE to match the derived password. Returns role name."""
    seed = _tenant_role_seed()
    if seed is None:
        raise RuntimeError(
            "cannot manage tenant roles: SQL_PROXY_ROLE_SEED/SQL_PROXY_KEY unset"
        )
    role_name = _role_name_for_tenant(tenant_id)
    password = _derive_role_password(seed, tenant_id)
    conn = _get_admin_conn()
    with conn.cursor() as cur:
        if _role_exists(role_name):
            cur.execute(
                psql.SQL(
                    "ALTER ROLE {} WITH LOGIN PASSWORD {}"
                ).format(psql.Identifier(role_name), psql.Literal(password))
            )
        else:
            cur.execute(
                psql.SQL(
                    "CREATE ROLE {} WITH LOGIN PASSWORD {}"
                ).format(psql.Identifier(role_name), psql.Literal(password))
            )
    return role_name


def _apply_in_tenant_db(db_name: str, role_name: str) -> None:
    """Inside the tenant DB: own `public` + transfer existing user objects.

    We deliberately don't use `REASSIGN OWNED BY <superuser>`: that also
    tries to move system-critical objects (e.g. extension catalog rows),
    which fails with `DependentObjectsStillExist`. Instead we enumerate
    just the user-level objects living in schema `public` and ALTER each
    one's owner. New tables created by the tenant role belong to it by
    default, so this only matters on first provisioning / migration.
    """
    conn = _superuser_conn_to(db_name)
    try:
        with conn.cursor() as cur:
            cur.execute(
                psql.SQL("ALTER SCHEMA public OWNER TO {}").format(
                    psql.Identifier(role_name)
                )
            )

            # Tables + matviews
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "UNION ALL "
                "SELECT matviewname FROM pg_matviews WHERE schemaname = 'public'"
            )
            for row in cur.fetchall():
                name = row["tablename"]
                cur.execute(
                    psql.SQL(
                        "ALTER TABLE public.{} OWNER TO {}"
                    ).format(
                        psql.Identifier(name), psql.Identifier(role_name)
                    )
                )

            # Sequences (auto-created for SERIAL / GENERATED columns)
            cur.execute(
                "SELECT sequence_name FROM information_schema.sequences "
                "WHERE sequence_schema = 'public'"
            )
            for row in cur.fetchall():
                name = row["sequence_name"]
                cur.execute(
                    psql.SQL(
                        "ALTER SEQUENCE public.{} OWNER TO {}"
                    ).format(
                        psql.Identifier(name), psql.Identifier(role_name)
                    )
                )

            # Views
            cur.execute(
                "SELECT table_name FROM information_schema.views "
                "WHERE table_schema = 'public'"
            )
            for row in cur.fetchall():
                name = row["table_name"]
                cur.execute(
                    psql.SQL(
                        "ALTER VIEW public.{} OWNER TO {}"
                    ).format(
                        psql.Identifier(name), psql.Identifier(role_name)
                    )
                )
    finally:
        conn.close()


def admin_create_tenant_with_role(db_name: str, tenant_id: str) -> None:
    """Provision a tenant DB that is owned by a per-tenant Postgres role.

    Flow:
      1. CREATE ROLE tenant_<id>_role LOGIN PASSWORD <derived>
      2. CREATE DATABASE tenant_<id> OWNER tenant_<id>_role
      3. REVOKE CONNECT ON DATABASE tenant_<id> FROM PUBLIC
      4. (inside tenant_<id>) ALTER SCHEMA public OWNER TO tenant_<id>_role
    """
    _validate_db_name(db_name)
    if _parse_tenant_id_from_db_name(db_name) != tenant_id:
        raise ValueError(
            f"db_name '{db_name}' does not match tenant_id '{tenant_id}'"
        )
    role_name = _ensure_role(tenant_id)

    conn = _get_admin_conn()
    with conn.cursor() as cur:
        cur.execute(
            psql.SQL("CREATE DATABASE {} OWNER {}").format(
                psql.Identifier(db_name), psql.Identifier(role_name)
            )
        )
        cur.execute(
            psql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                psql.Identifier(db_name)
            )
        )

    _apply_in_tenant_db(db_name, role_name)


def admin_drop_tenant_with_role(db_name: str, tenant_id: str) -> None:
    """Inverse of `admin_create_tenant_with_role`.

    DROP DATABASE first (which removes any objects the role owns in that
    DB), then DROP ROLE. If the role still owns objects elsewhere the
    DROP ROLE will fail — we let that surface as an error rather than
    silently leaking state.
    """
    _validate_db_name(db_name)
    if _parse_tenant_id_from_db_name(db_name) != tenant_id:
        raise ValueError(
            f"db_name '{db_name}' does not match tenant_id '{tenant_id}'"
        )
    _drop_pool_entry(db_name)

    conn = _get_admin_conn()
    with conn.cursor() as cur:
        cur.execute(
            psql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                psql.Identifier(db_name)
            )
        )
        role_name = _role_name_for_tenant(tenant_id)
        cur.execute(
            psql.SQL("DROP ROLE IF EXISTS {}").format(
                psql.Identifier(role_name)
            )
        )


def admin_migrate_tenant_to_role(db_name: str) -> dict:
    """Idempotently retrofit a role onto an existing tenant DB.

    - Skips DBs whose names don't match `tenant_t_*`.
    - Creates the role if missing (else ALTERs password to the derived value).
    - Transfers DB ownership to the role.
    - Reassigns in-DB objects (REASSIGN OWNED BY <superuser>).
    - REVOKE CONNECT ... FROM PUBLIC.

    Safe to re-run. Returns a small summary dict for the migration report.
    """
    tenant_id = _parse_tenant_id_from_db_name(db_name)
    if tenant_id is None:
        return {"db_name": db_name, "skipped": "not a tenant DB"}
    if not _db_exists(db_name):
        return {"db_name": db_name, "skipped": "database does not exist"}

    role_name = _ensure_role(tenant_id)

    _drop_pool_entry(db_name)

    conn = _get_admin_conn()
    with conn.cursor() as cur:
        cur.execute(
            psql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                psql.Identifier(db_name), psql.Identifier(role_name)
            )
        )
        cur.execute(
            psql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                psql.Identifier(db_name)
            )
        )

    _apply_in_tenant_db(db_name, role_name)
    return {
        "db_name": db_name,
        "tenant_id": tenant_id,
        "role": role_name,
        "migrated": True,
    }


def admin_migrate_all_tenants() -> list[dict]:
    """Run `admin_migrate_tenant_to_role` for every `tenant_t_*` DB."""
    results = []
    for db_name in admin_list_dbs():
        if _parse_tenant_id_from_db_name(db_name) is None:
            continue
        try:
            results.append(admin_migrate_tenant_to_role(db_name))
        except Exception as e:
            results.append({"db_name": db_name, "error": str(e)})
    return results


# ---------- JSON serializer ----------

def _default_json(obj):
    import decimal
    import datetime
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, datetime.timedelta):
        return obj.total_seconds()
    if isinstance(obj, memoryview):
        return obj.tobytes().decode("utf-8", errors="replace")
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"Not JSON serializable: {type(obj)}")


# ---------- HTTP handler ----------


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.info(fmt, *args)

    def _check_auth(self) -> bool:
        # Defense-in-depth: PROXY_KEY MUST be set at process startup
        # (see ``main()`` below). If we ever reach this branch with an
        # empty key, fail closed rather than fall open — early returns
        # in past versions defaulted to True here, which silently turned
        # a misconfigured deploy into an unauthenticated data plane.
        if not PROXY_KEY:
            self._json_response(503, {"error": "proxy misconfigured: SQL_PROXY_KEY unset"})
            return False
        key = self.headers.get("X-Proxy-Key", "")
        if not secrets.compare_digest(key.encode(), PROXY_KEY.encode()):
            self._json_response(401, {"error": "unauthorized"})
            return False
        return True

    def _check_admin(self) -> bool:
        if not ADMIN_KEY:
            self._json_response(
                503, {"error": "admin API disabled (SQL_PROXY_ADMIN_KEY unset)"}
            )
            return False
        key = self.headers.get("X-Admin-Key", "")
        if not secrets.compare_digest(key.encode(), ADMIN_KEY.encode()):
            self._json_response(401, {"error": "unauthorized"})
            return False
        return True

    def _tenant_db(self) -> str | None:
        name = self.headers.get("X-Tenant-DB", "").strip() or None
        if name is not None:
            _validate_db_name(name)
        return name

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _read_raw_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return b""
        return self.rfile.read(length)

    def _json_response(self, status: int, data: dict):
        body = json.dumps(data, default=_default_json).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- GET --

    def do_GET(self):
        if self.path == "/health":
            try:
                db_execute("SELECT 1", None, None)
                self._json_response(200, {"status": "ok"})
            except Exception as e:
                self._json_response(503, {"status": "error", "detail": str(e)})
            return

        if self.path == "/admin/tenants":
            if not self._check_admin():
                return
            try:
                dbs = admin_list_dbs()
                self._json_response(200, {"databases": dbs})
            except Exception as e:
                self._json_response(500, {"error": str(e)})
            return

        if self.path.startswith("/schema"):
            if not self._check_auth():
                return
            try:
                tenant_db = self._tenant_db()
            except ValueError as e:
                self._json_response(400, {"error": str(e)})
                return
            exclude = "exclude_internal=false" not in self.path
            try:
                rows = db_schema(exclude, tenant_db)
                self._json_response(200, {"rows": rows})
            except Exception as e:
                self._json_response(500, {"error": str(e)})
            return

        self._json_response(404, {"error": "not found"})

    # -- DELETE --

    def do_DELETE(self):
        if self.path.startswith("/admin/tenants/"):
            if not self._check_admin():
                return
            db_name = self.path.removeprefix("/admin/tenants/").strip("/")
            try:
                _validate_db_name(db_name)
            except ValueError as e:
                self._json_response(400, {"error": str(e)})
                return
            try:
                tenant_id = _parse_tenant_id_from_db_name(db_name)
                if tenant_id is not None and _tenant_role_seed() is not None:
                    admin_drop_tenant_with_role(db_name, tenant_id)
                else:
                    admin_drop_db(db_name)
                self._json_response(200, {"status": "ok", "dropped": db_name})
            except ValueError as e:
                self._json_response(400, {"error": str(e)})
            except Exception as e:
                self._json_response(500, {"error": str(e)})
            return

        self._json_response(404, {"error": "not found"})

    # -- POST --

    def do_POST(self):
        # -- Admin routes (X-Admin-Key) --
        if self.path == "/admin/tenants":
            if not self._check_admin():
                return
            try:
                body = self._read_body()
            except Exception as e:
                self._json_response(400, {"error": f"invalid JSON: {e}"})
                return
            db_name = (body.get("db_name") or "").strip()
            try:
                tenant_id = _parse_tenant_id_from_db_name(db_name)
                if tenant_id is not None and _tenant_role_seed() is not None:
                    admin_create_tenant_with_role(db_name, tenant_id)
                    created_role = _role_name_for_tenant(tenant_id)
                else:
                    admin_create_db(db_name)
                    created_role = None
            except ValueError as e:
                self._json_response(400, {"error": str(e)})
                return
            except psycopg.errors.DuplicateDatabase:
                self._json_response(
                    409, {"error": f"database '{db_name}' already exists"}
                )
                return
            except Exception as e:
                self._json_response(500, {"error": str(e)})
                return
            self._json_response(
                200,
                {
                    "status": "ok",
                    "db_name": db_name,
                    "created": True,
                    "role": created_role,
                },
            )
            return

        if self.path == "/admin/migrate-to-roles":
            if not self._check_admin():
                return
            if _tenant_role_seed() is None:
                self._json_response(
                    503,
                    {
                        "error": (
                            "tenant-role derivation disabled: "
                            "SQL_PROXY_ROLE_SEED/SQL_PROXY_KEY unset"
                        )
                    },
                )
                return
            try:
                results = admin_migrate_all_tenants()
                self._json_response(200, {"status": "ok", "results": results})
            except Exception as e:
                self._json_response(500, {"error": str(e)})
            return

        if self.path == "/admin/rename-database":
            if not self._check_admin():
                return
            try:
                body = self._read_body()
            except Exception as e:
                self._json_response(400, {"error": f"invalid JSON: {e}"})
                return
            old_name = (body.get("old_name") or "").strip()
            new_name = (body.get("new_name") or "").strip()
            try:
                admin_rename_db(old_name, new_name)
            except ValueError as e:
                self._json_response(400, {"error": str(e)})
                return
            except psycopg.errors.DuplicateDatabase:
                self._json_response(
                    409, {"error": f"database '{new_name}' already exists"}
                )
                return
            except psycopg.errors.InvalidCatalogName:
                self._json_response(
                    404, {"error": f"database '{old_name}' not found"}
                )
                return
            except psycopg.errors.ObjectInUse as e:
                self._json_response(
                    409, {"error": f"database '{old_name}' is in use: {e}"}
                )
                return
            except Exception as e:
                self._json_response(500, {"error": str(e)})
                return
            self._json_response(
                200,
                {
                    "status": "ok",
                    "old_name": old_name,
                    "new_name": new_name,
                },
            )
            return

        # -- Data-plane routes (X-Proxy-Key) --
        if not self._check_auth():
            return

        try:
            tenant_db = self._tenant_db()
        except ValueError as e:
            self._json_response(400, {"error": str(e)})
            return

        if self.path == "/import/sql":
            try:
                raw = self._read_raw_body()
                content_type = self.headers.get("Content-Type", "")
                if "json" in content_type:
                    body = json.loads(raw)
                    sql_dump = body.get("sql", "")
                else:
                    sql_dump = raw.decode("utf-8")
                if not sql_dump.strip():
                    self._json_response(400, {"error": "empty SQL dump"})
                    return
                result = db_import_sql(sql_dump, tenant_db)
                self._json_response(200, result)
            except Exception as e:
                self._json_response(400, {"error": str(e)})
            return

        if self.path == "/import/csv":
            try:
                raw = self._read_raw_body()
                content_type = self.headers.get("Content-Type", "")
                if "json" in content_type:
                    body = json.loads(raw)
                    table = body.get("table", "")
                    csv_data = body.get("data", "")
                    delimiter = body.get("delimiter", ",")
                    header = body.get("header", True)
                    columns = body.get("columns")
                else:
                    self._json_response(400, {"error": "use JSON Content-Type"})
                    return
                if not table:
                    self._json_response(400, {"error": "missing 'table' field"})
                    return
                if not csv_data:
                    self._json_response(400, {"error": "missing 'data' field"})
                    return
                rowcount = db_import_csv(
                    table, csv_data, delimiter, header, columns, tenant_db,
                )
                self._json_response(200, {"rows_imported": rowcount})
            except Exception as e:
                self._json_response(400, {"error": str(e)})
            return

        try:
            body = self._read_body()
        except Exception as e:
            self._json_response(400, {"error": f"invalid JSON: {e}"})
            return

        sql = body.get("sql", "")
        params = body.get("params")
        if not sql:
            self._json_response(400, {"error": "missing 'sql' field"})
            return

        if self.path == "/execute":
            try:
                rows = db_execute(sql, params, tenant_db)
                self._json_response(200, {"rows": rows})
            except Exception as e:
                self._json_response(400, {"error": str(e)})
            return

        if self.path == "/execute_commit":
            try:
                rowcount = db_execute_commit(sql, params, tenant_db)
                self._json_response(200, {"rowcount": rowcount})
            except Exception as e:
                self._json_response(400, {"error": str(e)})
            return

        self._json_response(404, {"error": "not found"})


# ---------- Main ----------


def _resolve_default_db_name() -> str:
    parsed = pg_conninfo.conninfo_to_dict(DB_DSN)
    return parsed.get("dbname", "")


def main():
    global _default_db_name
    # Refuse to start without a data-plane key. The previous behavior emitted
    # a warning and continued — any caller reachable on the network could
    # then run arbitrary SQL against any tenant database (since _check_auth
    # short-circuited to True when PROXY_KEY was empty). A misconfigured
    # deploy is much safer crashing loudly than silently fail-open. Operators
    # who genuinely need an unauthenticated proxy can set the variable to a
    # known sentinel, but the platform never assumes the empty default.
    if not PROXY_KEY:
        logger.error(
            "SQL_PROXY_KEY is unset — refusing to start. Set the env var to "
            "a non-empty value before launching the proxy."
        )
        sys.exit(2)
    if not ADMIN_KEY:
        logger.warning(
            "SQL_PROXY_ADMIN_KEY not set — admin API disabled"
        )
    _default_db_name = _resolve_default_db_name()
    logger.info("Default database: %s", _default_db_name)

    import time
    for attempt in range(30):
        try:
            _get_conn(None)
            logger.info("Connected to postgres")
            break
        except Exception as e:
            if attempt < 29:
                logger.info("Waiting for postgres... (%s)", e)
                time.sleep(2)
            else:
                logger.error("Could not connect to postgres after 60s")
                sys.exit(1)

    server = ThreadingHTTPServer(("0.0.0.0", PROXY_PORT), ProxyHandler)
    logger.info("SQL proxy listening on port %d", PROXY_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()

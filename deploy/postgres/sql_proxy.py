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

import json
import logging
import os
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

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

# Tenant DB naming: lowercase letters, digits, underscores; must start with
# a letter; 1–63 chars (Postgres identifier limit).
_DB_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# Reserved DB names the admin API refuses to create/drop.
_RESERVED_DBS = {"postgres", "template0", "template1"}

# ---------- DB helpers ----------

_pool_lock = threading.RLock()
_pool: dict[str, psycopg.Connection] = {}
_admin_conn: psycopg.Connection | None = None
_default_db_name: str | None = None


def _dsn_for_db(db_name: str | None) -> str:
    """Return a DSN targeting `db_name`, or DB_DSN unchanged if None."""
    if db_name is None:
        return DB_DSN
    parsed = pg_conninfo.conninfo_to_dict(DB_DSN)
    parsed["dbname"] = db_name
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
            conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
            _pool[dsn] = conn
        return conn


def _get_admin_conn() -> psycopg.Connection:
    """Admin connection for CREATE/DROP DATABASE (autocommit)."""
    global _admin_conn
    with _pool_lock:
        if _admin_conn is None or _admin_conn.closed:
            _admin_conn = psycopg.connect(
                DB_DSN, row_factory=dict_row, autocommit=True
            )
        return _admin_conn


def _drop_pool_entry(db_name: str) -> None:
    """Close and remove a per-DB pooled connection."""
    dsn = _dsn_for_db(db_name)
    with _pool_lock:
        conn = _pool.pop(dsn, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def db_execute(sql: str, params: list | None, db_name: str | None) -> list[dict]:
    with _pool_lock:
        conn = _get_conn(db_name)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or [])
                if cur.description is None:
                    conn.rollback()
                    return []
                rows = [dict(row) for row in cur.fetchall()]
            conn.rollback()  # read-only, no commit needed
            return rows
        except Exception:
            conn.rollback()
            raise


def db_execute_commit(sql: str, params: list | None, db_name: str | None) -> int:
    with _pool_lock:
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
    with _pool_lock:
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

    with _pool_lock:
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
        if not PROXY_KEY:
            return True
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
                admin_create_db(db_name)
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
                200, {"status": "ok", "db_name": db_name, "created": True}
            )
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
    if not PROXY_KEY:
        logger.warning("SQL_PROXY_KEY not set — data plane unauthenticated!")
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

    class _ThreadingServer(HTTPServer):
        # Override to handle concurrent requests — the GIL + per-DB lock
        # still serialize DB work, but this frees up connection pool reuse.
        pass

    server = HTTPServer(("0.0.0.0", PROXY_PORT), ProxyHandler)
    logger.info("SQL proxy listening on port %d", PROXY_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()

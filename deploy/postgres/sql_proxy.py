"""Lightweight HTTP-to-PostgreSQL proxy.

Runs as a sidecar in the postgres CVM, exposing three endpoints over HTTP
so that hivemind-core (in a separate CVM) can execute SQL without a raw
TCP postgres connection.

Endpoints:
    POST /execute         — SELECT queries, returns {"rows": [...]}
    POST /execute_commit  — write queries, returns {"rowcount": N}
    GET  /schema          — information_schema introspection
    GET  /health          — liveness check
    POST /import/sql      — execute a multi-statement SQL dump (DDL+DML) in one transaction
    POST /import/csv      — COPY CSV data into a table

Authentication: shared secret via X-Proxy-Key header.

Zero external dependencies beyond psycopg[binary] — uses stdlib http.server.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg
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

# ---------- DB helpers ----------

_conn_lock = threading.RLock()
_conn: psycopg.Connection | None = None


def _get_conn() -> psycopg.Connection:
    global _conn
    with _conn_lock:
        if _conn is None or _conn.closed:
            logger.info("Connecting to postgres...")
            _conn = psycopg.connect(DB_DSN, row_factory=dict_row, autocommit=False)
        return _conn


def db_execute(sql: str, params: list | None = None) -> list[dict]:
    with _conn_lock:
        conn = _get_conn()
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


def db_execute_commit(sql: str, params: list | None = None) -> int:
    with _conn_lock:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or [])
                rowcount = cur.rowcount
            conn.commit()
            return rowcount
        except Exception:
            conn.rollback()
            raise


def db_import_sql(sql_dump: str) -> dict:
    """Execute a multi-statement SQL dump in a single transaction.

    Splits on semicolons (respecting $$ dollar-quoting) and executes each
    statement. Returns summary of executed statements.
    """
    with _conn_lock:
        conn = _get_conn()
        total = 0
        try:
            with conn.cursor() as cur:
                # Use psycopg's execute to handle the full dump — it supports
                # multi-statement strings natively when using pipeline or
                # we split manually for better error reporting.
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


def db_import_csv(table: str, csv_data: str, delimiter: str = ",",
                  header: bool = True, columns: list[str] | None = None) -> int:
    """COPY CSV data into a table using Postgres COPY protocol."""
    import io as _io

    with _conn_lock:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                col_spec = f"({', '.join(columns)})" if columns else ""
                header_opt = "HEADER" if header else ""
                copy_sql = (
                    f"COPY {table} {col_spec} FROM STDIN WITH ("
                    f"FORMAT CSV, DELIMITER '{delimiter}', {header_opt})"
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
                i += 1  # skip escaped quote
            i += 1
            continue

        if ch == "'" and not in_dollar:
            in_single_quote = True
            current.append(ch)
            i += 1
            continue

        if ch == "$" and not in_dollar:
            # Check for $$ or $tag$
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

    # Last statement (no trailing semicolon)
    stmt = "".join(current).strip()
    if stmt:
        statements.append(stmt)

    return statements


def db_schema(exclude_internal: bool = True) -> list[dict]:
    rows = db_execute(
        """
        SELECT table_name, column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
        """
    )
    if exclude_internal:
        rows = [r for r in rows if not r["table_name"].startswith("_hivemind_")]
    return rows


# ---------- JSON serializer ----------

def _default_json(obj):
    """Handle types that stdlib json can't serialize (Decimal, date, etc.)."""
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
        if key != PROXY_KEY:
            self._json_response(401, {"error": "unauthorized"})
            return False
        return True

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

    def do_GET(self):
        if self.path == "/health":
            try:
                db_execute("SELECT 1")
                self._json_response(200, {"status": "ok"})
            except Exception as e:
                self._json_response(503, {"status": "error", "detail": str(e)})
            return

        if self.path.startswith("/schema"):
            if not self._check_auth():
                return
            exclude = "exclude_internal=false" not in self.path
            try:
                rows = db_schema(exclude_internal=exclude)
                self._json_response(200, {"rows": rows})
            except Exception as e:
                self._json_response(500, {"error": str(e)})
            return

        self._json_response(404, {"error": "not found"})

    def do_POST(self):
        if not self._check_auth():
            return

        # --- Import endpoints (handle separately) ---

        if self.path == "/import/sql":
            try:
                raw = self._read_raw_body()
                content_type = self.headers.get("Content-Type", "")
                if "json" in content_type:
                    body = json.loads(raw)
                    sql_dump = body.get("sql", "")
                else:
                    # Accept raw SQL text directly
                    sql_dump = raw.decode("utf-8")
                if not sql_dump.strip():
                    self._json_response(400, {"error": "empty SQL dump"})
                    return
                result = db_import_sql(sql_dump)
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
                    # Multipart or raw CSV — table name from query param
                    from urllib.parse import urlparse, parse_qs
                    # Not reached in practice, require JSON
                    self._json_response(400, {"error": "use JSON Content-Type"})
                    return
                if not table:
                    self._json_response(400, {"error": "missing 'table' field"})
                    return
                if not csv_data:
                    self._json_response(400, {"error": "missing 'data' field"})
                    return
                rowcount = db_import_csv(table, csv_data, delimiter, header, columns)
                self._json_response(200, {"rows_imported": rowcount})
            except Exception as e:
                self._json_response(400, {"error": str(e)})
            return

        # --- Standard query endpoints ---

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
                rows = db_execute(sql, params)
                self._json_response(200, {"rows": rows})
            except Exception as e:
                self._json_response(400, {"error": str(e)})
            return

        if self.path == "/execute_commit":
            try:
                rowcount = db_execute_commit(sql, params)
                self._json_response(200, {"rowcount": rowcount})
            except Exception as e:
                self._json_response(400, {"error": str(e)})
            return

        self._json_response(404, {"error": "not found"})


# ---------- Main ----------

def main():
    if not PROXY_KEY:
        logger.warning("SQL_PROXY_KEY not set — proxy is unauthenticated!")

    # Wait for postgres to be ready
    import time
    for attempt in range(30):
        try:
            _get_conn()
            logger.info("Connected to postgres")
            break
        except Exception as e:
            if attempt < 29:
                logger.info("Waiting for postgres... (%s)", e)
                time.sleep(2)
            else:
                logger.error("Could not connect to postgres after 60s")
                sys.exit(1)

    server = HTTPServer(("0.0.0.0", PROXY_PORT), ProxyHandler)
    logger.info("SQL proxy listening on port %d", PROXY_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()

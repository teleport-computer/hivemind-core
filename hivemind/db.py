"""Thin Postgres connection wrapper.

Provides execute/execute_commit/get_schema over a single psycopg connection.
Bootstraps internal _hivemind_agents and _hivemind_agent_files tables on init.

When the DSN starts with ``http://`` or ``https://``, :func:`connect` returns
an :class:`HttpDatabase` that proxies SQL over the HTTP SQL proxy sidecar
(see ``deploy/postgres/sql_proxy.py``).
"""

from __future__ import annotations

import logging
import threading

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

# Internal tables managed by hivemind — hidden from get_schema by default
_INTERNAL_PREFIX = "_hivemind_"


def connect(dsn: str, proxy_key: str = "") -> Database | HttpDatabase:
    """Create the right Database depending on DSN scheme."""
    if dsn.startswith("http://") or dsn.startswith("https://"):
        return HttpDatabase(dsn, proxy_key=proxy_key)
    return Database(dsn)


class Database:
    """Thin Postgres wrapper with thread-safe connection reuse."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
        self._lock = threading.RLock()
        self._bootstrap()

    def _bootstrap(self) -> None:
        """Create internal tables if they don't exist."""
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS _hivemind_agents (
                        agent_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        image TEXT NOT NULL,
                        entrypoint TEXT,
                        memory_mb INTEGER NOT NULL DEFAULT 256,
                        max_llm_calls INTEGER NOT NULL DEFAULT 20,
                        max_tokens INTEGER NOT NULL DEFAULT 100000,
                        timeout_seconds INTEGER NOT NULL DEFAULT 120,
                        created_at DOUBLE PRECISION NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS _hivemind_agent_files (
                        agent_id TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        content TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        PRIMARY KEY (agent_id, file_path)
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS _hivemind_query_runs (
                        run_id TEXT PRIMARY KEY,
                        agent_id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        s3_url TEXT,
                        error TEXT,
                        created_at DOUBLE PRECISION NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL,
                        scope_started_at DOUBLE PRECISION,
                        scope_ended_at DOUBLE PRECISION,
                        query_started_at DOUBLE PRECISION,
                        query_ended_at DOUBLE PRECISION,
                        mediator_started_at DOUBLE PRECISION,
                        mediator_ended_at DOUBLE PRECISION,
                        output TEXT
                    )
                """)
                # Migrate existing tables: add columns if missing
                for col, coltype in [
                    ("scope_started_at", "DOUBLE PRECISION"),
                    ("scope_ended_at", "DOUBLE PRECISION"),
                    ("query_started_at", "DOUBLE PRECISION"),
                    ("query_ended_at", "DOUBLE PRECISION"),
                    ("mediator_started_at", "DOUBLE PRECISION"),
                    ("mediator_ended_at", "DOUBLE PRECISION"),
                    ("output", "TEXT"),
                ]:
                    try:
                        cur.execute(
                            f"ALTER TABLE _hivemind_query_runs "
                            f"ADD COLUMN IF NOT EXISTS {col} {coltype}"
                        )
                    except Exception:
                        pass  # column already exists
            self._conn.commit()

    def execute(self, sql: str, params: list | tuple | None = None) -> list[dict]:
        """Run a SELECT query and return rows as list of dicts."""
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(sql, params or [])
                if cur.description is None:
                    return []
                return [dict(row) for row in cur.fetchall()]

    def execute_commit(self, sql: str, params: list | tuple | None = None) -> int:
        """Run a write query, commit, and return rowcount."""
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(sql, params or [])
                rowcount = cur.rowcount
            self._conn.commit()
            return rowcount

    def get_schema(self, exclude_internal: bool = True) -> list[dict]:
        """Introspect information_schema for table/column metadata."""
        sql = """
            SELECT table_name, column_name, data_type, is_nullable,
                   column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
        """
        rows = self.execute(sql)
        if exclude_internal:
            rows = [r for r in rows if not r["table_name"].startswith(_INTERNAL_PREFIX)]
        return rows

    def close(self) -> None:
        """Close the underlying connection."""
        try:
            self._conn.close()
        except Exception:
            pass


class HttpDatabase:
    """Database-compatible client that proxies SQL over HTTP.

    Used when hivemind-core and Postgres are in separate Phala CVMs,
    connected via the sql_proxy sidecar.
    """

    def __init__(self, base_url: str, proxy_key: str = ""):
        import httpx

        self._base_url = base_url.rstrip("/")
        self._headers = {}
        if proxy_key:
            self._headers["X-Proxy-Key"] = proxy_key
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=self._headers,
            timeout=30.0,
        )
        self._bootstrap()

    def _bootstrap(self) -> None:
        """Create internal tables via the proxy."""
        for ddl in [
            """
            CREATE TABLE IF NOT EXISTS _hivemind_agents (
                agent_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                image TEXT NOT NULL,
                entrypoint TEXT,
                memory_mb INTEGER NOT NULL DEFAULT 256,
                max_llm_calls INTEGER NOT NULL DEFAULT 20,
                max_tokens INTEGER NOT NULL DEFAULT 100000,
                timeout_seconds INTEGER NOT NULL DEFAULT 120,
                created_at DOUBLE PRECISION NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS _hivemind_agent_files (
                agent_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                content TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                PRIMARY KEY (agent_id, file_path)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS _hivemind_query_runs (
                run_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                s3_url TEXT,
                error TEXT,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                scope_started_at DOUBLE PRECISION,
                scope_ended_at DOUBLE PRECISION,
                query_started_at DOUBLE PRECISION,
                query_ended_at DOUBLE PRECISION,
                mediator_started_at DOUBLE PRECISION,
                mediator_ended_at DOUBLE PRECISION,
                output TEXT
            )
            """,
        ]:
            self.execute_commit(ddl)
        # Migrate existing tables
        for col, coltype in [
            ("scope_started_at", "DOUBLE PRECISION"),
            ("scope_ended_at", "DOUBLE PRECISION"),
            ("query_started_at", "DOUBLE PRECISION"),
            ("query_ended_at", "DOUBLE PRECISION"),
            ("mediator_started_at", "DOUBLE PRECISION"),
            ("mediator_ended_at", "DOUBLE PRECISION"),
            ("output", "TEXT"),
        ]:
            try:
                self.execute_commit(
                    f"ALTER TABLE _hivemind_query_runs "
                    f"ADD COLUMN IF NOT EXISTS {col} {coltype}"
                )
            except Exception:
                pass

    def _check(self, resp) -> dict:
        if resp.status_code >= 400:
            data = resp.json()
            raise RuntimeError(
                f"SQL proxy error ({resp.status_code}): {data.get('error', resp.text)}"
            )
        return resp.json()

    def execute(self, sql: str, params: list | tuple | None = None) -> list[dict]:
        resp = self._client.post(
            "/execute", json={"sql": sql, "params": list(params) if params else None}
        )
        return self._check(resp)["rows"]

    def execute_commit(self, sql: str, params: list | tuple | None = None) -> int:
        resp = self._client.post(
            "/execute_commit",
            json={"sql": sql, "params": list(params) if params else None},
        )
        return self._check(resp)["rowcount"]

    def get_schema(self, exclude_internal: bool = True) -> list[dict]:
        qs = "" if exclude_internal else "?exclude_internal=false"
        resp = self._client.get(f"/schema{qs}")
        return self._check(resp)["rows"]

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

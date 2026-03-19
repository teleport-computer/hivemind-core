"""Thin Postgres connection wrapper.

Provides execute/execute_commit/get_schema over a single psycopg connection.
Bootstraps internal _hivemind_agents and _hivemind_agent_files tables on init.
"""

from __future__ import annotations

import logging
import threading

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

# Internal tables managed by hivemind — hidden from get_schema by default
_INTERNAL_PREFIX = "_hivemind_"


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

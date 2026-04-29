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
from psycopg import conninfo as _conninfo
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

# Internal tables managed by hivemind — hidden from get_schema by default
_INTERNAL_PREFIX = "_hivemind_"

# Hard cap on per-statement runtime, in milliseconds. Pinned at the connection
# level so a runaway query (`SELECT pg_sleep(3600)`, accidental cross-join,
# etc.) cannot indefinitely hold the single shared connection's RLock and
# starve every other request to this tenant. Postgres aborts the statement
# after this; the application surfaces it as a normal SQL error.
_STATEMENT_TIMEOUT_MS = 30_000


def _dsn_with_statement_timeout(dsn: str) -> str:
    """Append ``-c statement_timeout`` to the DSN's ``options`` field.

    Preserves any pre-existing options the operator set. If parsing fails
    (e.g. for a non-standard DSN form), returns the original DSN unchanged
    rather than refusing the connection — the cap is defense-in-depth, not
    an authentication boundary.
    """
    try:
        parts = _conninfo.conninfo_to_dict(dsn)
    except Exception:
        return dsn
    extra = f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}"
    existing = parts.get("options", "")
    parts["options"] = (existing + " " + extra).strip() if existing else extra
    try:
        return _conninfo.make_conninfo(**parts)
    except Exception:
        return dsn


# Single source of truth for hivemind's internal schema. Both Database and
# HttpDatabase iterate this list at bootstrap. Keep DDL idempotent
# (CREATE TABLE/INDEX IF NOT EXISTS) so reboots and HTTP-proxy bootstraps
# are safe to retry.
_INTERNAL_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS _hivemind_agents (
        agent_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        agent_type TEXT NOT NULL DEFAULT 'query',
        image TEXT NOT NULL,
        entrypoint TEXT,
        memory_mb INTEGER NOT NULL DEFAULT 256,
        max_llm_calls INTEGER NOT NULL DEFAULT 20,
        max_tokens INTEGER NOT NULL DEFAULT 100000,
        timeout_seconds INTEGER NOT NULL DEFAULT 120,
        inspection_mode TEXT NOT NULL DEFAULT 'full',
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
    # `attestable=False` excludes a file from `attested_files_digest`
    # (B's verification surface) while still binding it via image_digest.
    # `ciphertext` carries sealed source when inspection_mode='sealed'.
    """
    CREATE TABLE IF NOT EXISTS _hivemind_agent_files (
        agent_id TEXT NOT NULL,
        file_path TEXT NOT NULL,
        content TEXT,
        ciphertext TEXT,
        seal_mode TEXT NOT NULL DEFAULT '',
        room_id TEXT,
        size_bytes INTEGER NOT NULL,
        attestable BOOLEAN NOT NULL DEFAULT TRUE,
        PRIMARY KEY (agent_id, file_path)
    )
    """,
    # Tenant seal: per-tenant-DB DEK wrapped under a KEK derived from the
    # owner's hmk_ key. Singleton row (tenant DB == one tenant). Empty
    # until first owner interaction populates it. Binary fields stored as
    # base64 TEXT so the values survive JSON transport over the SQL HTTP
    # proxy without lossy UTF-8 coercion.
    """
    CREATE TABLE IF NOT EXISTS _hivemind_tenant_kek (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE
            CHECK (singleton),
        salt TEXT NOT NULL,
        wrapped_dek TEXT NOT NULL,
        kdf_params TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
    # `attestation` JSONB carries the CVM-signed run envelope
    # ({body, signature_b64, signer_pubkey_b64}); base64 wrappers travel
    # cleanly over the SQL HTTP proxy. `issuer_token_id` is the 12-hex
    # prefix of the bearer that issued the run (NULL for owner-initiated
    # runs).
    """
    CREATE TABLE IF NOT EXISTS _hivemind_query_runs (
        run_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        room_id TEXT,
        room_manifest_hash TEXT,
        scope_agent_id TEXT,
        index_agent_id TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        s3_url TEXT,
        error TEXT,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        build_started_at DOUBLE PRECISION,
        build_ended_at DOUBLE PRECISION,
        scope_started_at DOUBLE PRECISION,
        scope_ended_at DOUBLE PRECISION,
        query_started_at DOUBLE PRECISION,
        query_ended_at DOUBLE PRECISION,
        mediator_started_at DOUBLE PRECISION,
        mediator_ended_at DOUBLE PRECISION,
        index_started_at DOUBLE PRECISION,
        index_ended_at DOUBLE PRECISION,
        output TEXT,
        index_output TEXT,
        attestation JSONB,
        issuer_token_id TEXT,
        output_visibility TEXT NOT NULL DEFAULT 'owner_and_querier',
        artifacts_enabled BOOLEAN NOT NULL DEFAULT TRUE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS _hivemind_query_runs_room_idx
    ON _hivemind_query_runs (room_id, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS _hivemind_query_artifacts (
        run_id TEXT NOT NULL,
        filename TEXT NOT NULL,
        content BYTEA NOT NULL,
        content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
        size_bytes BIGINT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (run_id, filename)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS _hivemind_query_artifacts_created_idx
    ON _hivemind_query_artifacts (created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS _hivemind_rooms (
        room_id TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        envelope TEXT NOT NULL,
        manifest_hash TEXT NOT NULL,
        scope_agent_id TEXT NOT NULL,
        fixed_query_agent_id TEXT,
        query_mode TEXT NOT NULL,
        output_visibility TEXT NOT NULL,
        allowed_llm_providers TEXT NOT NULL DEFAULT '[]',
        allow_artifacts BOOLEAN NOT NULL DEFAULT FALSE,
        room_policy TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL,
        revoked_at DOUBLE PRECISION
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS _hivemind_rooms_created_idx
    ON _hivemind_rooms (created_at DESC)
    """,
    # Room vault: persistent room data encrypted under a per-room DEK.
    # The DEK is not KMS-derived. Each participant gets a separate wrap
    # row derived from their bearer token, so data stays sealed after a
    # restart until a participant interacts with the room again.
    """
    CREATE TABLE IF NOT EXISTS _hivemind_room_key_wraps (
        room_id TEXT NOT NULL,
        wrap_id TEXT NOT NULL,
        salt TEXT NOT NULL,
        wrapped_dek TEXT NOT NULL,
        kdf_params TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (room_id, wrap_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS _hivemind_room_vault_items (
        room_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        ciphertext TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}',
        created_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (room_id, item_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS _hivemind_room_vault_items_room_idx
    ON _hivemind_room_vault_items (room_id, created_at)
    """,
)

_INTERNAL_MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE _hivemind_query_runs "
    "ADD COLUMN IF NOT EXISTS room_id TEXT",
    "ALTER TABLE _hivemind_query_runs "
    "ADD COLUMN IF NOT EXISTS room_manifest_hash TEXT",
    "ALTER TABLE _hivemind_query_runs "
    "ADD COLUMN IF NOT EXISTS output_visibility TEXT "
    "NOT NULL DEFAULT 'owner_and_querier'",
    "ALTER TABLE _hivemind_query_runs "
    "ADD COLUMN IF NOT EXISTS artifacts_enabled BOOLEAN "
    "NOT NULL DEFAULT TRUE",
    "ALTER TABLE _hivemind_agent_files "
    "ADD COLUMN IF NOT EXISTS seal_mode TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE _hivemind_agent_files "
    "ADD COLUMN IF NOT EXISTS room_id TEXT",
)


def connect(
    dsn: str,
    proxy_key: str = "",
    tenant_db: str | None = None,
) -> Database | HttpDatabase:
    """Create the right Database depending on DSN scheme.

    If `tenant_db` is set and `dsn` is HTTP, the resulting ``HttpDatabase``
    sends ``X-Tenant-DB`` on every request so the sql_proxy routes traffic
    to the named Postgres database. For direct psycopg (non-HTTP) DSNs,
    ``tenant_db`` rewrites the DSN's dbname component.
    """
    if dsn.startswith("http://") or dsn.startswith("https://"):
        return HttpDatabase(dsn, proxy_key=proxy_key, tenant_db=tenant_db)
    if tenant_db:
        # Direct psycopg: rewrite DSN's dbname to the tenant database.
        parsed = _conninfo.conninfo_to_dict(dsn)
        parsed["dbname"] = tenant_db
        dsn = _conninfo.make_conninfo(**parsed)
    return Database(dsn)


class Database:
    """Thin Postgres wrapper with thread-safe connection reuse."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        # autocommit=True: each statement is its own transaction. psycopg
        # auto-rolls-back on failure, so a single SQL error can't poison the
        # shared connection with InFailedSqlTransaction (which would 500 every
        # subsequent request). All writes in this codebase are single-statement
        # so we lose nothing by dropping implicit transactions.
        self._conn = psycopg.connect(
            _dsn_with_statement_timeout(dsn),
            row_factory=dict_row,
            autocommit=True,
        )
        self._lock = threading.RLock()
        self._bootstrap()

    def _bootstrap(self) -> None:
        """Create internal tables if they don't exist."""
        with self._lock:
            with self._conn.cursor() as cur:
                for ddl in _INTERNAL_DDL:
                    cur.execute(ddl)
                for ddl in _INTERNAL_MIGRATIONS:
                    cur.execute(ddl)
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

    def __init__(
        self,
        base_url: str,
        proxy_key: str = "",
        tenant_db: str | None = None,
    ):
        import httpx

        self._base_url = base_url.rstrip("/")
        self._headers: dict[str, str] = {}
        if proxy_key:
            self._headers["X-Proxy-Key"] = proxy_key
        if tenant_db:
            self._headers["X-Tenant-DB"] = tenant_db
        self.tenant_db = tenant_db
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=self._headers,
            timeout=30.0,
        )
        self._bootstrap()

    def _bootstrap(self) -> None:
        """Create internal tables via the proxy."""
        for ddl in _INTERNAL_DDL:
            self.execute_commit(ddl)
        for ddl in _INTERNAL_MIGRATIONS:
            self.execute_commit(ddl)

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

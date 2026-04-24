"""Tenant registry — multi-tenant control plane.

Runs inside the core CVM. Looks up bearer tokens against the control
database, constructs per-tenant Hivemind instances (LRU-cached), and
exposes admin operations (create / list / delete / register existing).

Isolation properties:
- Each tenant's data lives in its own Postgres database (`tenant_<id>`).
- sql_proxy routes SQL calls by `X-Tenant-DB` header — no shared pool.
- Each per-tenant Hivemind has its own db/agent_store/run_store/pipeline.
- Docker image tags are prefixed with tenant_id to avoid collisions.
- The control DB stores hashed API keys only, never plaintext.

Admin sees: tenant metadata (id, name, created_at), and can provision
or delete tenants. Admin does NOT see tenant data, tenant API keys
(hashes only), nor can impersonate a tenant without their key.

Trust model is TEE-enforced: admin has no shell on the CVMs and no
direct DB access (sql_proxy's data key lives only inside core).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any

from .admin_proxy import make_admin
from .config import Settings
from .core import Hivemind
from .db import connect as _db_connect

logger = logging.getLogger(__name__)


_TENANT_ID_PREFIX = "t_"
_API_KEY_PREFIX = "hmk_"


def _new_tenant_id() -> str:
    return _TENANT_ID_PREFIX + secrets.token_hex(6)


def _new_api_key() -> str:
    return _API_KEY_PREFIX + secrets.token_urlsafe(32)


def _hash_api_key(key: str) -> bytes:
    # SHA-256 is fine here — the key has >=256 bits of entropy, so brute
    # force is infeasible even without slow hashing.
    return hashlib.sha256(key.encode()).digest()


class TenantRegistry:
    """Bearer-token → Hivemind resolver with LRU cache."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._cache: "OrderedDict[str, Hivemind]" = OrderedDict()
        self._cache_max = max(1, int(settings.tenant_cache_size))

        # Admin client needs to come first so we can auto-create the
        # control DB if it doesn't exist on this Postgres cluster.
        self._pg_admin: Any | None = None
        if settings.admin_key:
            self._pg_admin = make_admin(
                settings.database_url, settings.sql_proxy_admin_key
            )

        self._control_db = self._connect_control_db()
        self._bootstrap_control_schema()

    def _connect_control_db(self):
        """Connect to the control DB, auto-creating it on first run."""
        try:
            return _db_connect(
                self.settings.database_url,
                proxy_key=self.settings.sql_proxy_key,
                tenant_db=self.settings.control_database,
            )
        except Exception as e:
            # Typical first-run failure: database does not exist. If we
            # have admin privileges, create it and retry once.
            if self._pg_admin is None:
                raise
            msg = str(e).lower()
            if "does not exist" not in msg and "3d000" not in msg:
                raise
            logger.info(
                "Control DB '%s' not found — creating it now",
                self.settings.control_database,
            )
            try:
                self._pg_admin.create_database(self.settings.control_database)
            except Exception as ce:
                raise RuntimeError(
                    f"Failed to auto-create control database "
                    f"'{self.settings.control_database}': {ce}"
                ) from e
            return _db_connect(
                self.settings.database_url,
                proxy_key=self.settings.sql_proxy_key,
                tenant_db=self.settings.control_database,
            )

    def close(self) -> None:
        with self._lock:
            for hm in list(self._cache.values()):
                try:
                    hm.db.close()
                except Exception:
                    pass
            self._cache.clear()
        try:
            self._control_db.close()
        except Exception:
            pass
        if self._pg_admin is not None:
            try:
                self._pg_admin.close()
            except Exception:
                pass

    # ── Control schema ──────────────────────────────────────────────

    def _bootstrap_control_schema(self) -> None:
        self._control_db.execute_commit(
            """
            CREATE TABLE IF NOT EXISTS _tenants (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                api_key_hash BYTEA NOT NULL,
                db_name TEXT NOT NULL UNIQUE,
                created_at DOUBLE PRECISION NOT NULL,
                suspended BOOLEAN NOT NULL DEFAULT FALSE
            )
            """
        )
        self._control_db.execute_commit(
            "CREATE INDEX IF NOT EXISTS _tenants_api_key_hash_idx "
            "ON _tenants (api_key_hash)"
        )

    # ── Admin operations ────────────────────────────────────────────

    def provision(self, name: str) -> dict:
        """Create tenant: new DB, new API key. Returns key once."""
        if not name or not name.strip():
            raise ValueError("tenant name required")
        if self._pg_admin is None:
            raise RuntimeError(
                "tenant provisioning requires admin_key + sql_proxy_admin_key"
            )

        tenant_id = _new_tenant_id()
        db_name = f"tenant_{tenant_id}"
        api_key = _new_api_key()
        api_key_hash = _hash_api_key(api_key)

        self._pg_admin.create_database(db_name)

        try:
            self._control_db.execute_commit(
                "INSERT INTO _tenants "
                "(id, name, api_key_hash, db_name, created_at, suspended) "
                "VALUES (%s, %s, %s, %s, %s, FALSE)",
                [tenant_id, name.strip(), api_key_hash, db_name, time.time()],
            )
        except Exception:
            try:
                self._pg_admin.drop_database(db_name)
            except Exception as drop_err:
                logger.error(
                    "Failed to rollback DB after insert error: %s", drop_err
                )
            raise

        return {
            "tenant_id": tenant_id,
            "api_key": api_key,
            "db_name": db_name,
            "name": name.strip(),
        }

    def register_existing(
        self,
        name: str,
        db_name: str,
        api_key: str | None = None,
        tenant_id: str | None = None,
    ) -> dict:
        """Adopt an existing Postgres database as a tenant.

        Does NOT create or modify the database — assumes tables are already
        set up. Useful for bringing a pre-populated DB under control-plane
        management (e.g., one-time migration, per-customer hosted DBs).

        Pass `tenant_id` to keep the control-plane id aligned with a DB you
        already renamed to `tenant_<tenant_id>`. Validated against the
        standard t_<hex> shape.
        """
        if not name or not name.strip():
            raise ValueError("tenant name required")
        if not db_name:
            raise ValueError("db_name required")

        if tenant_id is not None:
            tenant_id = tenant_id.strip()
            if not tenant_id.startswith(_TENANT_ID_PREFIX):
                raise ValueError(
                    f"tenant_id must start with '{_TENANT_ID_PREFIX}'"
                )
            suffix = tenant_id[len(_TENANT_ID_PREFIX):]
            if not suffix or not all(c in "0123456789abcdef" for c in suffix):
                raise ValueError("tenant_id suffix must be lowercase hex")
        else:
            tenant_id = _new_tenant_id()

        if api_key is None:
            api_key = _new_api_key()
        api_key_hash = _hash_api_key(api_key)

        self._control_db.execute_commit(
            "INSERT INTO _tenants "
            "(id, name, api_key_hash, db_name, created_at, suspended) "
            "VALUES (%s, %s, %s, %s, %s, FALSE)",
            [tenant_id, name.strip(), api_key_hash, db_name, time.time()],
        )

        return {
            "tenant_id": tenant_id,
            "api_key": api_key,
            "db_name": db_name,
            "name": name.strip(),
        }

    def rotate_key(self, tenant_id: str) -> dict:
        """Issue a fresh API key for `tenant_id`. Invalidates the old key.

        Returns ``{"tenant_id": ..., "api_key": "hmk_..."}``. The new key
        is returned once; only its hash is stored. The Hivemind instance
        cached under ``tenant_id`` is not evicted because it depends on
        the tenant's db_name, not on the bearer token.
        """
        rows = self._control_db.execute(
            "SELECT id FROM _tenants WHERE id = %s", [tenant_id]
        )
        if not rows:
            raise KeyError(f"tenant '{tenant_id}' not found")
        new_key = _new_api_key()
        new_hash = _hash_api_key(new_key)
        self._control_db.execute_commit(
            "UPDATE _tenants SET api_key_hash = %s WHERE id = %s",
            [new_hash, tenant_id],
        )
        return {"tenant_id": tenant_id, "api_key": new_key}

    def delete(self, tenant_id: str) -> None:
        """Drop tenant DB, evict from cache, remove control row."""
        rows = self._control_db.execute(
            "SELECT db_name FROM _tenants WHERE id = %s", [tenant_id]
        )
        if not rows:
            raise KeyError(f"tenant '{tenant_id}' not found")
        db_name = rows[0]["db_name"]

        with self._lock:
            hm = self._cache.pop(tenant_id, None)
        if hm is not None:
            try:
                hm.db.close()
            except Exception:
                pass

        if self._pg_admin is not None:
            try:
                self._pg_admin.drop_database(db_name)
            except Exception as e:
                logger.warning(
                    "drop_database('%s') failed: %s "
                    "(removing control row anyway)", db_name, e
                )

        self._control_db.execute_commit(
            "DELETE FROM _tenants WHERE id = %s", [tenant_id]
        )

    def list_tenants(self) -> list[dict]:
        rows = self._control_db.execute(
            "SELECT id, name, db_name, created_at, suspended "
            "FROM _tenants ORDER BY created_at DESC"
        )
        return [dict(r) for r in rows]

    def get_by_id(self, tenant_id: str) -> dict | None:
        rows = self._control_db.execute(
            "SELECT id, name, db_name, created_at, suspended "
            "FROM _tenants WHERE id = %s",
            [tenant_id],
        )
        return dict(rows[0]) if rows else None

    # ── Hot path: bearer → Hivemind ─────────────────────────────────

    def resolve(self, api_key: str) -> tuple[str, Hivemind] | None:
        """Bearer token → (tenant_id, per-tenant Hivemind). None if invalid."""
        if not api_key:
            return None
        api_key_hash = _hash_api_key(api_key)
        rows = self._control_db.execute(
            "SELECT id, db_name, suspended FROM _tenants "
            "WHERE api_key_hash = %s",
            [api_key_hash],
        )
        if not rows:
            return None
        row = rows[0]
        if row["suspended"]:
            return None
        tenant_id = row["id"]
        db_name = row["db_name"]

        with self._lock:
            hm = self._cache.get(tenant_id)
            if hm is not None:
                self._cache.move_to_end(tenant_id)
                return tenant_id, hm

        # Construct outside the lock — bootstrap can take a moment.
        hm = Hivemind(self.settings, tenant_db=db_name, tenant_id=tenant_id)
        try:
            import asyncio
            asyncio.get_running_loop()
            hm.start_retention_sweeper()
        except RuntimeError:
            # No running loop (tests / CLI). Sweeper will start on first
            # await, or never — fine for short-lived clients.
            pass

        with self._lock:
            existing = self._cache.get(tenant_id)
            if existing is not None:
                # Another thread raced us. Close our extra instance.
                try:
                    hm.db.close()
                except Exception:
                    pass
                self._cache.move_to_end(tenant_id)
                return tenant_id, existing
            self._cache[tenant_id] = hm
            self._cache.move_to_end(tenant_id)
            while len(self._cache) > self._cache_max:
                evicted_id, evicted_hm = self._cache.popitem(last=False)
                logger.info("Evicting tenant '%s' from cache", evicted_id)
                try:
                    evicted_hm.db.close()
                except Exception:
                    pass
        return tenant_id, hm

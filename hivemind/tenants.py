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
import json as _json
import logging
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal

from .admin_proxy import make_admin
from .config import Settings
from .core import Hivemind
from .db import connect as _db_connect
from .seal import TenantSealer
from .tenant_seal import ensure_unsealed

logger = logging.getLogger(__name__)


_TENANT_ID_PREFIX = "t_"
_API_KEY_PREFIX = "hmk_"

# Capability-token prefix. Owner tokens stay on hmk_ for backward compat;
# the prefix lets the resolver pick the right table without trying both
# lookups on every request.
_QUERY_TOKEN_PREFIX = "hmq_"  # query-only: submit prompts via the active scope agent


Role = Literal["owner", "query"]


@dataclass(frozen=True)
class Caller:
    """Resolved bearer-token identity.

    Carries the per-tenant Hivemind plus the role / constraints that
    server endpoints use for access decisions. ``constraints`` schema
    depends on role:
      - owner: ``{}``
      - query: ``{"scope_agent_id": <id>}`` — every query through this
        token is forced to use this scope agent (enforced in
        /v1/query/run/submit).
    """

    tenant_id: str
    role: Role
    constraints: dict
    hive: Hivemind
    token_id: str = ""  # hex digest prefix; "" for owner tokens (no row)
    sealed: bool = False  # capability-token landed on a cold-cache tenant


def _new_tenant_id() -> str:
    return _TENANT_ID_PREFIX + secrets.token_hex(6)


def _new_api_key() -> str:
    return _API_KEY_PREFIX + secrets.token_urlsafe(32)


def _new_capability_token(prefix: str) -> str:
    """Mint a fresh capability token with the given prefix (hmq_)."""
    return prefix + secrets.token_urlsafe(32)


def _hash_api_key(key: str) -> str:
    # SHA-256 is fine here — the key has >=256 bits of entropy, so brute
    # force is infeasible even without slow hashing. Return hex so the
    # value survives JSON serialization across the sql-proxy boundary.
    return hashlib.sha256(key.encode()).hexdigest()


def _token_id(token_hash_hex: str) -> str:
    """Short, user-visible id for a capability token (first 12 hex chars
    of its sha256). Stable, collision-resistant for in-tenant listings,
    and never reveals the token itself."""
    return token_hash_hex[:12]


class TenantRegistry:
    """Bearer-token → Hivemind resolver with LRU cache."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._cache: "OrderedDict[str, Hivemind]" = OrderedDict()
        self._cache_max = max(1, int(settings.tenant_cache_size))
        # One process-wide DEK cache, shared across tenants. Lives only
        # in RAM — restart-evicts everything, which is the seal property.
        self.sealer = TenantSealer()

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
                api_key_hash TEXT NOT NULL,
                db_name TEXT NOT NULL UNIQUE,
                created_at DOUBLE PRECISION NOT NULL,
                suspended BOOLEAN NOT NULL DEFAULT FALSE
            )
            """
        )
        # Migrate legacy BYTEA → TEXT for DBs initialized with the older
        # schema. Guard on the actual column type: if it's already TEXT,
        # running the ALTER with USING encode(...::bytea, 'hex') would
        # re-encode the stored hex string as ASCII bytes → double-hex,
        # breaking every tenant lookup on the next boot.
        rows = self._control_db.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = '_tenants' AND column_name = 'api_key_hash'"
        )
        if rows and rows[0]["data_type"] == "bytea":
            try:
                self._control_db.execute_commit(
                    "ALTER TABLE _tenants ALTER COLUMN api_key_hash TYPE TEXT "
                    "USING encode(api_key_hash, 'hex')"
                )
            except Exception:
                pass
        # Repair rows that were already double-encoded by the previous
        # unguarded migration: a 128-char hex string whose decoded bytes
        # are themselves valid 64-char hex is almost certainly a
        # double-encoded hash. Unwrap in place.
        try:
            self._control_db.execute_commit(
                "UPDATE _tenants SET api_key_hash = "
                "convert_from(decode(api_key_hash, 'hex'), 'UTF8') "
                "WHERE length(api_key_hash) = 128 "
                "AND api_key_hash ~ '^[0-9a-f]+$'"
            )
        except Exception:
            pass
        self._control_db.execute_commit(
            "CREATE INDEX IF NOT EXISTS _tenants_api_key_hash_idx "
            "ON _tenants (api_key_hash)"
        )
        # Capability tokens. One row per delegated token; owner key stays
        # in _tenants. ON DELETE CASCADE so tenant deletion sweeps the
        # delegated tokens too. ``kind`` is kept in the schema but only
        # ``'query'`` is accepted by mint after the hmw_ removal.
        self._control_db.execute_commit(
            """
            CREATE TABLE IF NOT EXISTS _capability_tokens (
                token_hash  TEXT PRIMARY KEY,
                tenant_id   TEXT NOT NULL REFERENCES _tenants(id)
                                ON DELETE CASCADE,
                kind        TEXT NOT NULL,
                label       TEXT NOT NULL DEFAULT '',
                constraints TEXT NOT NULL DEFAULT '{}',
                created_at  DOUBLE PRECISION NOT NULL,
                revoked_at  DOUBLE PRECISION
            )
            """
        )
        # Drop the legacy CHECK constraint that allowed kind='write'.
        # Best-effort: if it's already gone (pre-migration DBs) skip.
        for legacy_check in (
            "_capability_tokens_kind_check",
            "_capability_tokens_kind_check1",
        ):
            try:
                self._control_db.execute_commit(
                    f"ALTER TABLE _capability_tokens "
                    f"DROP CONSTRAINT IF EXISTS {legacy_check}"
                )
            except Exception:
                pass
        self._control_db.execute_commit(
            "CREATE INDEX IF NOT EXISTS _capability_tokens_tenant_idx "
            "ON _capability_tokens (tenant_id)"
        )
        # Compose pins. Owner-signed envelopes that authorize one or more
        # ``compose_hash`` values for a scope agent. ``hmq_`` URIs can
        # reference a pin instead of baking a single compose_hash, so
        # they keep working across redeploys the owner has blessed.
        # The envelope is the source of truth; ``pubkey_b64`` is stored
        # alongside as a convenience (it is also embedded in the
        # envelope) so listings can show the signer without parsing.
        self._control_db.execute_commit(
            """
            CREATE TABLE IF NOT EXISTS _tenant_compose_pins (
                pin_id      TEXT PRIMARY KEY,
                tenant_id   TEXT NOT NULL REFERENCES _tenants(id)
                                ON DELETE CASCADE,
                envelope    TEXT NOT NULL,
                pubkey_b64  TEXT NOT NULL,
                created_at  DOUBLE PRECISION NOT NULL,
                revoked_at  DOUBLE PRECISION
            )
            """
        )
        self._control_db.execute_commit(
            "CREATE INDEX IF NOT EXISTS _tenant_compose_pins_tenant_idx "
            "ON _tenant_compose_pins (tenant_id, created_at DESC)"
        )

    # ── Compose-pin operations ──────────────────────────────────────

    def store_compose_pin(
        self,
        tenant_id: str,
        envelope_json: str,
        pubkey_b64: str,
    ) -> dict:
        """Persist a signed compose pin envelope. Caller must have
        verified the signature already.

        ``pin_id`` is the SHA-256 of the envelope (first 12 hex chars) —
        deterministic, so re-submitting the same envelope no-ops cleanly.
        """
        rows = self._control_db.execute(
            "SELECT id FROM _tenants WHERE id = %s", [tenant_id]
        )
        if not rows:
            raise KeyError(f"tenant '{tenant_id}' not found")
        pin_id = hashlib.sha256(envelope_json.encode("utf-8")).hexdigest()[:12]
        self._control_db.execute_commit(
            "INSERT INTO _tenant_compose_pins "
            "(pin_id, tenant_id, envelope, pubkey_b64, created_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (pin_id) DO NOTHING",
            [pin_id, tenant_id, envelope_json, pubkey_b64, time.time()],
        )
        return {"pin_id": pin_id, "tenant_id": tenant_id}

    def list_compose_pins(self, tenant_id: str) -> list[dict]:
        rows = self._control_db.execute(
            "SELECT pin_id, envelope, pubkey_b64, created_at, revoked_at "
            "FROM _tenant_compose_pins WHERE tenant_id = %s "
            "ORDER BY created_at DESC",
            [tenant_id],
        )
        return [dict(r) for r in rows]

    def get_compose_pin(self, tenant_id: str, pin_id: str) -> dict | None:
        rows = self._control_db.execute(
            "SELECT pin_id, envelope, pubkey_b64, created_at, revoked_at "
            "FROM _tenant_compose_pins WHERE tenant_id = %s AND pin_id = %s",
            [tenant_id, pin_id],
        )
        return dict(rows[0]) if rows else None

    def latest_compose_pin(self, tenant_id: str) -> dict | None:
        """Most recent non-revoked pin, or ``None``."""
        rows = self._control_db.execute(
            "SELECT pin_id, envelope, pubkey_b64, created_at, revoked_at "
            "FROM _tenant_compose_pins WHERE tenant_id = %s "
            "AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1",
            [tenant_id],
        )
        return dict(rows[0]) if rows else None

    def revoke_compose_pin(self, tenant_id: str, pin_id: str) -> bool:
        rowcount = self._control_db.execute_commit(
            "UPDATE _tenant_compose_pins SET revoked_at = %s "
            "WHERE tenant_id = %s AND pin_id = %s AND revoked_at IS NULL",
            [time.time(), tenant_id, pin_id],
        )
        return bool(rowcount)

    # ── Capability-token operations ─────────────────────────────────

    def mint_capability(
        self,
        tenant_id: str,
        kind: str,
        label: str,
        constraints: dict | None,
    ) -> dict:
        """Issue a new query token for ``tenant_id``.

        Returns ``{"token": "<plaintext>", "token_id": "<short>", "kind",
        "label", "constraints"}``. The plaintext is shown only at this
        call; only the hash is stored. Validates that the tenant exists
        and that constraints match the kind. Only ``kind='query'`` is
        supported (write tokens were removed).
        """
        if kind != "query":
            raise ValueError("kind must be 'query'")
        constraints = dict(constraints or {})
        sid = (constraints.get("scope_agent_id") or "").strip()
        if not sid:
            raise ValueError(
                "query token requires constraints.scope_agent_id"
            )
        # ``can_upload_query_agent`` (Phase 4) opts a query token into
        # POST /v1/query-agents/submit. Default false: existing tokens
        # keep their narrow capability of submitting prompts only.
        raw_upload = constraints.get("can_upload_query_agent", False)
        if raw_upload not in (True, False):
            raise ValueError(
                "can_upload_query_agent must be a boolean"
            )
        constraints = {
            "scope_agent_id": sid,
            "can_upload_query_agent": bool(raw_upload),
        }

        rows = self._control_db.execute(
            "SELECT id FROM _tenants WHERE id = %s", [tenant_id]
        )
        if not rows:
            raise KeyError(f"tenant '{tenant_id}' not found")

        token = _new_capability_token(_QUERY_TOKEN_PREFIX)
        token_hash = _hash_api_key(token)
        self._control_db.execute_commit(
            "INSERT INTO _capability_tokens "
            "(token_hash, tenant_id, kind, label, constraints, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            [
                token_hash,
                tenant_id,
                kind,
                label.strip(),
                _json.dumps(constraints),
                time.time(),
            ],
        )
        return {
            "token": token,
            "token_id": _token_id(token_hash),
            "kind": kind,
            "label": label.strip(),
            "constraints": constraints,
        }

    def list_capabilities(self, tenant_id: str) -> list[dict]:
        """List non-revoked capability tokens for a tenant. Hashes only."""
        rows = self._control_db.execute(
            "SELECT token_hash, kind, label, constraints, created_at, revoked_at "
            "FROM _capability_tokens WHERE tenant_id = %s "
            "ORDER BY created_at DESC",
            [tenant_id],
        )
        out: list[dict] = []
        for r in rows:
            try:
                cons = _json.loads(r["constraints"]) if r["constraints"] else {}
            except (TypeError, ValueError):
                cons = {}
            out.append(
                {
                    "token_id": _token_id(r["token_hash"]),
                    "kind": r["kind"],
                    "label": r["label"] or "",
                    "constraints": cons,
                    "created_at": r["created_at"],
                    "revoked_at": r["revoked_at"],
                }
            )
        return out

    def revoke_capability(self, tenant_id: str, token_id_prefix: str) -> bool:
        """Revoke a token by its short id. Returns True if a row updated."""
        token_id_prefix = (token_id_prefix or "").strip().lower()
        if len(token_id_prefix) < 6:
            raise ValueError("token_id prefix must be at least 6 hex chars")
        rowcount = self._control_db.execute_commit(
            "UPDATE _capability_tokens SET revoked_at = %s "
            "WHERE tenant_id = %s AND substr(token_hash,1,%s) = %s "
            "AND revoked_at IS NULL",
            [time.time(), tenant_id, len(token_id_prefix), token_id_prefix],
        )
        return bool(rowcount)

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

    def for_tenant(self, tenant_id: str) -> Hivemind | None:
        """Admin-side: load a tenant's Hivemind by tenant_id (no api_key).

        Used by admin endpoints that need to operate on a tenant's data
        plane without minting / rotating its API key. Goes through the
        same LRU cache as ``resolve`` so subsequent tenant requests reuse
        the warmed instance.
        """
        rows = self._control_db.execute(
            "SELECT id, db_name, suspended FROM _tenants WHERE id = %s",
            [tenant_id],
        )
        if not rows or rows[0]["suspended"]:
            return None
        db_name = rows[0]["db_name"]
        with self._lock:
            hm = self._cache.get(tenant_id)
            if hm is not None:
                self._cache.move_to_end(tenant_id)
                return hm
        hm = Hivemind(
            self.settings, tenant_db=db_name, tenant_id=tenant_id,
            sealer=self.sealer,
        )
        with self._lock:
            existing = self._cache.get(tenant_id)
            if existing is not None:
                try:
                    hm.db.close()
                except Exception:
                    pass
                self._cache.move_to_end(tenant_id)
                return existing
            self._cache[tenant_id] = hm
            self._cache.move_to_end(tenant_id)
            while len(self._cache) > self._cache_max:
                evicted_id, evicted_hm = self._cache.popitem(last=False)
                logger.info("Evicting tenant '%s' from cache", evicted_id)
                try:
                    evicted_hm.db.close()
                except Exception:
                    pass
        return hm

    def resolve_any(self, token: str) -> Caller | None:
        """Bearer token → ``Caller``, regardless of role.

        Dispatches by prefix:
          - ``hmk_…`` → owner via :meth:`resolve`
          - ``hmq_…`` → query capability via ``_capability_tokens``

        Returns ``None`` if the token doesn't match any active row, the
        backing tenant is suspended, or the row is revoked. Callers
        should treat ``None`` as a 401.

        Side effect: thaws the per-tenant DEK cache when possible. An
        ``hmk_`` bearer always thaws (and initializes the seal record on
        first contact). An ``hmq_`` bearer cannot derive the owner-bound
        KEK, so it leaves the seal sealed; downstream operations that
        need DEK material will surface ``TenantSealed`` → HTTP 503.
        """
        if not token:
            return None
        if token.startswith(_API_KEY_PREFIX):
            res = self.resolve(token)
            if res is None:
                return None
            tenant_id, hive = res
            unsealed = False
            try:
                unsealed = ensure_unsealed(
                    self.sealer, hive.db, tenant_id, token,
                    can_initialize=True,
                )
            except Exception as e:
                # Seal failures should not break auth — but they should
                # be visible. Capability-bound endpoints will 503 if the
                # cache is needed, surfacing the issue at request time.
                logger.warning(
                    "tenant %s seal thaw raised: %s", tenant_id, e,
                )
            if not unsealed:
                # Wrong owner key for an existing seal record (e.g.
                # post-rotation old key). Treat as 401 — this is the
                # same outcome as if the API-key hash mismatched.
                return None
            return Caller(
                tenant_id=tenant_id,
                role="owner",
                constraints={},
                hive=hive,
                token_id="",
                sealed=False,
            )
        if not token.startswith(_QUERY_TOKEN_PREFIX):
            return None

        token_hash = _hash_api_key(token)
        rows = self._control_db.execute(
            "SELECT c.tenant_id, c.kind, c.constraints, c.revoked_at, "
            "       t.suspended "
            "FROM _capability_tokens c "
            "JOIN _tenants t ON t.id = c.tenant_id "
            "WHERE c.token_hash = %s",
            [token_hash],
        )
        if not rows:
            return None
        row = rows[0]
        if row["revoked_at"] is not None:
            return None
        if row["suspended"]:
            return None
        if row["kind"] != "query":
            # Anything other than the supported kind → treat as forgery.
            return None
        try:
            constraints = (
                _json.loads(row["constraints"]) if row["constraints"] else {}
            )
        except (TypeError, ValueError):
            constraints = {}
        tenant_id = row["tenant_id"]
        hive = self.for_tenant(tenant_id)
        if hive is None:
            return None
        # Capability tokens cannot thaw the seal: the owner-bound KEK
        # is keyed by hmk_, not hmq_. Reads of encrypted data while the
        # cache is cold will surface TenantSealed → 503.
        sealed = not self.sealer.is_unsealed(tenant_id)
        return Caller(
            tenant_id=tenant_id,
            role="query",
            constraints=constraints,
            hive=hive,
            token_id=_token_id(token_hash),
            sealed=sealed,
        )

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
        hm = Hivemind(
            self.settings, tenant_db=db_name, tenant_id=tenant_id,
            sealer=self.sealer,
        )
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

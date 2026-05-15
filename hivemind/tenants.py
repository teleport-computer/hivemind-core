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
from .tenant_billing import BillingRegistryMixin
from .tenant_credit_codes import CreditCodeRegistryMixin
from .tenant_keys import (
    API_KEY_PREFIX as _API_KEY_PREFIX,
    CREDIT_CODE_ID_PREFIX as _CREDIT_CODE_ID_PREFIX,
    CREDIT_CODE_PREFIX as _CREDIT_CODE_PREFIX,
    QUERY_TOKEN_PREFIX as _QUERY_TOKEN_PREFIX,
    SHARE_TOKEN_PREFIX as _SHARE_TOKEN_PREFIX,
    TENANT_ID_PREFIX as _TENANT_ID_PREFIX,
    charge_for_tokens as _charge_for_tokens,
    hash_api_key as _hash_api_key,
    new_api_key as _new_api_key,
    new_capability_token as _new_capability_token,
    new_credit_code as _new_credit_code,
    new_credit_code_id as _new_credit_code_id,
    new_tenant_id as _new_tenant_id,
    token_id as _token_id,
    usd_per_mtok_to_micro as _usd_per_mtok_to_micro,
    usd_to_micro_usd as _usd_to_micro_usd,
    usd_to_micro_usd_nonnegative as _usd_to_micro_usd_nonnegative,
)
from .seal import TenantSealed
from .tenant_seal import (
    ensure_unsealed,
    unwrap_dek_for_bearer,
    wrap_dek_for_bearer,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Caller",
    "DuplicateTenantNameError",
    "Role",
    "TenantRegistry",
    "_API_KEY_PREFIX",
    "_CREDIT_CODE_ID_PREFIX",
    "_CREDIT_CODE_PREFIX",
    "_QUERY_TOKEN_PREFIX",
    "_SHARE_TOKEN_PREFIX",
    "_TENANT_ID_PREFIX",
    "_charge_for_tokens",
    "_hash_api_key",
    "_new_api_key",
    "_new_capability_token",
    "_new_credit_code",
    "_new_credit_code_id",
    "_new_tenant_id",
    "_token_id",
    "_usd_per_mtok_to_micro",
    "_usd_to_micro_usd",
    "_usd_to_micro_usd_nonnegative",
]


# Initial pricing snapshots. Operators can override these in the control DB.
# Values are micro-USD per million tokens, derived from provider public model
# lists. Unknown providers/models are still metered as token usage but are not
# charged until a price row is configured.
_DEFAULT_MODEL_PRICES: tuple[tuple[str, str, int, int, str], ...] = (
    ("tinfoil", "z-ai/glm-5", 1_000_000, 3_200_000, "z-ai-pricing"),
    ("openrouter", "z-ai/glm-5", 600_000, 2_080_000, "openrouter"),
    ("openrouter", "moonshotai/kimi-k2.6", 750_000, 3_500_000, "openrouter"),
    ("openrouter", "moonshotai/kimi-k2.5", 440_000, 2_000_000, "openrouter"),
    ("openrouter", "moonshotai/kimi-k2-0905", 400_000, 2_000_000, "openrouter"),
    ("openrouter", "moonshotai/kimi-k2-thinking", 600_000, 2_500_000, "openrouter"),
    ("openrouter", "moonshotai/kimi-k2", 570_000, 2_300_000, "openrouter"),
    ("openrouter", "anthropic/claude-haiku-4.5", 1_000_000, 5_000_000, "openrouter"),
    ("openrouter", "anthropic/claude-sonnet-4.5", 3_000_000, 15_000_000, "openrouter"),
    ("openrouter", "openai/gpt-5-mini", 250_000, 2_000_000, "openrouter"),
    ("openrouter", "google/gemini-2.5-flash-lite", 100_000, 400_000, "openrouter"),
    ("openrouter", "google/gemini-2.5-flash", 300_000, 2_500_000, "openrouter"),
)


Role = Literal["owner", "query", "share"]


class DuplicateTenantNameError(ValueError):
    """Raised when tenant creation would reuse an existing display name."""

    def __init__(self, name: str, existing: list[dict]):
        self.name = name
        self.existing = existing
        ids = ", ".join(str(row.get("id")) for row in existing)
        suffix = f" ({ids})" if ids else ""
        super().__init__(
            f"tenant name '{name}' already exists{suffix}; "
            "pass allow_duplicate_name=true only if you really want another "
            "tenant with the same name"
        )


@dataclass(frozen=True)
class Caller:
    """Resolved bearer-token identity.

    Carries the per-tenant Hivemind plus the role / constraints that
    server endpoints use for access decisions. ``constraints`` schema
    depends on role:
      - owner: ``{}``
      - query: room invite constraints. Every run is forced through the
        signed room manifest; the tenant DB room row remains the source
        of truth. Room tokens also carry a debug/audit snapshot including
        ``allowed_tables`` when minted from a room manifest.
      - share: stable per-room share-link. Constraints carry only
        ``{"room_id": "<id>"}``. The asker's own ``hmk_`` (passed via
        ``X-Hivemind-Api-Key``) provides identity and pays for the run.
        ``token_id`` is the 12-hex prefix of the share-token hash.
    """

    tenant_id: str
    role: Role
    constraints: dict
    hive: Hivemind
    token_id: str = ""  # hex digest prefix; "" for owner tokens (no row)
    sealed: bool = False  # capability-token landed on a cold-cache tenant


def _is_missing_database_error(exc: Exception, db_name: str) -> bool:
    """Return true only for a missing database, not any missing object.

    The HTTP SQL proxy returns database errors as plain text. A previous
    broad check for ``"does not exist"`` also matched schema failures like
    ``column "room_id" does not exist`` and incorrectly tried to create an
    already-existing control DB.
    """
    msg = str(exc).lower()
    db = (db_name or "").strip().lower()
    if "3d000" in msg:
        return True
    if not db or "database" not in msg or "does not exist" not in msg:
        return False
    return (
        f'database "{db}" does not exist' in msg
        or f"database '{db}' does not exist" in msg
        or f"database {db} does not exist" in msg
    )


class TenantRegistry(CreditCodeRegistryMixin, BillingRegistryMixin):
    """Bearer-token → Hivemind resolver with LRU cache."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._cache: "OrderedDict[str, Hivemind]" = OrderedDict()
        self._cache_inflight: dict[str, threading.Event] = {}
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
            if not _is_missing_database_error(
                e, self.settings.control_database
            ):
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
        # Migrate BYTEA → TEXT for DBs initialized with the older
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
        # ``'query'`` is accepted by mint.
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
        self._control_db.execute_commit(
            "CREATE INDEX IF NOT EXISTS _capability_tokens_tenant_idx "
            "ON _capability_tokens (tenant_id)"
        )
        for column in (
            "seal_salt TEXT",
            "seal_wrapped_dek TEXT",
            "seal_kdf_params TEXT",
        ):
            try:
                self._control_db.execute_commit(
                    f"ALTER TABLE _capability_tokens "
                    f"ADD COLUMN IF NOT EXISTS {column}"
                )
            except Exception:
                pass
        # Stable per-room share links — Google-Docs-style "anyone with the
        # link who has any tenant key can ask in this room." One row per
        # (tenant_id, room_id); rotation replaces share_token in place,
        # disable hard-deletes the row. Plaintext share_token is kept
        # (deviating from API-key threat model) so the owner can re-fetch
        # the link any time without losing it. Threat model note: control
        # DB lives inside the CVM; an attacker with control-DB read can
        # already read every capability_token's seal_wrapped_dek, so
        # storing share-token plaintext here does not weaken the existing
        # posture meaningfully. Owner can rotate to invalidate.
        self._control_db.execute_commit(
            """
            CREATE TABLE IF NOT EXISTS _room_share_links (
                tenant_id        TEXT NOT NULL REFERENCES _tenants(id)
                                     ON DELETE CASCADE,
                room_id          TEXT NOT NULL,
                share_token      TEXT NOT NULL UNIQUE,
                prefix           TEXT NOT NULL,
                created_at       DOUBLE PRECISION NOT NULL,
                rotated_at       DOUBLE PRECISION,
                seal_salt        TEXT,
                seal_wrapped_dek TEXT,
                seal_kdf_params  TEXT,
                PRIMARY KEY (tenant_id, room_id)
            )
            """
        )
        self._control_db.execute_commit(
            "CREATE INDEX IF NOT EXISTS _room_share_links_token_idx "
            "ON _room_share_links (share_token)"
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
        # Billing lives in the control DB because charges belong to tenant
        # identities, not to a data owner's private tenant database. Positive
        # ledger amounts are credits/releases; negative amounts are holds or
        # settled usage charges.
        self._control_db.execute_commit(
            """
            CREATE TABLE IF NOT EXISTS _billing_ledger (
                entry_id    TEXT PRIMARY KEY,
                tenant_id   TEXT NOT NULL REFERENCES _tenants(id)
                            ON DELETE CASCADE,
                created_at  DOUBLE PRECISION NOT NULL,
                kind        TEXT NOT NULL,
                run_id      TEXT,
                amount_micro_usd BIGINT NOT NULL,
                metadata    TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self._control_db.execute_commit(
            "CREATE INDEX IF NOT EXISTS _billing_ledger_tenant_idx "
            "ON _billing_ledger (tenant_id, created_at DESC)"
        )
        # Admin-minted credit codes. Plaintext codes are returned once
        # during creation; the control DB stores only a hash. A code can be
        # redeemed by an existing tenant until max_redemptions is reached,
        # it expires, or the admin revokes it.
        self._control_db.execute_commit(
            """
            CREATE TABLE IF NOT EXISTS _credit_codes (
                code_id        TEXT PRIMARY KEY,
                code_hash        TEXT NOT NULL UNIQUE,
                label            TEXT NOT NULL DEFAULT '',
                credit_micro_usd BIGINT NOT NULL DEFAULT 0,
                max_redemptions  INTEGER NOT NULL DEFAULT 1,
                redeemed_count   INTEGER NOT NULL DEFAULT 0,
                created_at       DOUBLE PRECISION NOT NULL,
                expires_at       DOUBLE PRECISION,
                revoked_at       DOUBLE PRECISION
            )
            """
        )
        self._control_db.execute_commit(
            "CREATE INDEX IF NOT EXISTS _credit_codes_created_idx "
            "ON _credit_codes (created_at DESC)"
        )
        self._control_db.execute_commit(
            """
            CREATE TABLE IF NOT EXISTS _credit_code_redemptions (
                redemption_id TEXT PRIMARY KEY,
                code_id     TEXT NOT NULL REFERENCES _credit_codes(code_id)
                              ON DELETE CASCADE,
                tenant_id     TEXT NOT NULL REFERENCES _tenants(id)
                              ON DELETE CASCADE,
                redeemed_at   DOUBLE PRECISION NOT NULL,
                UNIQUE (code_id, tenant_id)
            )
            """
        )
        self._control_db.execute_commit(
            "CREATE INDEX IF NOT EXISTS _credit_code_redemptions_tenant_idx "
            "ON _credit_code_redemptions (tenant_id, redeemed_at DESC)"
        )
        self._control_db.execute_commit(
            """
            CREATE TABLE IF NOT EXISTS _billing_model_prices (
                provider TEXT NOT NULL,
                model    TEXT NOT NULL,
                prompt_microusd_per_mtok BIGINT NOT NULL,
                completion_microusd_per_mtok BIGINT NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (provider, model)
            )
            """
        )
        now = time.time()
        for provider, model, prompt_price, completion_price, source in (
            _DEFAULT_MODEL_PRICES
        ):
            self._control_db.execute_commit(
                "INSERT INTO _billing_model_prices "
                "(provider, model, prompt_microusd_per_mtok, "
                "completion_microusd_per_mtok, updated_at, source) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (provider, model) DO NOTHING",
                [provider, model, prompt_price, completion_price, now, source],
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
        # ``can_upload_query_agent`` opts a room invite into
        # POST /v1/rooms/{room_id}/query-agents.
        raw_upload = constraints.get("can_upload_query_agent", False)
        if raw_upload not in (True, False):
            raise ValueError(
                "can_upload_query_agent must be a boolean"
            )
        normalized = {
            "scope_agent_id": sid,
            "can_upload_query_agent": bool(raw_upload),
        }
        # Room invites are still query tokens. Preserve the room
        # enforcement snapshot for cheap whoami/debug output while the
        # server re-loads the signed room manifest before every run.
        room_id = (constraints.get("room_id") or "").strip()
        if room_id:
            normalized["room_id"] = room_id
        for key in (
            "room_manifest_hash",
            "query_mode",
            "fixed_query_agent_id",
            "fixed_mediator_agent_id",
            "query_inspection_mode",
            "output_visibility",
            "policy",
        ):
            value = constraints.get(key)
            if isinstance(value, str) and value.strip():
                normalized[key] = value.strip()
        for key in ("allowed_llm_providers", "allowed_tables"):
            value = constraints.get(key)
            if isinstance(value, list):
                normalized[key] = [str(v).strip() for v in value if str(v).strip()]
            elif key in constraints:
                raise ValueError(f"{key} must be a list")
        if "allow_artifacts" in constraints:
            normalized["allow_artifacts"] = bool(constraints.get("allow_artifacts"))
        constraints = normalized

        rows = self._control_db.execute(
            "SELECT id FROM _tenants WHERE id = %s", [tenant_id]
        )
        if not rows:
            raise KeyError(f"tenant '{tenant_id}' not found")

        token = _new_capability_token(_QUERY_TOKEN_PREFIX)
        token_hash = _hash_api_key(token)
        seal_salt, seal_wrapped_dek, seal_kdf_params = (
            self._capability_dek_wrap(tenant_id, token)
        )
        self._control_db.execute_commit(
            "INSERT INTO _capability_tokens "
            "(token_hash, tenant_id, kind, label, constraints, created_at, "
            "seal_salt, seal_wrapped_dek, seal_kdf_params) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                token_hash,
                tenant_id,
                kind,
                label.strip(),
                _json.dumps(constraints),
                time.time(),
                seal_salt,
                seal_wrapped_dek,
                seal_kdf_params,
            ],
        )
        return {
            "token": token,
            "token_id": _token_id(token_hash),
            "kind": kind,
            "label": label.strip(),
            "constraints": constraints,
        }

    def _capability_dek_wrap(
        self,
        tenant_id: str,
        token: str,
    ) -> tuple[str | None, str | None, str | None]:
        try:
            dek = self.sealer.get_dek(tenant_id)
        except TenantSealed:
            return None, None, None
        try:
            return wrap_dek_for_bearer(dek, token)
        except Exception as e:
            logger.warning("tenant %s capability DEK wrap failed: %s", tenant_id, e)
            return None, None, None

    def _thaw_capability_dek(
        self,
        *,
        tenant_id: str,
        token: str,
        row: dict,
    ) -> bool:
        salt = row.get("seal_salt")
        wrapped = row.get("seal_wrapped_dek")
        if not salt or not wrapped:
            return False
        try:
            dek = unwrap_dek_for_bearer(
                salt,
                wrapped,
                row.get("seal_kdf_params"),
                token,
            )
        except Exception as e:
            logger.warning(
                "tenant %s capability seal unwrap failed: %s", tenant_id, e,
            )
            return False
        self.sealer.cache(tenant_id, dek)
        return True

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

    # ── Room share-link operations ──────────────────────────────────

    def get_room_share_link(self, tenant_id: str, room_id: str) -> dict | None:
        """Return the active share link for a room, including plaintext.

        Owner-readable any time (the room owner already has full access
        to their tenant's data; surfacing the share-token plaintext here
        is the same UX as a Google-Docs share URL). Returns ``None`` if
        no share link is configured for this room.
        """
        rows = self._control_db.execute(
            "SELECT share_token, prefix, created_at, rotated_at "
            "FROM _room_share_links WHERE tenant_id = %s AND room_id = %s",
            [tenant_id, room_id],
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "tenant_id": tenant_id,
            "room_id": room_id,
            "share_token": r["share_token"],
            "prefix": r["prefix"],
            "created_at": r["created_at"],
            "rotated_at": r["rotated_at"],
        }

    def enable_room_share_link(
        self,
        tenant_id: str,
        room_id: str,
        owner_token: str,
    ) -> dict:
        """Create a stable share link for ``(tenant_id, room_id)``.

        Idempotent: if a row already exists, returns the existing one
        (same plaintext) so the website's "Generate" button is safe to
        click twice. Use :meth:`rotate_room_share_link` to invalidate
        and replace. ``owner_token`` is the bearer the owner used to
        reach this endpoint; it's needed only to wrap the tenant DEK
        against the share token so share-link askers can thaw the seal
        across redeploys, mirroring the capability-token flow.
        """
        rows = self._control_db.execute(
            "SELECT id FROM _tenants WHERE id = %s", [tenant_id]
        )
        if not rows:
            raise KeyError(f"tenant '{tenant_id}' not found")

        existing = self.get_room_share_link(tenant_id, room_id)
        if existing is not None:
            return existing

        token = _new_capability_token(_SHARE_TOKEN_PREFIX)
        prefix = _token_id(_hash_api_key(token))[:8]
        seal_salt, seal_wrapped_dek, seal_kdf_params = (
            self._capability_dek_wrap(tenant_id, token)
        )
        self._control_db.execute_commit(
            "INSERT INTO _room_share_links "
            "(tenant_id, room_id, share_token, prefix, created_at, "
            "seal_salt, seal_wrapped_dek, seal_kdf_params) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [
                tenant_id,
                room_id,
                token,
                prefix,
                time.time(),
                seal_salt,
                seal_wrapped_dek,
                seal_kdf_params,
            ],
        )
        # owner_token is intentionally unused for now — kept in the
        # signature so future tightening (e.g. an explicit owner-key
        # binding on the share row) doesn't churn the registry call site.
        del owner_token
        return self.get_room_share_link(tenant_id, room_id) or {
            "tenant_id": tenant_id,
            "room_id": room_id,
            "share_token": token,
            "prefix": prefix,
            "created_at": time.time(),
            "rotated_at": None,
        }

    def rotate_room_share_link(
        self,
        tenant_id: str,
        room_id: str,
        owner_token: str,
    ) -> dict:
        """Replace the share token in place. Old plaintext is invalidated.

        Raises ``KeyError`` if no share link exists for this room — the
        caller should mint via :meth:`enable_room_share_link` instead.
        """
        existing = self.get_room_share_link(tenant_id, room_id)
        if existing is None:
            raise KeyError(
                f"no share link for room '{room_id}' — enable first"
            )
        token = _new_capability_token(_SHARE_TOKEN_PREFIX)
        prefix = _token_id(_hash_api_key(token))[:8]
        seal_salt, seal_wrapped_dek, seal_kdf_params = (
            self._capability_dek_wrap(tenant_id, token)
        )
        self._control_db.execute_commit(
            "UPDATE _room_share_links SET "
            "share_token = %s, prefix = %s, rotated_at = %s, "
            "seal_salt = %s, seal_wrapped_dek = %s, seal_kdf_params = %s "
            "WHERE tenant_id = %s AND room_id = %s",
            [
                token,
                prefix,
                time.time(),
                seal_salt,
                seal_wrapped_dek,
                seal_kdf_params,
                tenant_id,
                room_id,
            ],
        )
        del owner_token
        return self.get_room_share_link(tenant_id, room_id) or {
            "tenant_id": tenant_id,
            "room_id": room_id,
            "share_token": token,
            "prefix": prefix,
            "created_at": existing.get("created_at"),
            "rotated_at": time.time(),
        }

    def disable_room_share_link(self, tenant_id: str, room_id: str) -> bool:
        """Hard-delete the share link row. Existing tokens stop resolving."""
        rowcount = self._control_db.execute_commit(
            "DELETE FROM _room_share_links "
            "WHERE tenant_id = %s AND room_id = %s",
            [tenant_id, room_id],
        )
        return bool(rowcount)

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

    def revoke_capabilities_for_room(
        self, tenant_id: str, room_id: str,
    ) -> int:
        """Revoke every live invite (``hmq_…`` capability token) whose
        constraints bind it to this room.

        Used by the room-revoke cascade so that deleting a room also
        retires every invite that pointed at it — otherwise revoked
        rooms leave orphaned invites in /app/settings, which is
        confusing to owners and means revocation isn't really
        revocation. The share link (``hms_…``) for the room is handled
        separately by :meth:`disable_room_share_link`.

        Returns the number of tokens revoked. The filter is done in
        Python because ``constraints`` is stored as TEXT (JSON-encoded)
        and we don't want to rely on a Postgres-specific cast.
        """
        room_id = (room_id or "").strip()
        if not room_id:
            return 0
        rows = self._control_db.execute(
            "SELECT token_hash, constraints FROM _capability_tokens "
            "WHERE tenant_id = %s AND revoked_at IS NULL",
            [tenant_id],
        )
        targets: list[str] = []
        for row in rows:
            token_hash = row[0] if not hasattr(row, "keys") else row["token_hash"]
            constraints_text = (
                row[1] if not hasattr(row, "keys") else row["constraints"]
            )
            try:
                c = _json.loads(constraints_text or "{}")
            except Exception:
                continue
            if isinstance(c, dict) and c.get("room_id") == room_id:
                targets.append(token_hash)
        if not targets:
            return 0
        now = time.time()
        for h in targets:
            self._control_db.execute_commit(
                "UPDATE _capability_tokens SET revoked_at = %s "
                "WHERE token_hash = %s AND revoked_at IS NULL",
                [now, h],
            )
        return len(targets)

    # ── Admin operations ────────────────────────────────────────────

    def _find_tenants_by_name(self, name: str) -> list[dict]:
        rows = self._control_db.execute(
            "SELECT id, name, db_name, created_at, suspended "
            "FROM _tenants WHERE lower(name) = lower(%s) "
            "ORDER BY created_at DESC",
            [name.strip()],
        )
        return [dict(r) for r in rows]

    def _reject_duplicate_name_unless_allowed(
        self, name: str, allow_duplicate_name: bool,
    ) -> None:
        if allow_duplicate_name:
            return
        existing = self._find_tenants_by_name(name)
        if existing:
            raise DuplicateTenantNameError(name, existing)

    def provision(
        self, name: str, *, allow_duplicate_name: bool = False,
    ) -> dict:
        """Create tenant: new DB, new API key. Returns key once."""
        if not name or not name.strip():
            raise ValueError("tenant name required")
        clean_name = name.strip()
        self._reject_duplicate_name_unless_allowed(
            clean_name, allow_duplicate_name,
        )
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
                [tenant_id, clean_name, api_key_hash, db_name, time.time()],
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
            "name": clean_name,
        }

    def register_existing(
        self,
        name: str,
        db_name: str,
        api_key: str | None = None,
        tenant_id: str | None = None,
        allow_duplicate_name: bool = False,
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
        clean_name = name.strip()
        self._reject_duplicate_name_unless_allowed(
            clean_name, allow_duplicate_name,
        )
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
            [tenant_id, clean_name, api_key_hash, db_name, time.time()],
        )

        return {
            "tenant_id": tenant_id,
            "api_key": api_key,
            "db_name": db_name,
            "name": clean_name,
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

    def admin_reset_tenant_key(
        self,
        tenant_id: str,
        *,
        clear_seal: bool = False,
        revoke_capabilities: bool = False,
    ) -> dict:
        """Admin-only clean-start key reset for a tenant.

        Resetting the hash alone is not enough for sealed tenants: the existing
        tenant DEK wrap is derived from the old owner key, so a fresh key cannot
        thaw it. ``clear_seal`` intentionally drops that wrap and evicts the
        in-process DEK cache so the next owner request initializes a fresh seal.
        Existing encrypted agent files become unreadable; tenant application
        tables are untouched.
        """
        rows = self._control_db.execute(
            "SELECT id, name, db_name FROM _tenants WHERE id = %s",
            [tenant_id],
        )
        if not rows:
            raise KeyError(f"tenant '{tenant_id}' not found")
        tenant = dict(rows[0])

        seal_rows_deleted = 0
        if clear_seal:
            hm = self.for_tenant(tenant_id)
            if hm is None:
                raise KeyError(f"tenant '{tenant_id}' not found")
            try:
                seal_rows_deleted = int(
                    hm.db.execute_commit("DELETE FROM _hivemind_tenant_kek")
                    or 0
                )
            except Exception as e:
                raise RuntimeError(f"failed to clear tenant seal: {e}") from e
            self.sealer.evict(tenant_id)

        capabilities_revoked = 0
        if revoke_capabilities:
            capabilities_revoked = int(
                self._control_db.execute_commit(
                    "UPDATE _capability_tokens SET revoked_at = %s "
                    "WHERE tenant_id = %s AND revoked_at IS NULL",
                    [time.time(), tenant_id],
                )
                or 0
            )

        new_key = _new_api_key()
        new_hash = _hash_api_key(new_key)
        rowcount = self._control_db.execute_commit(
            "UPDATE _tenants SET api_key_hash = %s WHERE id = %s",
            [new_hash, tenant_id],
        )
        if rowcount != 1:
            raise RuntimeError(f"reset-key rowcount={rowcount} (expected 1)")
        return {
            "tenant_id": tenant_id,
            "name": tenant.get("name") or "",
            "db_name": tenant.get("db_name") or "",
            "api_key": new_key,
            "clear_seal": bool(clear_seal),
            "seal_rows_deleted": seal_rows_deleted,
            "revoke_capabilities": bool(revoke_capabilities),
            "capabilities_revoked": capabilities_revoked,
        }

    def admin_rename_tenant(
        self,
        tenant_id: str,
        new_name: str,
        *,
        allow_duplicate_name: bool = False,
    ) -> dict:
        """Update the human-readable name on the _tenants row.

        Tenant id, db_name, api_key, room ownership and capability tokens are
        unchanged. Only the display name is rewritten.
        """
        clean_name = (new_name or "").strip()
        if not clean_name:
            raise ValueError("tenant name required")
        rows = self._control_db.execute(
            "SELECT id, name, db_name, created_at, suspended "
            "FROM _tenants WHERE id = %s",
            [tenant_id],
        )
        if not rows:
            raise KeyError(f"tenant '{tenant_id}' not found")
        existing = dict(rows[0])
        if existing["name"] == clean_name:
            return existing
        if not allow_duplicate_name:
            collisions = [
                r for r in self._find_tenants_by_name(clean_name)
                if r["id"] != tenant_id
            ]
            if collisions:
                raise DuplicateTenantNameError(clean_name, collisions)
        rowcount = self._control_db.execute_commit(
            "UPDATE _tenants SET name = %s WHERE id = %s",
            [clean_name, tenant_id],
        )
        if rowcount != 1:
            raise RuntimeError(f"rename rowcount={rowcount} (expected 1)")
        return {
            "id": tenant_id,
            "name": clean_name,
            "db_name": existing.get("db_name") or "",
            "created_at": existing.get("created_at"),
            "suspended": existing.get("suspended"),
        }

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

    def _insert_cached_hive_locked(
        self, tenant_id: str, hm: Hivemind
    ) -> Hivemind:
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

    def _get_or_create_hive(self, tenant_id: str, db_name: str) -> Hivemind:
        """Single-flight per-tenant Hivemind construction.

        Tenant bootstrap can build Docker contexts or run migrations. If the
        first HTTP caller times out, the worker thread continues; concurrent
        requests should wait for that construction instead of starting another
        identical warmup.
        """
        while True:
            with self._lock:
                hm = self._cache.get(tenant_id)
                if hm is not None:
                    self._cache.move_to_end(tenant_id)
                    return hm
                event = self._cache_inflight.get(tenant_id)
                if event is None:
                    event = threading.Event()
                    self._cache_inflight[tenant_id] = event
                    break
            event.wait()

        try:
            hm = Hivemind(
                self.settings,
                tenant_db=db_name,
                tenant_id=tenant_id,
                sealer=self.sealer,
                billing_meter=self,
            )
        except Exception:
            with self._lock:
                self._cache_inflight.pop(tenant_id, None)
                event.set()
            raise

        with self._lock:
            try:
                return self._insert_cached_hive_locked(tenant_id, hm)
            finally:
                self._cache_inflight.pop(tenant_id, None)
                event.set()

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
        return self._get_or_create_hive(tenant_id, db_name)

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
        first contact). An ``hmq_`` bearer thaws only when that capability
        row carries a DEK wrap minted by the owner.
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
        if token.startswith(_SHARE_TOKEN_PREFIX):
            return self._resolve_share_token(token)
        if not token.startswith(_QUERY_TOKEN_PREFIX):
            return None

        token_hash = _hash_api_key(token)
        rows = self._control_db.execute(
            "SELECT c.tenant_id, c.kind, c.constraints, c.revoked_at, "
            "       c.seal_salt, c.seal_wrapped_dek, c.seal_kdf_params, "
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
        # Capability tokens minted while the owner tenant is warm carry
        # their own encrypted tenant-DEK wrap. That lets room invites keep
        # working after a deploy without requiring a separate owner request.
        sealed = not self.sealer.is_unsealed(tenant_id)
        if sealed:
            sealed = not self._thaw_capability_dek(
                tenant_id=tenant_id,
                token=token,
                row=row,
            )
        return Caller(
            tenant_id=tenant_id,
            role="query",
            constraints=constraints,
            hive=hive,
            token_id=_token_id(token_hash),
            sealed=sealed,
        )

    def _resolve_share_token(self, token: str) -> Caller | None:
        """Resolve an ``hms_`` share-link bearer to a Caller(role='share').

        Lookup is by exact match on the plaintext share_token column
        (UNIQUE-indexed). The Caller's tenant_id and hive belong to the
        room owner (which is whose tenant DB stores the room). The asker's
        own identity for billing comes via ``X-Hivemind-Api-Key`` later
        in ``_payer_for_request``.
        """
        rows = self._control_db.execute(
            "SELECT s.tenant_id, s.room_id, s.seal_salt, s.seal_wrapped_dek, "
            "       s.seal_kdf_params, t.suspended "
            "FROM _room_share_links s "
            "JOIN _tenants t ON t.id = s.tenant_id "
            "WHERE s.share_token = %s",
            [token],
        )
        if not rows:
            return None
        row = rows[0]
        if row["suspended"]:
            return None
        tenant_id = row["tenant_id"]
        hive = self.for_tenant(tenant_id)
        if hive is None:
            return None
        sealed = not self.sealer.is_unsealed(tenant_id)
        if sealed:
            sealed = not self._thaw_capability_dek(
                tenant_id=tenant_id,
                token=token,
                row=row,
            )
        token_hash = _hash_api_key(token)
        return Caller(
            tenant_id=tenant_id,
            role="share",
            constraints={"room_id": row["room_id"]},
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

        hm = self._get_or_create_hive(tenant_id, db_name)
        try:
            import asyncio
            asyncio.get_running_loop()
            hm.start_retention_sweeper()
        except RuntimeError:
            # No running loop (tests / CLI). Sweeper will start on first
            # await, or never — fine for short-lived clients.
            pass

        return tenant_id, hm

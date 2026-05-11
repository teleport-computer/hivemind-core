"""First-class signed room manifests and storage.

A room is the small, signed contract that binds the pieces a recipient
needs to reason about before entering:

* which scope agent protects the owner's data,
* whether the query agent is fixed or recipient-uploaded,
* which agent source is inspectable vs sealed,
* what egress is allowed,
* who may see final output and artifacts, and
* how live compose hashes are trusted.

The manifest is tenant-signed, then stored as canonical JSON. Query
capability tokens carry only a ``room_id`` plus a snapshot of enforcement
fields; the room row remains the source of truth for live checks.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives import serialization
from pydantic import BaseModel, ConfigDict, Field, model_validator


RoomQueryMode = Literal["fixed", "uploadable"]
RoomVisibility = Literal["inspectable", "sealed"]
RoomOutputVisibility = Literal["querier_only", "owner_and_querier"]
RoomTrustMode = Literal[
    "pinned",
    "owner_approved",
    "operator_updates",
]

_KNOWN_LLM_PROVIDERS = {"openrouter", "tinfoil"}
_DEFAULT_ROOM_LLM_PROVIDER = "openrouter"


def _canonical_json(value: dict) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sha256_json(value: dict) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def normalize_llm_providers(providers: list[str] | None) -> list[str]:
    """Normalize a provider allowlist.

    Empty list means "no external LLM egress". ``None`` is treated the
    same as empty here because room manifests must be explicit.
    """
    out: list[str] = []
    for raw in providers or []:
        key = (raw or "").strip().lower()
        if not key:
            continue
        if key not in _KNOWN_LLM_PROVIDERS:
            raise ValueError(
                f"unknown LLM provider {raw!r}; valid: {sorted(_KNOWN_LLM_PROVIDERS)}"
            )
        if key not in out:
            out.append(key)
    return out


def visibility_from_inspection_mode(mode: str | None) -> RoomVisibility:
    return "sealed" if (mode or "full").strip().lower() == "sealed" else "inspectable"


def inspection_mode_from_visibility(value: str | None) -> str:
    return "sealed" if (value or "inspectable") == "sealed" else "full"


class RoomEgress(BaseModel):
    """Room egress contract.

    ``llm_providers=[]`` means the bridge still exists for tools, but all
    LLM endpoints reject. ``allow_artifacts=False`` removes the artifact
    upload endpoint from query-agent bridge sessions.
    """

    llm_providers: list[str] = Field(default_factory=lambda: [_DEFAULT_ROOM_LLM_PROVIDER])
    allow_artifacts: bool = True

    @model_validator(mode="after")
    def _normalize(self):
        self.llm_providers = normalize_llm_providers(self.llm_providers)
        return self


class RoomTrust(BaseModel):
    mode: RoomTrustMode = "operator_updates"
    allowed_composes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize(self):
        self.allowed_composes = [
            c.strip().lower() for c in self.allowed_composes if c and c.strip()
        ]
        if self.mode == "pinned" and not self.allowed_composes:
            raise ValueError("trust.mode='pinned' requires allowed_composes")
        return self


class RoomCreateRequest(BaseModel):
    """Compact owner-facing API for creating a room.

    The API route resolves an omitted query mode to the service default
    query agent when configured. Callers that want recipient-uploaded
    query agents should set ``query_mode="uploadable"`` explicitly.
    """

    name: str = ""
    rules: str = ""
    policy: str | None = None
    scope_agent_id: str = Field(..., min_length=1)
    scope_visibility: RoomVisibility | None = None
    query_mode: RoomQueryMode | None = None
    query_agent_id: str | None = None
    query_visibility: RoomVisibility = "sealed"
    mediator_agent_id: str | None = None
    mediator_visibility: RoomVisibility | None = None
    output_visibility: RoomOutputVisibility = "querier_only"
    egress: RoomEgress = Field(default_factory=RoomEgress)
    trust: RoomTrust = Field(default_factory=RoomTrust)
    # Per-room data sources. List of tenant table names this room's scope/
    # query agents may reference. Names are validated against the tenant DB
    # at create time. An empty list is valid and means "no SQL data sources" —
    # only the room vault is reachable.
    allowed_tables: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_mode(self):
        if self.query_agent_id and self.query_mode is None:
            self.query_mode = "fixed"
        if self.query_mode == "fixed" and not (self.query_agent_id or "").strip():
            raise ValueError("query_agent_id is required when query_mode='fixed'")
        if self.query_agent_id is not None:
            self.query_agent_id = self.query_agent_id.strip() or None
        if self.mediator_agent_id is not None:
            self.mediator_agent_id = self.mediator_agent_id.strip() or None
        if self.policy is None:
            self.policy = self.rules or ""
        self.scope_agent_id = self.scope_agent_id.strip()
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in self.allowed_tables:
            t = (raw or "").strip()
            if not t:
                continue
            # Postgres identifier shape, conservative.
            if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$", t):
                raise ValueError(
                    f"allowed_tables entry '{raw}' is not a valid "
                    "Postgres identifier"
                )
            if t.startswith("_"):
                raise ValueError(
                    f"allowed_tables entry '{raw}' is reserved "
                    "(internal table prefix)"
                )
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(t)
        self.allowed_tables = cleaned
        return self


class RoomRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1)
    query_agent_id: str | None = None
    mediator_agent_id: str | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_llm_calls: int | None = Field(default=None, ge=1)
    timeout_seconds: int | None = Field(default=None, ge=1)
    model: str | None = None
    scope_model: str | None = None
    query_model: str | None = None
    mediator_model: str | None = None
    provider: str | None = None

    @model_validator(mode="after")
    def _validate_query(self):
        if not self.query.strip():
            raise ValueError("'query' is required")
        return self


class RoomTrustUpdateRequest(BaseModel):
    mode: RoomTrustMode | None = None
    allowed_composes: list[str] | None = None
    append_live: bool = False


class RoomVaultItemRequest(BaseModel):
    text: str = Field(..., min_length=1)
    metadata: dict = Field(default_factory=dict)


def build_room_manifest(
    *,
    room_id: str,
    tenant_id: str,
    created_at: float,
    req: RoomCreateRequest,
    scope_visibility: RoomVisibility,
    query_visibility: RoomVisibility,
    mediator_visibility: RoomVisibility | None,
    signer_pubkey_b64: str,
) -> dict:
    mediator = {"agent_id": req.mediator_agent_id or ""}
    if req.mediator_agent_id:
        mediator["visibility"] = mediator_visibility or "inspectable"
    return {
        "schema": "hivemind.room.v1",
        "room_id": room_id,
        "tenant_id": tenant_id,
        "name": req.name.strip(),
        "rules": req.rules or "",
        "rules_hash": _sha256_text(req.rules or ""),
        "policy": req.policy or "",
        "scope": {
            "agent_id": req.scope_agent_id,
            "visibility": scope_visibility,
        },
        "query": {
            "mode": req.query_mode,
            "agent_id": req.query_agent_id or "",
            "visibility": query_visibility,
        },
        "mediator": mediator,
        "output": {
            "visibility": req.output_visibility,
        },
        "egress": req.egress.model_dump(),
        "trust": req.trust.model_dump(),
        # Per-room SQL allowlist. Empty means "no SQL data sources". This must
        # always be signed into the manifest.
        "allowed_tables": req.allowed_tables,
        "created_at": created_at,
        "owner_pubkey_b64": signer_pubkey_b64,
    }


def sign_manifest(manifest: dict, priv: Ed25519PrivateKey) -> dict:
    body = _canonical_json(manifest).encode("utf-8")
    sig = priv.sign(body)
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "manifest": manifest,
        "manifest_hash": hashlib.sha256(body).hexdigest(),
        "signature_b64": base64.b64encode(sig).decode("ascii"),
        "signer_pubkey_b64": base64.b64encode(pub).decode("ascii"),
    }


def verify_room_envelope(
    envelope: dict,
    *,
    expected_pubkey_b64: str | None = None,
) -> tuple[bool, str]:
    """Verify a signed room envelope.

    ``expected_pubkey_b64`` comes from an `hmroom://` link. When present,
    it is the trust anchor: the envelope signer must match it. Without
    it, this still checks self-consistency (hash + signature) but does
    not tell the caller whether the signer is the one they intended to
    trust.
    """
    try:
        manifest = envelope.get("manifest")
        manifest_hash = envelope.get("manifest_hash") or ""
        sig_b64 = envelope.get("signature_b64") or ""
        signer_b64 = envelope.get("signer_pubkey_b64") or ""
        if not isinstance(manifest, dict) or not manifest_hash:
            return False, "room envelope missing manifest or manifest_hash"
        if not sig_b64 or not signer_b64:
            return False, "room envelope missing signature or signer_pubkey"
        if expected_pubkey_b64 and signer_b64 != expected_pubkey_b64:
            return False, "room signer does not match owner_pubkey in link"
        manifest_pub = manifest.get("owner_pubkey_b64") or ""
        if manifest_pub and manifest_pub != signer_b64:
            return False, "manifest owner_pubkey does not match signer_pubkey"

        body = _canonical_json(manifest).encode("utf-8")
        actual_hash = hashlib.sha256(body).hexdigest()
        if actual_hash != manifest_hash:
            return False, "manifest_hash does not match manifest body"

        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(signer_b64))
        pub.verify(base64.b64decode(sig_b64), body)
        return True, "ok"
    except Exception as e:
        return False, f"room signature did not verify: {e}"


def room_constraints(envelope: dict) -> dict:
    """Capability-token constraint snapshot for a room invite."""
    manifest = envelope["manifest"]
    raw_allowed_tables = manifest.get("allowed_tables")
    if raw_allowed_tables is None:
        raise ValueError("room manifest is missing signed allowed_tables")
    if not isinstance(raw_allowed_tables, list):
        raise ValueError("room manifest has invalid allowed_tables")
    query = manifest["query"]
    egress = manifest["egress"]
    return {
        "room_id": manifest["room_id"],
        "room_manifest_hash": envelope["manifest_hash"],
        "scope_agent_id": manifest["scope"]["agent_id"],
        "can_upload_query_agent": query["mode"] == "uploadable",
        "query_mode": query["mode"],
        "fixed_query_agent_id": query.get("agent_id") or "",
        "fixed_mediator_agent_id": (
            (manifest.get("mediator") or {}).get("agent_id") or ""
        ),
        "query_inspection_mode": inspection_mode_from_visibility(
            query.get("visibility")
        ),
        "output_visibility": manifest["output"]["visibility"],
        "allowed_llm_providers": list(egress.get("llm_providers") or []),
        "allow_artifacts": bool(egress.get("allow_artifacts")),
        "allowed_tables": [
            str(t).strip() for t in raw_allowed_tables if str(t).strip()
        ],
        "policy": manifest.get("policy") or "",
    }


class RoomStore:
    """CRUD for tenant-local ``_hivemind_rooms`` rows."""

    def __init__(self, db):
        self.db = db

    def create(self, envelope: dict) -> dict:
        manifest = envelope["manifest"]
        room_id = manifest["room_id"]
        query = manifest["query"]
        egress = manifest["egress"]
        envelope_json = _canonical_json(envelope)
        self.db.execute_commit(
            "INSERT INTO _hivemind_rooms "
            "(room_id, name, envelope, manifest_hash, scope_agent_id, "
            "fixed_query_agent_id, query_mode, output_visibility, "
            "allowed_llm_providers, allow_artifacts, room_policy, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                room_id,
                manifest.get("name") or "",
                envelope_json,
                envelope["manifest_hash"],
                manifest["scope"]["agent_id"],
                query.get("agent_id") or None,
                query["mode"],
                manifest["output"]["visibility"],
                json.dumps(list(egress.get("llm_providers") or [])),
                bool(egress.get("allow_artifacts")),
                manifest.get("policy") or "",
                float(manifest["created_at"]),
            ],
        )
        return self.get(room_id) or {
            "room_id": room_id,
            "envelope": envelope,
            "manifest_hash": envelope["manifest_hash"],
        }

    def update(self, envelope: dict) -> dict | None:
        manifest = envelope["manifest"]
        room_id = manifest["room_id"]
        query = manifest["query"]
        egress = manifest["egress"]
        envelope_json = _canonical_json(envelope)
        rowcount = self.db.execute_commit(
            "UPDATE _hivemind_rooms SET "
            "name = %s, envelope = %s, manifest_hash = %s, "
            "scope_agent_id = %s, fixed_query_agent_id = %s, "
            "query_mode = %s, output_visibility = %s, "
            "allowed_llm_providers = %s, allow_artifacts = %s, "
            "room_policy = %s "
            "WHERE room_id = %s",
            [
                manifest.get("name") or "",
                envelope_json,
                envelope["manifest_hash"],
                manifest["scope"]["agent_id"],
                query.get("agent_id") or None,
                query["mode"],
                manifest["output"]["visibility"],
                json.dumps(list(egress.get("llm_providers") or [])),
                bool(egress.get("allow_artifacts")),
                manifest.get("policy") or "",
                room_id,
            ],
        )
        if not rowcount:
            return None
        return self.get(room_id)

    def _row_to_room(self, row: dict) -> dict:
        envelope = json.loads(row["envelope"])
        return {
            "room_id": row["room_id"],
            "name": row.get("name") or "",
            "envelope": envelope,
            "manifest": envelope.get("manifest") or {},
            "manifest_hash": row["manifest_hash"],
            "scope_agent_id": row["scope_agent_id"],
            "fixed_query_agent_id": row.get("fixed_query_agent_id") or "",
            "fixed_mediator_agent_id": (
                (envelope.get("manifest") or {}).get("mediator") or {}
            ).get("agent_id") or "",
            "query_mode": row["query_mode"],
            "output_visibility": row["output_visibility"],
            "allowed_llm_providers": json.loads(
                row.get("allowed_llm_providers") or "[]"
            ),
            "allow_artifacts": bool(row.get("allow_artifacts")),
            "policy": row.get("room_policy") or "",
            "created_at": row["created_at"],
            "revoked_at": row.get("revoked_at"),
        }

    def get(self, room_id: str) -> dict | None:
        rows = self.db.execute(
            "SELECT room_id, name, envelope, manifest_hash, scope_agent_id, "
            "fixed_query_agent_id, query_mode, output_visibility, "
            "allowed_llm_providers, allow_artifacts, room_policy, "
            "created_at, revoked_at "
            "FROM _hivemind_rooms WHERE room_id = %s",
            [room_id],
        )
        return self._row_to_room(rows[0]) if rows else None

    def list(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT room_id, name, envelope, manifest_hash, scope_agent_id, "
            "fixed_query_agent_id, query_mode, output_visibility, "
            "allowed_llm_providers, allow_artifacts, room_policy, "
            "created_at, revoked_at "
            "FROM _hivemind_rooms ORDER BY created_at DESC LIMIT %s",
            [min(max(int(limit), 1), 100)],
        )
        return [self._row_to_room(r) for r in rows]

    def revoke(self, room_id: str) -> bool:
        return bool(
            self.db.execute_commit(
                "UPDATE _hivemind_rooms SET revoked_at = %s "
                "WHERE room_id = %s AND revoked_at IS NULL",
                [time.time(), room_id],
            )
        )


__all__ = [
    "RoomCreateRequest",
    "RoomEgress",
    "RoomRunRequest",
    "RoomStore",
    "RoomTrust",
    "RoomTrustUpdateRequest",
    "RoomVaultItemRequest",
    "build_room_manifest",
    "inspection_mode_from_visibility",
    "normalize_llm_providers",
    "room_constraints",
    "sign_manifest",
    "verify_room_envelope",
    "visibility_from_inspection_mode",
]

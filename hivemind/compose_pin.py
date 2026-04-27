"""ComposePin: tenant-signed envelope authorizing one or more
``compose_hash`` values for a scope agent.

Why this exists:
- The owner (A) wants to redeploy the CVM (e.g. patch the runtime image)
  without invalidating every ``hmq_`` URI they've already minted to B.
- Baking a single ``compose_hash`` into a URI breaks on every redeploy.
- Baking nothing means B can't tell A's CVM apart from a
  pretend-to-be-A CVM that the operator stood up.
- ComposePin threads the needle: the URI references a *signed list of
  composes* that A has approved, plus the attested files digest. B
  fetches the pin from any server, checks the signature with A's
  pubkey, then asserts the live ``compose_hash`` is in the list and the
  attested digest matches. As long as A has blessed the new compose,
  the URI keeps working.

Envelope schema (signed):
    schema_version: int = 1
    tenant_id: str                   # t_<hex>
    allowed_composes: list[str]      # 64-hex compose_hash entries
    scope_agent_id: str              # the agent the URI is bound to
    attested_files_digest: str       # 64-hex over attestable files
    issued_at: int                   # unix seconds
    exp: int                         # unix seconds (0 = no expiry)
    signer_pubkey: str               # b64 of 32-byte Ed25519 pubkey

Signature: Ed25519 over the canonical-JSON encoding of the above
fields (signer_pubkey included so a swap-the-pubkey attack fails).

Verification rules (recipient side):
1. ``signature`` checks against ``signer_pubkey`` over the canonical
   JSON of the envelope.
2. ``signer_pubkey`` matches the pubkey the recipient derives from the
   tenant's published key fingerprint (or the one they trust on file).
3. ``exp == 0`` or ``exp >= now``.
4. The live ``compose_hash`` from /v1/attestation is in
   ``allowed_composes``.
5. The live attested files digest matches ``attested_files_digest``.

Note: We deliberately keep this format JSON-canonical (sorted keys, no
whitespace) rather than CBOR / protobuf — it's easy to inspect, easy
to re-implement in a non-Python verifier (e.g. a browser extension),
and avoids a new dep.
"""

from __future__ import annotations

import base64
import json
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, Field


_SCHEMA_VERSION = 1


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


class ComposePin(BaseModel):
    """Signed envelope. ``signature`` is empty until :meth:`sign` is called.

    The unsigned region (every field except ``signature``) is what gets
    fed to Ed25519. ``signer_pubkey`` is part of the signed region — a
    recipient can't be tricked into accepting a swap by an operator who
    pairs a valid signature with someone else's pubkey.
    """

    schema_version: int = _SCHEMA_VERSION
    tenant_id: str
    allowed_composes: list[str] = Field(default_factory=list)
    scope_agent_id: str
    attested_files_digest: str
    issued_at: int
    exp: int = 0
    signer_pubkey: str = ""
    signature: str = ""

    def _signing_payload(self) -> bytes:
        """Canonical-JSON encoding of the envelope minus ``signature``.

        Using ``sort_keys=True`` + ``separators=(",", ":")`` gives a
        deterministic byte sequence — same envelope, same payload, same
        signature, regardless of dict ordering on either side.
        """
        body = self.model_dump(exclude={"signature"})
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )

    def sign(self, priv: Ed25519PrivateKey) -> "ComposePin":
        """Populate ``signer_pubkey`` and ``signature``. Returns self."""
        pub = priv.public_key()
        from cryptography.hazmat.primitives import serialization

        pub_bytes = pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.signer_pubkey = _b64e(pub_bytes)
        sig = priv.sign(self._signing_payload())
        self.signature = _b64e(sig)
        return self

    def verify(self, expected_pubkey: bytes | None = None) -> bool:
        """Check the signature. If ``expected_pubkey`` is provided, it
        must equal the embedded ``signer_pubkey`` (defends against
        pairing a valid sig with a different pubkey).

        Returns True iff the signature verifies AND (when given) the
        pubkey matches.
        """
        if not self.signature or not self.signer_pubkey:
            return False
        try:
            pub_bytes = _b64d(self.signer_pubkey)
        except Exception:
            return False
        if expected_pubkey is not None and pub_bytes != expected_pubkey:
            return False
        try:
            pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
        except Exception:
            return False
        try:
            sig = _b64d(self.signature)
        except Exception:
            return False
        try:
            pub.verify(sig, self._signing_payload())
            return True
        except InvalidSignature:
            return False

    def is_expired(self, now: int | None = None) -> bool:
        """``exp == 0`` means no expiry."""
        if self.exp == 0:
            return False
        return (now if now is not None else int(time.time())) > self.exp

    def to_json(self) -> str:
        """Canonical JSON string (signed). Stable across runs."""
        return json.dumps(
            self.model_dump(),
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, s: str) -> "ComposePin":
        return cls.model_validate(json.loads(s))


def make_unsigned_pin(
    *,
    tenant_id: str,
    allowed_composes: list[str],
    scope_agent_id: str,
    attested_files_digest: str,
    ttl_seconds: int = 0,
    now: int | None = None,
) -> ComposePin:
    """Build a fresh unsigned :class:`ComposePin`. Caller signs separately.

    ``ttl_seconds=0`` → no expiry. Otherwise ``exp = issued_at +
    ttl_seconds``.
    """
    issued = int(now if now is not None else time.time())
    exp = 0 if ttl_seconds <= 0 else issued + ttl_seconds
    return ComposePin(
        tenant_id=tenant_id,
        allowed_composes=list(allowed_composes),
        scope_agent_id=scope_agent_id,
        attested_files_digest=attested_files_digest,
        issued_at=issued,
        exp=exp,
    )


__all__ = [
    "ComposePin",
    "make_unsigned_pin",
]

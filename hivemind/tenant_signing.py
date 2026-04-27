"""Per-tenant Ed25519 signing key, deterministically derived from ``hmk_``.

Used to sign :class:`hivemind.compose_pin.ComposePin` envelopes — the
record that says "tenant X has approved compose_hash C for scope agent
S, with attested-files digest D, until expiry T". The signature lets a
recipient (B) trust a pin without trusting the server.

Derivation:
- HKDF-SHA256(ikm=token_bytes, salt=tenant_id, info=b"hivemind:tenant-sign:v1")
- 32-byte output → Ed25519 seed
- Deterministic: same ``hmk_ + tenant_id`` always yield the same keypair.

Properties:
- The signing key never leaves CVM RAM. We do NOT persist it; on every
  POST /v1/tenants/{id}/compose-pin we re-derive from the bearer.
- Rotating ``hmk_`` rotates the signing key — old pins remain verifiable
  against the embedded ``signer_pubkey``, but new pins must be re-signed.
- Salt ties the key to ``tenant_id``: an attacker who steals a token
  meant for tenant A can't forge pins for tenant B even with the same
  bytes.
- The ``info`` string domain-separates this key from any other future
  HKDF-derived material on the same token.

Why HKDF instead of scrypt?
- Scrypt is for slow KDFs from low-entropy passwords. ``hmk_`` is
  ``secrets.token_urlsafe(32)`` — already 256 bits of entropy. HKDF is
  the right primitive for high-entropy IKM and runs in microseconds, so
  every signing operation re-derives without per-request cost.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


_INFO = b"hivemind:tenant-sign:v1"
_SEED_LEN = 32


def derive_signing_seed(token: str, tenant_id: str) -> bytes:
    """Return the 32-byte Ed25519 seed derived from ``token + tenant_id``.

    Exposed for tests. Production code should call
    :func:`derive_signing_keypair`.
    """
    if not token:
        raise ValueError("empty token")
    if not tenant_id:
        raise ValueError("empty tenant_id")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=_SEED_LEN,
        salt=tenant_id.encode("utf-8"),
        info=_INFO,
    )
    return hkdf.derive(token.encode("utf-8"))


def derive_signing_keypair(
    token: str, tenant_id: str
) -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Return ``(private_key, public_key)`` for the tenant.

    The keypair is purely a function of the inputs — call it as many
    times as you like, you get the same keys back. Callers should hold
    the private key only as long as they need to sign, then drop it.
    """
    seed = derive_signing_seed(token, tenant_id)
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    return priv, priv.public_key()


__all__ = [
    "derive_signing_keypair",
    "derive_signing_seed",
]

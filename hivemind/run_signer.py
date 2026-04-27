"""dstack-KMS-bound Ed25519 signer for run records.

Phase 5: every run record carries a signature so a recipient can verify
that *this* CVM (compose_hash X) wrote *this* output for *this* prompt.
The trust chain is:

  on-chain HivemindAppAuth (Sepolia)
    └─ governs the set of allowed compose_hashes
       └─ KMS releases keys for those compose_hashes
          └─ ``dstack.get_key("hivemind-runs-v1", "")`` derives this signer
             └─ signer.public_key is published in /v1/attestation
                └─ recipient pins it on first sight (or matches against
                   compose_hash they already trust) and verifies signatures

Determinism — given the same compose_hash + KMS key path, every replica
inside the same enclave-image gets the same Ed25519 keypair (the seed is
KMS-released; the public key is bit-identical). So restarts and rolling
deploys of the same compose are restart-stable: B's pinned pubkey
keeps matching after a hivemind-core restart, no re-trust ceremony.

Signing surface is `sign_payload(priv_key, payload: dict)`:
  • canonical JSON (sort_keys=True, separators=(",", ":"))
  • raw Ed25519 over the resulting bytes
  • returns (signature_bytes, signer_pubkey_bytes)
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

HIVEMIND_RUNS_KEY_PATH = "hivemind-runs-v1"


def derive_run_signer(dstack: Any) -> tuple[Ed25519PrivateKey, bytes]:
    """Return ``(priv_key, raw_pubkey_bytes)`` derived from KMS.

    Mirrors :func:`hivemind.tls.derive_tls_cert_and_key` — same ``get_key``
    call, same fold-into-sha256 for domain separation. The 32-byte
    digest becomes the Ed25519 seed (RFC 8032 §5.1.5).
    ``raw_pubkey_bytes`` is the 32-byte Ed25519 public key in raw form
    (what a verifier loads via ``Ed25519PublicKey.from_public_bytes``).
    """
    seed_resp = dstack.get_key(HIVEMIND_RUNS_KEY_PATH, "")
    seed = (
        bytes.fromhex(seed_resp.key)
        if isinstance(seed_resp.key, str)
        else seed_resp.key
    )
    seed32 = hashlib.sha256(
        HIVEMIND_RUNS_KEY_PATH.encode() + b"|" + seed[:32]
    ).digest()
    priv = Ed25519PrivateKey.from_private_bytes(seed32)
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv, pub_bytes


def canonical_json(payload: dict[str, Any]) -> bytes:
    """Stable bytes for signing/hashing run payloads.

    ``sort_keys`` + the tightest ``separators`` give a single canonical
    encoding regardless of dict insertion order or Python version, so
    the recipient's verifier reproduces the exact bytes the enclave
    signed.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sign_payload(
    priv_key: Ed25519PrivateKey, payload: dict[str, Any],
) -> tuple[bytes, bytes]:
    """Sign ``payload`` (canonical JSON). Returns ``(sig, body_bytes)``."""
    body = canonical_json(payload)
    return priv_key.sign(body), body


def verify_payload(
    pubkey_bytes: bytes,
    payload: dict[str, Any],
    signature: bytes,
) -> bool:
    """Verify a signed run payload. Returns True iff signature checks out.

    Used by the CLI (and any out-of-band auditor). Raising would be
    noisier than necessary — the caller decides what to do on False.
    """
    try:
        pub = Ed25519PublicKey.from_public_bytes(pubkey_bytes)
        pub.verify(signature, canonical_json(payload))
        return True
    except Exception:
        return False


__all__ = [
    "HIVEMIND_RUNS_KEY_PATH",
    "derive_run_signer",
    "canonical_json",
    "sign_payload",
    "verify_payload",
]

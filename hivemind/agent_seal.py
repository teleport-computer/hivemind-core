"""Phase 6: KMS-derived enclave key for legacy sealed agent files.

Non-room sealed-mode agents (``inspection_mode=sealed`` without a room id)
have their source bytes encrypted under a key derived from
``dstack.get_key("hivemind-agent-private-v1", "")``. The key is:

  • deterministic per CVM (same compose_hash → same key on every restart)
  • released by KMS *only* to the running compose_hash (governance-gated
    via the on-chain HivemindAppAuth contract)
  • never exposed to A's ``hmk_`` token: there's no API path that does
    a token-driven KEK derivation against this key, unlike tenant DEK

The runtime difference vs. ``seal.py`` (tenant DEK):

  - tenant DEK: wrapped under a KEK derived from the bearer (``hmk_``
    or ``hmq_``). A's owner key thaws it. Designed for at-rest
    protection from cold pgdata + 503-when-sealed semantics.
  - agent seal: derived from KMS once at boot. No bearer. Used only
    by build/rebuild paths. The ``files/{path}`` HTTP endpoint
    refuses to serve plaintext for sealed agents — defense in depth.

Room-uploaded sealed query agents do not use this module. Their source bytes
are sealed under the participant-presented room vault key instead, so a
backend restart/update needs a room participant to interact before rebuild
or digest paths can decrypt them.

Threat model: A's tenant role can SELECT ciphertext rows but cannot
decrypt them. Only the running CVM (this compose_hash) can. To add a
"leak this back to A" endpoint, A would need to ship a new compose,
get on-chain governance approval, and have B re-trust the new
compose_hash — at which point B has explicit consent.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

logger = logging.getLogger(__name__)

HIVEMIND_AGENT_PRIVATE_KEY_PATH = "hivemind-agent-private-v1"

_NONCE_LEN = 12
_KEY_LEN = 32

_state: dict[str, Any] = {
    "key": None,           # raw 32-byte ChaCha20 key, or None when KMS unreachable
    "key_path": "",        # KMS path used (for surfacing in attestation)
    "lock": threading.RLock(),
}


def bootstrap(dstack: Any) -> None:
    """Derive the enclave-only ChaCha20 key from KMS.

    Called once during attestation bootstrap. Idempotent. On any
    failure the key stays None and sealed-mode uploads will be
    rejected at the upload boundary (config validation in server.py).
    """
    with _state["lock"]:
        if _state["key"] is not None:
            return
        try:
            seed_resp = dstack.get_key(HIVEMIND_AGENT_PRIVATE_KEY_PATH, "")
            seed = (
                bytes.fromhex(seed_resp.key)
                if isinstance(seed_resp.key, str)
                else seed_resp.key
            )
            key = hashlib.sha256(
                HIVEMIND_AGENT_PRIVATE_KEY_PATH.encode() + b"|" + seed[:32]
            ).digest()
            if len(key) != _KEY_LEN:
                raise ValueError(f"derived key wrong length: {len(key)}")
            _state["key"] = key
            _state["key_path"] = HIVEMIND_AGENT_PRIVATE_KEY_PATH
        except Exception as e:
            logger.warning("agent_seal bootstrap failed: %r", e)
            _state["key"] = None
            _state["key_path"] = ""


def is_available() -> bool:
    """True iff the enclave-only key has been derived (KMS reachable)."""
    with _state["lock"]:
        return _state["key"] is not None


def _key() -> bytes:
    with _state["lock"]:
        k = _state["key"]
    if k is None:
        raise RuntimeError(
            "agent_seal key not available — sealed-mode requires a "
            "live dstack-KMS connection"
        )
    return k


def _aad(agent_id: str, file_path: str) -> bytes:
    """AAD binds ciphertext to (agent_id, path) so rows can't be
    swapped within the same agents table."""
    return f"agent-sealed|{agent_id}|{file_path}".encode("utf-8")


def encrypt_b64(agent_id: str, file_path: str, plaintext: str | bytes) -> str:
    """Encrypt + base64-encode for storage in the ciphertext column."""
    import os

    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    nonce = os.urandom(_NONCE_LEN)
    blob = ChaCha20Poly1305(_key()).encrypt(
        nonce, plaintext, _aad(agent_id, file_path),
    )
    return base64.b64encode(nonce + blob).decode("ascii")


def decrypt_b64(agent_id: str, file_path: str, b64: str) -> str:
    raw = base64.b64decode(b64.encode("ascii"))
    if len(raw) < _NONCE_LEN + 16:
        raise ValueError("agent-sealed ciphertext too short")
    nonce, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
    pt = ChaCha20Poly1305(_key()).decrypt(nonce, ct, _aad(agent_id, file_path))
    return pt.decode("utf-8")


def reset_for_tests() -> None:
    """Clear bootstrap state. Test-only."""
    with _state["lock"]:
        _state["key"] = None
        _state["key_path"] = ""


__all__ = [
    "HIVEMIND_AGENT_PRIVATE_KEY_PATH",
    "bootstrap",
    "is_available",
    "encrypt_b64",
    "decrypt_b64",
    "reset_for_tests",
]

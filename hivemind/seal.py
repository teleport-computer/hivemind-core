"""Tenant-data seal: keys client-held bearer tokens to a per-tenant DEK.

Property: an operator with cold access to pgdata learns nothing about
encrypted blobs. The DEK is unwrappable only by deriving a KEK from a
valid bearer token (``hmk_…`` owner key or ``hmq_…`` capability token),
and live KEK/DEK material exists only in process memory of the running
CVM. After a restart or redeploy, the in-memory DEK cache is empty —
the system is sealed until a client interacts with a valid bearer.
That bearer-presentation is the user's intentional "interaction" gate.

Wire format:
- KDF: scrypt(N=2**15, r=8, p=1, dkLen=32). Memory-hard; ~32 MiB / ~50ms
  on a current laptop. Tuned to be expensive at attack scale (~1000x
  slower than HKDF) without making warm-cold thaw painful at request
  scale (cache hit skips KDF entirely).
- AEAD: ChaCha20-Poly1305 with random 12-byte nonces. ``cryptography`` —
  no new top-level dep.
- Wrapped blob layout: ``nonce(12) || ciphertext_with_tag``.
- AAD on file ciphertext: ``b"file|<tenant_id>|<agent_id>|<path>"``.
  Binds a row to its (tenant, agent, path) so the operator can't swap
  rows across agents to fool a decrypt-and-rebuild path.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

logger = logging.getLogger(__name__)


# scrypt parameters. Bumped down from the maximum to keep first-thaw
# latency tolerable on the small CVM CPUs (Phala default 2 vCPU). 2**15
# is ~50ms locally and ~150ms on the CVM — acceptable for "first request
# after restart". Cache hits skip KDF entirely.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_DEK_LEN = 32
_NONCE_LEN = 12
_SALT_LEN = 16


@dataclass(frozen=True)
class KdfParams:
    """Persisted KDF parameter bundle. Lets us migrate parameters per
    tenant without invalidating existing rows."""

    n: int = _SCRYPT_N
    r: int = _SCRYPT_R
    p: int = _SCRYPT_P
    length: int = _KEY_LEN

    def to_json(self) -> str:
        return json.dumps({"kdf": "scrypt", "n": self.n, "r": self.r,
                           "p": self.p, "length": self.length})

    @classmethod
    def from_json(cls, data: str | None) -> KdfParams:
        if not data:
            return cls()
        try:
            d = json.loads(data)
        except (TypeError, ValueError):
            return cls()
        if d.get("kdf") not in (None, "scrypt"):
            raise ValueError(f"unsupported KDF {d.get('kdf')!r}")
        return cls(
            n=int(d.get("n", _SCRYPT_N)),
            r=int(d.get("r", _SCRYPT_R)),
            p=int(d.get("p", _SCRYPT_P)),
            length=int(d.get("length", _KEY_LEN)),
        )


def derive_kek(token: str, salt: bytes, params: KdfParams | None = None) -> bytes:
    """Derive a 32-byte KEK from a bearer token + salt. Memory-hard.

    Token bytes are taken raw — they're already high-entropy
    (``secrets.token_urlsafe(32)`` minted by tenants.py). Tying KDF
    inputs strictly to ``token + salt`` (no extra "context" string)
    keeps the wire format minimal and the cross-implementation surface
    obvious."""
    if not token:
        raise ValueError("empty token")
    if len(salt) < 8:
        raise ValueError("salt too short")
    p = params or KdfParams()
    kdf = Scrypt(salt=bytes(salt), length=p.length, n=p.n, r=p.r, p=p.p)
    return kdf.derive(token.encode("utf-8"))


def _aead_seal(key: bytes, plaintext: bytes, aad: bytes | None) -> bytes:
    """ChaCha20-Poly1305 seal → nonce || ciphertext_with_tag."""
    nonce = os.urandom(_NONCE_LEN)
    ct = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)
    return nonce + ct


def _aead_open(key: bytes, blob: bytes, aad: bytes | None) -> bytes:
    if len(blob) < _NONCE_LEN + 16:  # nonce + min poly1305 tag
        raise ValueError("ciphertext too short")
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return ChaCha20Poly1305(key).decrypt(nonce, ct, aad)


def wrap_dek(kek: bytes, dek: bytes) -> bytes:
    """Wrap a DEK under a KEK. AAD=b'dek-wrap-v1' to domain-separate
    from file ciphertexts."""
    return _aead_seal(kek, dek, b"dek-wrap-v1")


def unwrap_dek(kek: bytes, wrapped: bytes) -> bytes:
    return _aead_open(kek, wrapped, b"dek-wrap-v1")


def file_aad(tenant_id: str, agent_id: str, file_path: str) -> bytes:
    return f"file|{tenant_id}|{agent_id}|{file_path}".encode("utf-8")


def encrypt_file(dek: bytes, plaintext: str | bytes, aad: bytes) -> bytes:
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    return _aead_seal(dek, plaintext, aad)


def decrypt_file(dek: bytes, blob: bytes, aad: bytes) -> str:
    return _aead_open(dek, blob, aad).decode("utf-8")


def new_dek() -> bytes:
    return os.urandom(_DEK_LEN)


def new_salt() -> bytes:
    return os.urandom(_SALT_LEN)


class TenantSealed(Exception):
    """Raised when an operation needs a DEK but none is cached.

    Server should translate this to HTTP 503 with a clear message:
    the tenant's encrypted data can't be read until a valid bearer
    token has been presented since the last process start."""


class TenantSealer:
    """Process-wide DEK cache, keyed by tenant_id.

    Holds plaintext DEKs strictly in RAM. Eviction is restart-only —
    no TTL, no refcount. The only inputs that can populate the cache
    are valid bearer tokens (validated upstream by the registry).

    Thread-safe. ``unseal_with`` does the KDF + AEAD-unwrap; cache hits
    bypass it entirely so steady-state per-request cost is a dict
    lookup."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: dict[str, bytes] = {}

    def is_unsealed(self, tenant_id: str) -> bool:
        with self._lock:
            return tenant_id in self._cache

    def get_dek(self, tenant_id: str) -> bytes:
        with self._lock:
            dek = self._cache.get(tenant_id)
        if dek is None:
            raise TenantSealed(
                f"tenant {tenant_id!r} is sealed — present a valid "
                "bearer token (hmk_ or hmq_) to thaw before making "
                "data requests"
            )
        return dek

    def cache(self, tenant_id: str, dek: bytes) -> None:
        if len(dek) != _DEK_LEN:
            raise ValueError(f"DEK must be {_DEK_LEN} bytes")
        with self._lock:
            self._cache[tenant_id] = dek

    def evict(self, tenant_id: str) -> None:
        with self._lock:
            self._cache.pop(tenant_id, None)

    def unseal_with(
        self,
        tenant_id: str,
        token: str,
        salt: bytes,
        wrapped_dek: bytes,
        params: KdfParams | None = None,
    ) -> bytes:
        """Derive KEK from token+salt, unwrap the DEK, and cache it."""
        kek = derive_kek(token, salt, params)
        try:
            dek = unwrap_dek(kek, wrapped_dek)
        finally:
            # Best-effort scrub of the ephemeral KEK. Python doesn't
            # really let us zero memory but at least drop the binding.
            del kek
        if len(dek) != _DEK_LEN:
            raise ValueError("unwrapped DEK has unexpected length")
        self.cache(tenant_id, dek)
        return dek


__all__ = [
    "KdfParams",
    "TenantSealed",
    "TenantSealer",
    "decrypt_file",
    "derive_kek",
    "encrypt_file",
    "file_aad",
    "new_dek",
    "new_salt",
    "unwrap_dek",
    "wrap_dek",
]

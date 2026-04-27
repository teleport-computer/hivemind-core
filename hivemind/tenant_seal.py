"""Per-tenant seal IO: read/write the ``_hivemind_tenant_kek`` row and
expose a single ``ensure_unsealed(token)`` entrypoint that turns a
bearer token + tenant DB into a cached DEK on the process-wide
:class:`TenantSealer`.

Kept apart from :mod:`hivemind.seal` so the crypto module stays pure
and independently testable. This module handles the database-shaped
parts: the singleton row, base64 transport encoding, and lazy first-time
DEK provisioning when an owner interacts with a fresh tenant.
"""

from __future__ import annotations

import base64
import logging
import time

from .seal import (
    KdfParams,
    TenantSealer,
    derive_kek,
    new_dek,
    new_salt,
    unwrap_dek,
    wrap_dek,
)

logger = logging.getLogger(__name__)


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _b64d(s: str) -> bytes:
    return base64.b64decode(s)


def load_seal_record(db) -> tuple[bytes, bytes, KdfParams] | None:
    """Read the singleton _hivemind_tenant_kek row.

    Returns ``(salt, wrapped_dek, params)`` or ``None`` if the row has
    never been written. Decoding is the inverse of :func:`save_seal_record`.
    """
    rows = db.execute(
        "SELECT salt, wrapped_dek, kdf_params "
        "FROM _hivemind_tenant_kek WHERE singleton = TRUE"
    )
    if not rows:
        return None
    r = rows[0]
    return _b64d(r["salt"]), _b64d(r["wrapped_dek"]), KdfParams.from_json(
        r["kdf_params"]
    )


def save_seal_record(
    db, salt: bytes, wrapped_dek: bytes, params: KdfParams,
) -> None:
    db.execute_commit(
        "INSERT INTO _hivemind_tenant_kek "
        "(singleton, salt, wrapped_dek, kdf_params, created_at) "
        "VALUES (TRUE, %s, %s, %s, %s) "
        "ON CONFLICT (singleton) DO UPDATE SET "
        "salt = EXCLUDED.salt, "
        "wrapped_dek = EXCLUDED.wrapped_dek, "
        "kdf_params = EXCLUDED.kdf_params",
        [_b64e(salt), _b64e(wrapped_dek), params.to_json(), time.time()],
    )


def ensure_unsealed(
    sealer: TenantSealer,
    db,
    tenant_id: str,
    bearer: str,
    *,
    can_initialize: bool,
) -> bool:
    """Populate the sealer's DEK cache for ``tenant_id`` using ``bearer``.

    - If the cache is already warm, no-op (``True``).
    - If a seal record exists, derive KEK from ``bearer`` + stored salt,
      unwrap the DEK, cache. Returns ``True`` on success, ``False`` if
      unwrap fails (wrong key — surface as 401 by the caller).
    - If no seal record exists and ``can_initialize`` is True, mint a
      fresh DEK, derive KEK from ``bearer`` + a fresh salt, wrap, store,
      cache. Returns ``True``.
    - If no seal record exists and ``can_initialize`` is False (capability
      token on a never-thawed tenant), returns ``False``. Owner has to
      touch the system once before capabilities can read encrypted data.
    """
    if sealer.is_unsealed(tenant_id):
        return True
    rec = load_seal_record(db)
    if rec is None:
        if not can_initialize:
            logger.info(
                "tenant %s has no seal record and bearer is not owner; "
                "leaving sealed", tenant_id,
            )
            return False
        salt = new_salt()
        params = KdfParams()
        dek = new_dek()
        kek = derive_kek(bearer, salt, params)
        try:
            wrapped = wrap_dek(kek, dek)
        finally:
            del kek
        save_seal_record(db, salt, wrapped, params)
        sealer.cache(tenant_id, dek)
        logger.info("tenant %s seal initialized (first owner interaction)",
                    tenant_id)
        return True
    salt, wrapped_dek, params = rec
    kek = derive_kek(bearer, salt, params)
    try:
        dek = unwrap_dek(kek, wrapped_dek)
    except Exception as e:
        # Unwrap failure means the bearer doesn't match the KEK — most
        # commonly a rotated owner key against an old seal record. We
        # surface this as "auth-equivalent failure" so the caller can
        # 401, distinct from "tenant locked" (TenantSealed).
        logger.warning("tenant %s seal unwrap failed: %s", tenant_id, e)
        return False
    finally:
        del kek
    sealer.cache(tenant_id, dek)
    return True

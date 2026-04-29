"""Participant-presented key release for room data.

Room manifests describe who may enter and what may leave. The room vault
stores the owner's room data under a random room DEK that is never derived
from the CVM/KMS. The DEK is wrapped to participant bearer tokens instead:
the owner ``hmk_`` and each room invite ``hmq_`` get their own wrap row.

After a restart or redeploy, the in-memory room DEK cache is empty. A room
with vault items can only be read again when a participant presents a bearer
token that has a wrap for that room. That is the intended interaction gate:
an operator-approved malicious update cannot decrypt old room data unless a
participant interacts with that updated backend.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from typing import Any

from cryptography.exceptions import InvalidTag

from .seal import (
    KdfParams,
    decrypt_file,
    derive_kek,
    encrypt_file,
    new_dek,
    new_salt,
    unwrap_dek,
    wrap_dek,
)


def _b64e(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64d(value: str) -> bytes:
    return base64.b64decode((value or "").encode("ascii"))


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class RoomVaultSealed(Exception):
    """Raised when room vault data exists but no participant has opened it."""


class RoomVault:
    """Encrypted room document store scoped to one tenant database."""

    def __init__(self, db, *, tenant_id: str | None = None):
        self.db = db
        self.tenant_id = tenant_id or ""
        self._lock = threading.RLock()
        self._cache: dict[str, bytes] = {}

    def _cache_key(self, room_id: str) -> str:
        return f"{self.tenant_id}:{room_id}"

    def _item_aad(self, room_id: str, item_id: str) -> bytes:
        return (
            f"room-vault-item|{self.tenant_id}|{room_id}|{item_id}"
        ).encode("utf-8")

    def _agent_file_aad(self, room_id: str, agent_id: str, file_path: str) -> bytes:
        return (
            f"room-vault-agent-file|{self.tenant_id}|{room_id}|"
            f"{agent_id}|{file_path}"
        ).encode("utf-8")

    def _cached_dek(self, room_id: str) -> bytes | None:
        with self._lock:
            return self._cache.get(self._cache_key(room_id))

    def is_open(self, room_id: str) -> bool:
        return self._cached_dek(room_id) is not None

    def evict(self, room_id: str) -> None:
        with self._lock:
            self._cache.pop(self._cache_key(room_id), None)

    def evict_all(self) -> None:
        with self._lock:
            self._cache.clear()

    def _cache_dek(self, room_id: str, dek: bytes) -> None:
        with self._lock:
            self._cache[self._cache_key(room_id)] = dek

    def _wrap_row(self, room_id: str, wrap_id: str) -> dict | None:
        rows = self.db.execute(
            "SELECT salt, wrapped_dek, kdf_params "
            "FROM _hivemind_room_key_wraps "
            "WHERE room_id = %s AND wrap_id = %s",
            [room_id, wrap_id],
        )
        return rows[0] if rows else None

    def _wrap_count(self, room_id: str) -> int:
        rows = self.db.execute(
            "SELECT COUNT(*) AS n FROM _hivemind_room_key_wraps "
            "WHERE room_id = %s",
            [room_id],
        )
        return int(rows[0]["n"]) if rows else 0

    def item_count(self, room_id: str) -> int:
        rows = self.db.execute(
            "SELECT COUNT(*) AS n FROM _hivemind_room_vault_items "
            "WHERE room_id = %s",
            [room_id],
        )
        return int(rows[0]["n"]) if rows else 0

    def ensure_room_key(self, room_id: str, wrap_id: str, bearer: str) -> bytes:
        """Return an open room DEK, initializing only a room with no wraps.

        Owners call this during room creation and vault writes. For existing
        rooms it behaves like ``open`` for the supplied participant wrap.
        """
        row = self._wrap_row(room_id, wrap_id)
        cached = self._cached_dek(room_id)
        if row and cached is not None:
            return cached
        if row:
            return self.open(room_id, wrap_id, bearer)
        if self._wrap_count(room_id):
            raise RoomVaultSealed(
                f"room {room_id!r} has no key wrap for this participant"
            )
        dek = new_dek()
        self._cache_dek(room_id, dek)
        self.add_wrap(room_id, wrap_id, bearer, dek=dek)
        return dek

    def add_wrap(
        self,
        room_id: str,
        wrap_id: str,
        bearer: str,
        *,
        dek: bytes | None = None,
    ) -> None:
        """Wrap the room DEK to one participant bearer token."""
        dek = dek or self._cached_dek(room_id)
        if dek is None:
            raise RoomVaultSealed(
                f"room {room_id!r} is sealed; open it before adding a key wrap"
            )
        salt = new_salt()
        params = KdfParams()
        kek = derive_kek(bearer, salt, params)
        try:
            wrapped = wrap_dek(kek, dek)
        finally:
            del kek
        self.db.execute_commit(
            "INSERT INTO _hivemind_room_key_wraps "
            "(room_id, wrap_id, salt, wrapped_dek, kdf_params, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (room_id, wrap_id) DO UPDATE SET "
            "salt = EXCLUDED.salt, wrapped_dek = EXCLUDED.wrapped_dek, "
            "kdf_params = EXCLUDED.kdf_params",
            [
                room_id,
                wrap_id,
                _b64e(salt),
                _b64e(wrapped),
                params.to_json(),
                time.time(),
            ],
        )

    def open(self, room_id: str, wrap_id: str, bearer: str) -> bytes:
        """Open a room DEK with the participant's current bearer token."""
        row = self._wrap_row(room_id, wrap_id)
        if not row:
            raise RoomVaultSealed(
                f"room {room_id!r} has no key wrap for this participant"
            )
        cached = self._cached_dek(room_id)
        if cached is not None:
            return cached
        params = KdfParams.from_json(row.get("kdf_params"))
        kek = derive_kek(bearer, _b64d(row["salt"]), params)
        try:
            dek = unwrap_dek(kek, _b64d(row["wrapped_dek"]))
        except (InvalidTag, ValueError) as e:
            raise RoomVaultSealed(
                f"room {room_id!r} could not be opened with this bearer"
            ) from e
        finally:
            del kek
        self._cache_dek(room_id, dek)
        return dek

    def status(self, room_id: str) -> dict[str, Any]:
        return {
            "room_id": room_id,
            "open": self.is_open(room_id),
            "wrap_count": self._wrap_count(room_id),
            "item_count": self.item_count(room_id),
        }

    def put_item(
        self,
        room_id: str,
        *,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        dek = self._cached_dek(room_id)
        if dek is None:
            raise RoomVaultSealed(
                f"room {room_id!r} is sealed; open it before writing vault data"
            )
        item_id = f"rvi_{secrets.token_hex(8)}"
        created_at = time.time()
        metadata_json = _json(metadata or {})
        ciphertext = encrypt_file(dek, text, self._item_aad(room_id, item_id))
        self.db.execute_commit(
            "INSERT INTO _hivemind_room_vault_items "
            "(room_id, item_id, ciphertext, metadata, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            [room_id, item_id, _b64e(ciphertext), metadata_json, created_at],
        )
        return {
            "room_id": room_id,
            "item_id": item_id,
            "metadata": json.loads(metadata_json),
            "created_at": created_at,
            "size_bytes": len(text.encode("utf-8")),
        }

    def _item_rows(self, room_id: str) -> list[dict]:
        return self.db.execute(
            "SELECT room_id, item_id, ciphertext, metadata, created_at "
            "FROM _hivemind_room_vault_items "
            "WHERE room_id = %s ORDER BY created_at ASC, item_id ASC",
            [room_id],
        )

    def list_items(self, room_id: str) -> list[dict[str, Any]]:
        rows = self._item_rows(room_id)
        if not rows:
            return []
        dek = self._cached_dek(room_id)
        if dek is None:
            raise RoomVaultSealed(
                f"room {room_id!r} is sealed; present a participant bearer "
                "before reading vault data"
            )
        out: list[dict[str, Any]] = []
        for row in rows:
            item_id = row["item_id"]
            text = decrypt_file(
                dek,
                _b64d(row["ciphertext"]),
                self._item_aad(room_id, item_id),
            )
            try:
                metadata = json.loads(row.get("metadata") or "{}")
            except (TypeError, ValueError):
                metadata = {}
            out.append(
                {
                    "room_id": room_id,
                    "item_id": item_id,
                    "text": text,
                    "metadata": metadata,
                    "created_at": row["created_at"],
                    "size_bytes": len(text.encode("utf-8")),
                }
            )
        return out

    def encrypt_agent_file_b64(
        self,
        room_id: str,
        agent_id: str,
        file_path: str,
        plaintext: str | bytes,
    ) -> str:
        dek = self._cached_dek(room_id)
        if dek is None:
            raise RoomVaultSealed(
                f"room {room_id!r} is sealed; open it before sealing agent files"
            )
        ciphertext = encrypt_file(
            dek,
            plaintext,
            self._agent_file_aad(room_id, agent_id, file_path),
        )
        return _b64e(ciphertext)

    def decrypt_agent_file_b64(
        self,
        room_id: str,
        agent_id: str,
        file_path: str,
        ciphertext_b64: str,
    ) -> str:
        dek = self._cached_dek(room_id)
        if dek is None:
            raise RoomVaultSealed(
                f"room {room_id!r} is sealed; present a participant bearer "
                "before reading sealed room agent files"
            )
        return decrypt_file(
            dek,
            _b64d(ciphertext_b64),
            self._agent_file_aad(room_id, agent_id, file_path),
        )

    def list_items_for_bearer(
        self,
        room_id: str,
        wrap_id: str,
        bearer: str,
    ) -> list[dict[str, Any]]:
        if self.item_count(room_id) == 0:
            return []
        self.open(room_id, wrap_id, bearer)
        return self.list_items(room_id)


__all__ = ["RoomVault", "RoomVaultSealed"]

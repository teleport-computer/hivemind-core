from __future__ import annotations

import base64
import logging
import time
from typing import TYPE_CHECKING

from .models import AgentConfig

if TYPE_CHECKING:
    from ..db import Database
    from ..room_vault import RoomVault
    from ..seal import TenantSealer

logger = logging.getLogger(__name__)


class AgentSealedReadError(Exception):
    """Raised when a plaintext read is attempted on a sealed-mode agent.

    Sealed agents have ``inspection_mode='sealed'``. Their source bytes
    are encrypted for runtime-only use (KMS for legacy/non-room agents,
    room-vault DEK for room-uploaded agents), and HTTP endpoints catch
    this and translate it to HTTP 403.
    """

    def __init__(self, agent_id: str):
        super().__init__(
            f"agent {agent_id!r} is sealed (inspection_mode=sealed); "
            "source bytes are not readable through this endpoint"
        )
        self.agent_id = agent_id


class AgentStore:
    """CRUD for registered agent configurations and extracted source files.

    Backed by Postgres tables _hivemind_agents and _hivemind_agent_files
    (bootstrapped by Database.__init__).

    File contents are encrypted at rest when a :class:`TenantSealer`
    has a cached DEK for ``tenant_id``: the in-memory DEK is acquired
    via the per-request bearer (``hmk_`` or ``hmq_``). Pgdata-only access
    sees ciphertext; reads in a sealed state raise ``TenantSealed``.

    Legacy plaintext rows (uploaded before the seal landed) are still
    readable as-is, so this is a non-disruptive migration: new uploads
    encrypt, old uploads keep working.
    """

    def __init__(
        self,
        db: Database,
        sealer: TenantSealer | None = None,
        tenant_id: str | None = None,
        room_vault: RoomVault | None = None,
    ):
        self.db = db
        self.sealer = sealer
        self.tenant_id = tenant_id
        self.room_vault = room_vault

    # ── helpers ────────────────────────────────────────────────────

    def _seal_active(self) -> bool:
        """Encryption is "active" when the sealer is bound AND the
        tenant's DEK is in the cache. We do not raise ``TenantSealed``
        on writes because legitimate write paths (system bootstrap of
        default agents at Hivemind construction) run before the owner
        has thawed the seal; those rows stay plaintext, which is fine
        because they hold public-image-derived bootstrap content, not
        user-uploaded data. Reads, in contrast, do raise on a cold
        cache when the row is ciphertext — there's no fallback."""
        if self.sealer is None or not self.tenant_id:
            return False
        return self.sealer.is_unsealed(self.tenant_id)

    def _encode_ct(self, plaintext: str, agent_id: str, file_path: str) -> str:
        from ..seal import encrypt_file, file_aad

        dek = self.sealer.get_dek(self.tenant_id)  # type: ignore[union-attr]
        aad = file_aad(self.tenant_id or "", agent_id, file_path)
        return base64.b64encode(encrypt_file(dek, plaintext, aad)).decode()

    def _decode_ct(self, b64: str, agent_id: str, file_path: str) -> str:
        from ..seal import decrypt_file, file_aad

        if self.sealer is None or not self.tenant_id:
            raise RuntimeError(
                "AgentStore has no sealer bound but row is ciphertext"
            )
        dek = self.sealer.get_dek(self.tenant_id)
        aad = file_aad(self.tenant_id or "", agent_id, file_path)
        return decrypt_file(dek, base64.b64decode(b64), aad)

    def create(self, config: AgentConfig) -> AgentConfig:
        """Register a new agent."""
        self.db.execute_commit(
            "INSERT INTO _hivemind_agents "
            "(agent_id, name, description, agent_type, image, entrypoint, "
            "memory_mb, max_llm_calls, max_tokens, timeout_seconds, "
            "inspection_mode, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                config.agent_id,
                config.name,
                config.description,
                config.agent_type,
                config.image,
                config.entrypoint,
                config.memory_mb,
                config.max_llm_calls,
                config.max_tokens,
                config.timeout_seconds,
                getattr(config, "inspection_mode", "full") or "full",
                time.time(),
            ],
        )
        return config

    def upsert(self, config: AgentConfig) -> AgentConfig:
        """Create or update an agent by ID."""
        self.db.execute_commit(
            """
            INSERT INTO _hivemind_agents
            (agent_id, name, description, agent_type, image, entrypoint,
             memory_mb, max_llm_calls, max_tokens, timeout_seconds,
             inspection_mode, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(agent_id) DO UPDATE SET
                name=EXCLUDED.name,
                description=EXCLUDED.description,
                agent_type=EXCLUDED.agent_type,
                image=EXCLUDED.image,
                entrypoint=EXCLUDED.entrypoint,
                memory_mb=EXCLUDED.memory_mb,
                max_llm_calls=EXCLUDED.max_llm_calls,
                max_tokens=EXCLUDED.max_tokens,
                timeout_seconds=EXCLUDED.timeout_seconds,
                inspection_mode=EXCLUDED.inspection_mode
            """,
            [
                config.agent_id,
                config.name,
                config.description,
                config.agent_type,
                config.image,
                config.entrypoint,
                config.memory_mb,
                config.max_llm_calls,
                config.max_tokens,
                config.timeout_seconds,
                getattr(config, "inspection_mode", "full") or "full",
                time.time(),
            ],
        )
        return config

    def _row_to_config(self, r: dict) -> AgentConfig:
        return AgentConfig(
            agent_id=r["agent_id"],
            name=r["name"],
            description=r["description"],
            agent_type=r.get("agent_type", "query"),
            image=r["image"],
            entrypoint=r["entrypoint"],
            memory_mb=r["memory_mb"],
            max_llm_calls=r["max_llm_calls"],
            max_tokens=r["max_tokens"],
            timeout_seconds=r["timeout_seconds"],
            inspection_mode=r.get("inspection_mode") or "full",
        )

    def get(self, agent_id: str) -> AgentConfig | None:
        """Look up an agent by ID."""
        rows = self.db.execute(
            "SELECT agent_id, name, description, agent_type, image, entrypoint, "
            "memory_mb, max_llm_calls, max_tokens, timeout_seconds, "
            "inspection_mode "
            "FROM _hivemind_agents WHERE agent_id = %s",
            [agent_id],
        )
        if not rows:
            return None
        return self._row_to_config(rows[0])

    def list_agents(self, agent_type: str | None = None) -> list[AgentConfig]:
        """List registered agents, optionally filtered by type."""
        if agent_type:
            rows = self.db.execute(
                "SELECT agent_id, name, description, agent_type, image, entrypoint, "
                "memory_mb, max_llm_calls, max_tokens, timeout_seconds, "
                "inspection_mode "
                "FROM _hivemind_agents WHERE agent_type = %s ORDER BY created_at DESC",
                [agent_type],
            )
        else:
            rows = self.db.execute(
                "SELECT agent_id, name, description, agent_type, image, entrypoint, "
                "memory_mb, max_llm_calls, max_tokens, timeout_seconds, "
                "inspection_mode "
                "FROM _hivemind_agents ORDER BY created_at DESC"
            )
        return [self._row_to_config(r) for r in rows]

    def _agent_inspection_mode(self, agent_id: str) -> str:
        """Look up a single agent's inspection_mode. Returns 'full' if
        the agent isn't registered yet (e.g. files saved before the
        record exists)."""
        rows = self.db.execute(
            "SELECT inspection_mode FROM _hivemind_agents "
            "WHERE agent_id = %s",
            [agent_id],
        )
        if not rows:
            return "full"
        return (rows[0].get("inspection_mode") or "full").strip() or "full"

    def save_files(
        self,
        agent_id: str,
        files: dict[str, str],
        private_paths: list[str] | None = None,
        inspection_mode: str | None = None,
        room_id: str | None = None,
    ) -> int:
        """Store extracted source files for an agent. Returns file count.

        Encryption routing:
          • ``inspection_mode='sealed'`` with ``room_id`` → ChaCha20
            under that room's participant-presented DEK. After restart,
            internal rebuild/digest paths can decrypt only after a room
            participant opens the room vault.
          • ``inspection_mode='sealed'`` without ``room_id`` → legacy
            ChaCha20 under the enclave-only KMS key
            (``agent_seal.encrypt_b64``). The files HTTP endpoint
            refuses to serve plaintext in either sealed case.
          • ``inspection_mode='full'`` (default) → encrypt under the
            tenant DEK if a sealer is bound, else store plaintext.
            Owner endpoint can decrypt, matching legacy behaviour.

        ``inspection_mode=None`` falls back to the agent record's stored
        mode (so the upload-endpoint → save_files chain doesn't have
        to re-thread it on rebuilds).

        ``private_paths`` marks specific files non-attestable: their
        contents are excluded from ``attested_files_digest`` (the digest
        recipients verify against published source). They remain bound
        by ``image_digest`` because the Docker image was built with them.
        Defaults to all files attestable (backwards-compatible).
        """
        mode = (inspection_mode or self._agent_inspection_mode(agent_id)
                or "full").strip() or "full"
        private = set(private_paths or [])
        room_id = (room_id or "").strip() or None
        if mode == "sealed":
            if room_id:
                if self.room_vault is None:
                    raise RuntimeError(
                        "room-sealed agent requested but no RoomVault is bound"
                    )
                for path, content in files.items():
                    size = len(content.encode())
                    attestable = path not in private
                    ct = self.room_vault.encrypt_agent_file_b64(
                        room_id, agent_id, path, content,
                    )
                    self.db.execute_commit(
                        "INSERT INTO _hivemind_agent_files "
                        "(agent_id, file_path, content, ciphertext, "
                        "seal_mode, room_id, size_bytes, attestable) "
                        "VALUES (%s, %s, NULL, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (agent_id, file_path) DO UPDATE SET "
                        "content=NULL, ciphertext=EXCLUDED.ciphertext, "
                        "seal_mode=EXCLUDED.seal_mode, "
                        "room_id=EXCLUDED.room_id, "
                        "size_bytes=EXCLUDED.size_bytes, "
                        "attestable=EXCLUDED.attestable",
                        [agent_id, path, ct, "room", room_id, size, attestable],
                    )
                return len(files)
            from .. import agent_seal as _aseal

            if not _aseal.is_available():
                raise RuntimeError(
                    "sealed-mode agent requested but agent_seal key is "
                    "not available — run inside a TEE with KMS access"
                )
            for path, content in files.items():
                size = len(content.encode())
                attestable = path not in private
                ct = _aseal.encrypt_b64(agent_id, path, content)
                self.db.execute_commit(
                    "INSERT INTO _hivemind_agent_files "
                    "(agent_id, file_path, content, ciphertext, "
                    "seal_mode, room_id, size_bytes, attestable) "
                    "VALUES (%s, %s, NULL, %s, %s, NULL, %s, %s) "
                    "ON CONFLICT (agent_id, file_path) DO UPDATE SET "
                    "content=NULL, ciphertext=EXCLUDED.ciphertext, "
                    "seal_mode=EXCLUDED.seal_mode, "
                    "room_id=EXCLUDED.room_id, "
                    "size_bytes=EXCLUDED.size_bytes, "
                    "attestable=EXCLUDED.attestable",
                    [agent_id, path, ct, "kms", size, attestable],
                )
            return len(files)
        encrypt = self._seal_active()
        for path, content in files.items():
            size = len(content.encode())
            attestable = path not in private
            if encrypt:
                ct = self._encode_ct(content, agent_id, path)
                self.db.execute_commit(
                    "INSERT INTO _hivemind_agent_files "
                    "(agent_id, file_path, content, ciphertext, "
                    "seal_mode, room_id, size_bytes, attestable) "
                    "VALUES (%s, %s, NULL, %s, %s, NULL, %s, %s) "
                    "ON CONFLICT (agent_id, file_path) DO UPDATE SET "
                    "content=NULL, ciphertext=EXCLUDED.ciphertext, "
                    "seal_mode=EXCLUDED.seal_mode, "
                    "room_id=EXCLUDED.room_id, "
                    "size_bytes=EXCLUDED.size_bytes, "
                    "attestable=EXCLUDED.attestable",
                    [agent_id, path, ct, "tenant", size, attestable],
                )
            else:
                self.db.execute_commit(
                    "INSERT INTO _hivemind_agent_files "
                    "(agent_id, file_path, content, ciphertext, "
                    "seal_mode, room_id, size_bytes, attestable) "
                    "VALUES (%s, %s, %s, NULL, '', NULL, %s, %s) "
                    "ON CONFLICT (agent_id, file_path) DO UPDATE SET "
                    "content=EXCLUDED.content, ciphertext=NULL, "
                    "seal_mode=EXCLUDED.seal_mode, "
                    "room_id=EXCLUDED.room_id, "
                    "size_bytes=EXCLUDED.size_bytes, "
                    "attestable=EXCLUDED.attestable",
                    [agent_id, path, content, size, attestable],
                )
        return len(files)

    def replace_files(
        self,
        agent_id: str,
        files: dict[str, str],
        private_paths: list[str] | None = None,
        inspection_mode: str | None = None,
        room_id: str | None = None,
    ) -> int:
        """Replace all extracted files for an agent."""
        mode = (inspection_mode or self._agent_inspection_mode(agent_id)
                or "full").strip() or "full"
        private = set(private_paths or [])
        room_id = (room_id or "").strip() or None
        self.db.execute_commit(
            "DELETE FROM _hivemind_agent_files WHERE agent_id = %s",
            [agent_id],
        )
        if mode == "sealed":
            if room_id:
                if self.room_vault is None:
                    raise RuntimeError(
                        "room-sealed agent requested but no RoomVault is bound"
                    )
                for path, content in files.items():
                    size = len(content.encode())
                    attestable = path not in private
                    ct = self.room_vault.encrypt_agent_file_b64(
                        room_id, agent_id, path, content,
                    )
                    self.db.execute_commit(
                        "INSERT INTO _hivemind_agent_files "
                        "(agent_id, file_path, content, ciphertext, "
                        "seal_mode, room_id, size_bytes, attestable) "
                        "VALUES (%s, %s, NULL, %s, %s, %s, %s, %s)",
                        [agent_id, path, ct, "room", room_id, size, attestable],
                    )
                return len(files)
            from .. import agent_seal as _aseal

            if not _aseal.is_available():
                raise RuntimeError(
                    "sealed-mode agent requested but agent_seal key is "
                    "not available — run inside a TEE with KMS access"
                )
            for path, content in files.items():
                size = len(content.encode())
                attestable = path not in private
                ct = _aseal.encrypt_b64(agent_id, path, content)
                self.db.execute_commit(
                    "INSERT INTO _hivemind_agent_files "
                    "(agent_id, file_path, content, ciphertext, "
                    "seal_mode, room_id, size_bytes, attestable) "
                    "VALUES (%s, %s, NULL, %s, %s, NULL, %s, %s)",
                    [agent_id, path, ct, "kms", size, attestable],
                )
            return len(files)
        encrypt = self._seal_active()
        for path, content in files.items():
            size = len(content.encode())
            attestable = path not in private
            if encrypt:
                ct = self._encode_ct(content, agent_id, path)
                self.db.execute_commit(
                    "INSERT INTO _hivemind_agent_files "
                    "(agent_id, file_path, content, ciphertext, "
                    "seal_mode, room_id, size_bytes, attestable) "
                    "VALUES (%s, %s, NULL, %s, %s, NULL, %s, %s)",
                    [agent_id, path, ct, "tenant", size, attestable],
                )
            else:
                self.db.execute_commit(
                    "INSERT INTO _hivemind_agent_files "
                    "(agent_id, file_path, content, ciphertext, "
                    "seal_mode, room_id, size_bytes, attestable) "
                    "VALUES (%s, %s, %s, NULL, '', NULL, %s, %s)",
                    [agent_id, path, content, size, attestable],
                )
        return len(files)

    def list_file_paths(self, agent_id: str) -> list[dict]:
        """List extracted files. Returns [{path, size_bytes, attestable}, ...]."""
        rows = self.db.execute(
            "SELECT file_path, size_bytes, attestable "
            "FROM _hivemind_agent_files "
            "WHERE agent_id = %s ORDER BY file_path",
            [agent_id],
        )
        return [
            {
                "path": r["file_path"],
                "size_bytes": r["size_bytes"],
                "attestable": bool(r.get("attestable", True)),
            }
            for r in rows
        ]

    def compute_digests(self, agent_id: str) -> dict:
        """Return ``{files_digest, attested_files_digest, files_count,
        attested_files_count}`` over this agent's stored files.

        ``files_digest`` covers ALL files (the on-disk reality, what
        the image was built from). ``attested_files_digest`` covers only
        files marked ``attestable=True`` — the digest a recipient
        compares against the agent's published source code. Files marked
        non-attestable (e.g. ``.env``, secret prompts) are excluded from
        the attested digest but still part of ``files_digest`` and the
        Docker image.

        Decrypts content on read so the digest is over plaintext (the
        same content the agent's code sees at runtime).
        """
        import hashlib as _h

        # Internal: digests run inside the enclave; sealed agents are
        # legitimately decrypted here so B can verify
        # ``attested_files_digest`` matches what they uploaded.
        files = self.get_files(agent_id, allow_sealed=True)
        attestable_set = {
            r["file_path"]
            for r in self.db.execute(
                "SELECT file_path FROM _hivemind_agent_files "
                "WHERE agent_id = %s AND attestable = TRUE",
                [agent_id],
            )
        }
        h_all = _h.sha256()
        h_att = _h.sha256()
        att_count = 0
        for path in sorted(files):
            content = files[path]
            blob = path.encode("utf-8") + b"\0" + content.encode(
                "utf-8", errors="replace"
            ) + b"\0"
            h_all.update(blob)
            if path in attestable_set:
                h_att.update(blob)
                att_count += 1
        return {
            "files_digest": h_all.hexdigest(),
            "attested_files_digest": h_att.hexdigest(),
            "files_count": len(files),
            "attested_files_count": att_count,
        }

    def _decrypt_row(
        self,
        agent_id: str,
        file_path: str,
        ct_b64: str,
        seal_mode: str | None = None,
        room_id: str | None = None,
    ) -> str:
        """Decrypt a ciphertext row regardless of which key wraps it.

        ``seal_mode`` is explicit for new rows:
          - ``room``: room-vault DEK, participant-presented
          - ``kms``: legacy sealed-agent KMS key
          - ``tenant``: tenant DEK

        Empty legacy rows infer from agent inspection mode.
        """
        seal_mode = (seal_mode or "").strip()
        if seal_mode == "room":
            if self.room_vault is None or not room_id:
                raise RuntimeError(
                    "room-sealed agent row is missing RoomVault or room_id"
                )
            return self.room_vault.decrypt_agent_file_b64(
                room_id, agent_id, file_path, ct_b64,
            )
        if seal_mode == "kms":
            from .. import agent_seal as _aseal
            return _aseal.decrypt_b64(agent_id, file_path, ct_b64)
        if seal_mode == "tenant":
            return self._decode_ct(ct_b64, agent_id, file_path)

        mode = self._agent_inspection_mode(agent_id)
        if mode == "sealed":
            from .. import agent_seal as _aseal
            return _aseal.decrypt_b64(agent_id, file_path, ct_b64)
        return self._decode_ct(ct_b64, agent_id, file_path)

    def read_file(
        self,
        agent_id: str,
        file_path: str,
        *,
        allow_sealed: bool = False,
    ) -> str | None:
        """Read a single extracted file's content. Returns None if not found.

        ``allow_sealed=False`` (default) → if the agent is sealed-mode,
        raise :class:`AgentSealedReadError`. The HTTP files endpoint
        leaves the default to refuse plaintext returns to anyone, even
        the room owner.

        ``allow_sealed=True`` is for internal rebuild/digest paths
        running inside the enclave that legitimately need plaintext.
        """
        rows = self.db.execute(
            "SELECT content, ciphertext, seal_mode, room_id "
            "FROM _hivemind_agent_files "
            "WHERE agent_id = %s AND file_path = %s",
            [agent_id, file_path],
        )
        if not rows:
            return None
        if not allow_sealed and self._agent_inspection_mode(agent_id) == "sealed":
            raise AgentSealedReadError(agent_id)
        r = rows[0]
        if r.get("ciphertext"):
            return self._decrypt_row(
                agent_id,
                file_path,
                r["ciphertext"],
                r.get("seal_mode"),
                r.get("room_id"),
            )
        return r["content"]

    def get_files(
        self, agent_id: str, *, allow_sealed: bool = False,
    ) -> dict[str, str]:
        """Get all extracted files for an agent as {path: content}.

        ``allow_sealed`` mirrors :meth:`read_file`: callers outside the
        enclave's internal rebuild path get a sealed-error rather than
        decrypted bytes.
        """
        rows = self.db.execute(
            "SELECT file_path, content, ciphertext, seal_mode, room_id "
            "FROM _hivemind_agent_files "
            "WHERE agent_id = %s ORDER BY file_path",
            [agent_id],
        )
        if not rows:
            return {}
        if not allow_sealed and self._agent_inspection_mode(agent_id) == "sealed":
            raise AgentSealedReadError(agent_id)
        out: dict[str, str] = {}
        for r in rows:
            if r.get("ciphertext"):
                out[r["file_path"]] = self._decrypt_row(
                    agent_id,
                    r["file_path"],
                    r["ciphertext"],
                    r.get("seal_mode"),
                    r.get("room_id"),
                )
            else:
                out[r["file_path"]] = r["content"] or ""
        return out

    def delete(self, agent_id: str) -> bool:
        """Delete an agent and its extracted files."""
        self.db.execute_commit(
            "DELETE FROM _hivemind_agent_files WHERE agent_id = %s",
            [agent_id],
        )
        rowcount = self.db.execute_commit(
            "DELETE FROM _hivemind_agents WHERE agent_id = %s",
            [agent_id],
        )
        return rowcount > 0

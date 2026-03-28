from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .models import AgentConfig

if TYPE_CHECKING:
    from ..db import Database


class AgentStore:
    """CRUD for registered agent configurations and extracted source files.

    Backed by Postgres tables _hivemind_agents and _hivemind_agent_files
    (bootstrapped by Database.__init__).
    """

    def __init__(self, db: Database):
        self.db = db

    def create(self, config: AgentConfig) -> AgentConfig:
        """Register a new agent."""
        self.db.execute_commit(
            "INSERT INTO _hivemind_agents "
            "(agent_id, name, description, image, entrypoint, "
            "memory_mb, max_llm_calls, max_tokens, timeout_seconds, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                config.agent_id,
                config.name,
                config.description,
                config.image,
                config.entrypoint,
                config.memory_mb,
                config.max_llm_calls,
                config.max_tokens,
                config.timeout_seconds,
                time.time(),
            ],
        )
        return config

    def upsert(self, config: AgentConfig) -> AgentConfig:
        """Create or update an agent by ID."""
        self.db.execute_commit(
            """
            INSERT INTO _hivemind_agents
            (agent_id, name, description, image, entrypoint,
             memory_mb, max_llm_calls, max_tokens, timeout_seconds, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(agent_id) DO UPDATE SET
                name=EXCLUDED.name,
                description=EXCLUDED.description,
                image=EXCLUDED.image,
                entrypoint=EXCLUDED.entrypoint,
                memory_mb=EXCLUDED.memory_mb,
                max_llm_calls=EXCLUDED.max_llm_calls,
                max_tokens=EXCLUDED.max_tokens,
                timeout_seconds=EXCLUDED.timeout_seconds
            """,
            [
                config.agent_id,
                config.name,
                config.description,
                config.image,
                config.entrypoint,
                config.memory_mb,
                config.max_llm_calls,
                config.max_tokens,
                config.timeout_seconds,
                time.time(),
            ],
        )
        return config

    def get(self, agent_id: str) -> AgentConfig | None:
        """Look up an agent by ID."""
        rows = self.db.execute(
            "SELECT agent_id, name, description, image, entrypoint, "
            "memory_mb, max_llm_calls, max_tokens, timeout_seconds "
            "FROM _hivemind_agents WHERE agent_id = %s",
            [agent_id],
        )
        if not rows:
            return None
        r = rows[0]
        return AgentConfig(
            agent_id=r["agent_id"],
            name=r["name"],
            description=r["description"],
            image=r["image"],
            entrypoint=r["entrypoint"],
            memory_mb=r["memory_mb"],
            max_llm_calls=r["max_llm_calls"],
            max_tokens=r["max_tokens"],
            timeout_seconds=r["timeout_seconds"],
        )

    def list_agents(self) -> list[AgentConfig]:
        """List all registered agents."""
        rows = self.db.execute(
            "SELECT agent_id, name, description, image, entrypoint, "
            "memory_mb, max_llm_calls, max_tokens, timeout_seconds "
            "FROM _hivemind_agents ORDER BY created_at DESC"
        )
        return [
            AgentConfig(
                agent_id=r["agent_id"],
                name=r["name"],
                description=r["description"],
                image=r["image"],
                entrypoint=r["entrypoint"],
                memory_mb=r["memory_mb"],
                max_llm_calls=r["max_llm_calls"],
                max_tokens=r["max_tokens"],
                timeout_seconds=r["timeout_seconds"],
            )
            for r in rows
        ]

    def save_files(self, agent_id: str, files: dict[str, str]) -> int:
        """Store extracted source files for an agent. Returns file count."""
        for path, content in files.items():
            self.db.execute_commit(
                "INSERT INTO _hivemind_agent_files "
                "(agent_id, file_path, content, size_bytes) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (agent_id, file_path) DO UPDATE SET "
                "content=EXCLUDED.content, size_bytes=EXCLUDED.size_bytes",
                [agent_id, path, content, len(content.encode())],
            )
        return len(files)

    def replace_files(self, agent_id: str, files: dict[str, str]) -> int:
        """Replace all extracted files for an agent."""
        self.db.execute_commit(
            "DELETE FROM _hivemind_agent_files WHERE agent_id = %s",
            [agent_id],
        )
        for path, content in files.items():
            self.db.execute_commit(
                "INSERT INTO _hivemind_agent_files "
                "(agent_id, file_path, content, size_bytes) VALUES (%s, %s, %s, %s)",
                [agent_id, path, content, len(content.encode())],
            )
        return len(files)

    def list_file_paths(self, agent_id: str) -> list[dict]:
        """List extracted files for an agent. Returns [{path, size_bytes}, ...]."""
        rows = self.db.execute(
            "SELECT file_path, size_bytes FROM _hivemind_agent_files "
            "WHERE agent_id = %s ORDER BY file_path",
            [agent_id],
        )
        return [{"path": r["file_path"], "size_bytes": r["size_bytes"]} for r in rows]

    def read_file(self, agent_id: str, file_path: str) -> str | None:
        """Read a single extracted file's content. Returns None if not found."""
        rows = self.db.execute(
            "SELECT content FROM _hivemind_agent_files "
            "WHERE agent_id = %s AND file_path = %s",
            [agent_id, file_path],
        )
        return rows[0]["content"] if rows else None

    def get_files(self, agent_id: str) -> dict[str, str]:
        """Get all extracted files for an agent as {path: content}."""
        rows = self.db.execute(
            "SELECT file_path, content FROM _hivemind_agent_files "
            "WHERE agent_id = %s ORDER BY file_path",
            [agent_id],
        )
        return {r["file_path"]: r["content"] for r in rows}

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

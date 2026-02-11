import sqlite3
import time

from .models import AgentConfig


class AgentStore:
    """CRUD for registered agent configurations and extracted source files.

    Stores agent metadata and extracted image files in SQLite.
    Agents are Docker images. Source files are extracted at registration
    and stored in a separate table — never exposed via API responses.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._init_table()

    def _init_table(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                image TEXT NOT NULL,
                entrypoint TEXT,
                memory_mb INTEGER NOT NULL DEFAULT 256,
                max_llm_calls INTEGER NOT NULL DEFAULT 20,
                max_tokens INTEGER NOT NULL DEFAULT 100000,
                timeout_seconds INTEGER NOT NULL DEFAULT 120,
                created_at REAL NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_files (
                agent_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                content TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                PRIMARY KEY (agent_id, file_path)
            )
        """)
        self.conn.commit()

    def create(self, config: AgentConfig) -> AgentConfig:
        """Register a new agent."""
        self.conn.execute(
            "INSERT INTO agents "
            "(agent_id, name, description, image, entrypoint, "
            "memory_mb, max_llm_calls, max_tokens, timeout_seconds, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
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
            ),
        )
        self.conn.commit()
        return config

    def get(self, agent_id: str) -> AgentConfig | None:
        """Look up an agent by ID."""
        row = self.conn.execute(
            "SELECT agent_id, name, description, image, entrypoint, "
            "memory_mb, max_llm_calls, max_tokens, timeout_seconds "
            "FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if not row:
            return None
        return AgentConfig(
            agent_id=row[0],
            name=row[1],
            description=row[2],
            image=row[3],
            entrypoint=row[4],
            memory_mb=row[5],
            max_llm_calls=row[6],
            max_tokens=row[7],
            timeout_seconds=row[8],
        )

    def list_agents(self) -> list[AgentConfig]:
        """List all registered agents."""
        rows = self.conn.execute(
            "SELECT agent_id, name, description, image, entrypoint, "
            "memory_mb, max_llm_calls, max_tokens, timeout_seconds "
            "FROM agents ORDER BY created_at DESC"
        ).fetchall()
        return [
            AgentConfig(
                agent_id=r[0],
                name=r[1],
                description=r[2],
                image=r[3],
                entrypoint=r[4],
                memory_mb=r[5],
                max_llm_calls=r[6],
                max_tokens=r[7],
                timeout_seconds=r[8],
            )
            for r in rows
        ]

    def save_files(self, agent_id: str, files: dict[str, str]) -> int:
        """Store extracted source files for an agent. Returns file count.

        Called once at registration time after image filesystem extraction.
        """
        for path, content in files.items():
            self.conn.execute(
                "INSERT OR REPLACE INTO agent_files "
                "(agent_id, file_path, content, size_bytes) VALUES (?, ?, ?, ?)",
                (agent_id, path, content, len(content.encode())),
            )
        self.conn.commit()
        return len(files)

    def list_file_paths(self, agent_id: str) -> list[dict]:
        """List extracted files for an agent. Returns [{path, size_bytes}, ...]."""
        rows = self.conn.execute(
            "SELECT file_path, size_bytes FROM agent_files "
            "WHERE agent_id = ? ORDER BY file_path",
            (agent_id,),
        ).fetchall()
        return [{"path": r[0], "size_bytes": r[1]} for r in rows]

    def read_file(self, agent_id: str, file_path: str) -> str | None:
        """Read a single extracted file's content. Returns None if not found."""
        row = self.conn.execute(
            "SELECT content FROM agent_files WHERE agent_id = ? AND file_path = ?",
            (agent_id, file_path),
        ).fetchone()
        return row[0] if row else None

    def delete(self, agent_id: str) -> bool:
        """Delete an agent and its extracted files."""
        self.conn.execute(
            "DELETE FROM agent_files WHERE agent_id = ?", (agent_id,)
        )
        cursor = self.conn.execute(
            "DELETE FROM agents WHERE agent_id = ?", (agent_id,)
        )
        self.conn.commit()
        return cursor.rowcount > 0

"""Postgres-backed artifact store for query agent outputs.

Replaces the S3 upload path. Agents POST /sandbox/artifact-upload to the
bridge, which writes the bytes directly into _hivemind_query_artifacts.
Callers fetch them via GET /v1/query/runs/{run_id}/artifacts/{filename}.

Retention: artifacts are purged after ARTIFACT_RETENTION_SECONDS (default
24 hours) by the periodic sweeper in hivemind.core.
"""

from __future__ import annotations

import time

from ..db import Database, HttpDatabase


DEFAULT_RETENTION_SECONDS = 24 * 60 * 60  # 24 hours


class ArtifactStore:
    """CRUD for _hivemind_query_artifacts records (BYTEA content)."""

    def __init__(self, db: Database | HttpDatabase):
        self.db = db

    def put(
        self,
        run_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict:
        """Store (or overwrite) an artifact for the given run.

        Returns {"run_id", "filename", "size_bytes", "created_at"}.
        """
        now = time.time()
        size = len(content)
        self.db.execute_commit(
            "INSERT INTO _hivemind_query_artifacts "
            "(run_id, filename, content, content_type, size_bytes, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (run_id, filename) DO UPDATE SET "
            "content = EXCLUDED.content, "
            "content_type = EXCLUDED.content_type, "
            "size_bytes = EXCLUDED.size_bytes, "
            "created_at = EXCLUDED.created_at",
            [run_id, filename, content, content_type, size, now],
        )
        return {
            "run_id": run_id,
            "filename": filename,
            "size_bytes": size,
            "created_at": now,
        }

    def get(self, run_id: str, filename: str) -> dict | None:
        """Fetch one artifact. Returns None if missing."""
        rows = self.db.execute(
            "SELECT run_id, filename, content, content_type, size_bytes, created_at "
            "FROM _hivemind_query_artifacts WHERE run_id = %s AND filename = %s",
            [run_id, filename],
        )
        return rows[0] if rows else None

    def list_for_run(self, run_id: str) -> list[dict]:
        """List artifact metadata for a run (no content)."""
        return self.db.execute(
            "SELECT filename, content_type, size_bytes, created_at "
            "FROM _hivemind_query_artifacts WHERE run_id = %s "
            "ORDER BY created_at ASC",
            [run_id],
        )

    def delete_expired(self, ttl_seconds: int = DEFAULT_RETENTION_SECONDS) -> int:
        """Delete artifacts older than ttl_seconds. Returns rows deleted."""
        cutoff = time.time() - ttl_seconds
        return self.db.execute_commit(
            "DELETE FROM _hivemind_query_artifacts WHERE created_at < %s",
            [cutoff],
        )

    def delete_for_run(self, run_id: str) -> int:
        """Delete every artifact tied to a run. Returns rows deleted."""
        return self.db.execute_commit(
            "DELETE FROM _hivemind_query_artifacts WHERE run_id = %s",
            [run_id],
        )

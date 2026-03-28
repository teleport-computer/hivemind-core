"""Run record storage for query agent executions.

Tracks the lifecycle of each query agent run: pending → running → completed/failed.
"""

from __future__ import annotations

import time

from ..db import Database


class RunStore:
    """CRUD for _hivemind_query_runs records."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, run_id: str, agent_id: str) -> dict:
        """Create a new run record with status=pending."""
        now = time.time()
        self.db.execute_commit(
            "INSERT INTO _hivemind_query_runs "
            "(run_id, agent_id, status, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            [run_id, agent_id, "pending", now, now],
        )
        return {
            "run_id": run_id,
            "agent_id": agent_id,
            "status": "pending",
            "s3_url": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }

    def update_status(
        self,
        run_id: str,
        status: str,
        *,
        s3_url: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Update run status. Returns True if a row was updated."""
        now = time.time()
        rowcount = self.db.execute_commit(
            "UPDATE _hivemind_query_runs "
            "SET status = %s, s3_url = COALESCE(%s, s3_url), "
            "error = COALESCE(%s, error), updated_at = %s "
            "WHERE run_id = %s",
            [status, s3_url, error, now, run_id],
        )
        return rowcount > 0

    def get(self, run_id: str) -> dict | None:
        """Get a run record by ID."""
        rows = self.db.execute(
            "SELECT run_id, agent_id, status, s3_url, error, "
            "created_at, updated_at "
            "FROM _hivemind_query_runs WHERE run_id = %s",
            [run_id],
        )
        return rows[0] if rows else None

    def list_by_agent(self, agent_id: str) -> list[dict]:
        """List runs for a given agent, most recent first."""
        return self.db.execute(
            "SELECT run_id, agent_id, status, s3_url, error, "
            "created_at, updated_at "
            "FROM _hivemind_query_runs WHERE agent_id = %s "
            "ORDER BY created_at DESC",
            [agent_id],
        )

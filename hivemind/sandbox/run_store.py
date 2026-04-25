"""Run record storage for query agent executions.

Tracks the lifecycle of each query agent run: pending → running → completed/failed.
Includes per-stage timing for scope, query, and mediator stages.
"""

from __future__ import annotations

import time

from ..db import Database

_COLUMNS = (
    "run_id, agent_id, status, error, "
    "created_at, updated_at, "
    "build_started_at, build_ended_at, "
    "scope_started_at, scope_ended_at, "
    "query_started_at, query_ended_at, "
    "mediator_started_at, mediator_ended_at, "
    "index_started_at, index_ended_at, index_output, "
    "scope_agent_id, index_agent_id, "
    "output"
)


class RunStore:
    """CRUD for _hivemind_query_runs records."""

    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        run_id: str,
        agent_id: str,
        *,
        scope_agent_id: str | None = None,
        index_agent_id: str | None = None,
    ) -> dict:
        """Create a new run record with status=pending."""
        now = time.time()
        self.db.execute_commit(
            "INSERT INTO _hivemind_query_runs "
            "(run_id, agent_id, scope_agent_id, index_agent_id, "
            "status, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [run_id, agent_id, scope_agent_id, index_agent_id, "pending", now, now],
        )
        return {
            "run_id": run_id,
            "agent_id": agent_id,
            "scope_agent_id": scope_agent_id,
            "index_agent_id": index_agent_id,
            "status": "pending",
            "error": None,
            "created_at": now,
            "updated_at": now,
        }

    def update_status(
        self,
        run_id: str,
        status: str,
        *,
        error: str | None = None,
        output: str | None = None,
    ) -> bool:
        """Update run status. Returns True if a row was updated."""
        now = time.time()
        rowcount = self.db.execute_commit(
            "UPDATE _hivemind_query_runs "
            "SET status = %s, "
            "error = COALESCE(%s, error), "
            "output = COALESCE(%s, output), "
            "updated_at = %s "
            "WHERE run_id = %s",
            [status, error, output, now, run_id],
        )
        return rowcount > 0

    def scrub_expired(self, ttl_seconds: int) -> int:
        """Null out output/error text on runs older than ttl_seconds.

        Run metadata (timings, status, agent IDs) stays so the API still
        reports that a run happened — we just stop holding the payload.
        """
        cutoff = time.time() - ttl_seconds
        return self.db.execute_commit(
            "UPDATE _hivemind_query_runs "
            "SET output = NULL, error = NULL, index_output = NULL "
            "WHERE updated_at < %s "
            "AND (output IS NOT NULL OR error IS NOT NULL "
            "OR index_output IS NOT NULL)",
            [cutoff],
        )

    def update_stage(
        self,
        run_id: str,
        stage: str,
        *,
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> bool:
        """Update timing for a pipeline stage (scope/query/mediator)."""
        if stage not in ("build", "scope", "query", "mediator", "index"):
            raise ValueError(f"Invalid stage: {stage}")
        now = time.time()
        sets = ["updated_at = %s"]
        params: list = [now]
        if started_at is not None:
            sets.append(f"{stage}_started_at = %s")
            params.append(started_at)
        if ended_at is not None:
            sets.append(f"{stage}_ended_at = %s")
            params.append(ended_at)
        params.append(run_id)
        rowcount = self.db.execute_commit(
            f"UPDATE _hivemind_query_runs SET {', '.join(sets)} "
            f"WHERE run_id = %s",
            params,
        )
        return rowcount > 0

    def update_index_output(self, run_id: str, index_output: str) -> bool:
        """Store index agent output text."""
        now = time.time()
        rowcount = self.db.execute_commit(
            "UPDATE _hivemind_query_runs "
            "SET index_output = %s, updated_at = %s WHERE run_id = %s",
            [index_output, now, run_id],
        )
        return rowcount > 0

    def get(self, run_id: str) -> dict | None:
        """Get a run record by ID."""
        rows = self.db.execute(
            f"SELECT {_COLUMNS} FROM _hivemind_query_runs WHERE run_id = %s",
            [run_id],
        )
        return rows[0] if rows else None

    def list_by_agent(self, agent_id: str) -> list[dict]:
        """List runs for a given agent, most recent first."""
        return self.db.execute(
            f"SELECT {_COLUMNS} FROM _hivemind_query_runs WHERE agent_id = %s "
            "ORDER BY created_at DESC",
            [agent_id],
        )

    def list_recent(self, limit: int = 20) -> list[dict]:
        """List recent runs across all agents."""
        return self.db.execute(
            f"SELECT {_COLUMNS} FROM _hivemind_query_runs "
            "ORDER BY created_at DESC LIMIT %s",
            [limit],
        )

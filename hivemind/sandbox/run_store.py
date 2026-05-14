"""Run record storage for query agent executions.

Tracks the lifecycle of each query agent run: pending → running → completed/failed.
Includes per-stage timing for scope, query, and mediator stages.
"""

from __future__ import annotations

import json
import time

from ..db import Database

_COLUMNS = (
    "run_id, agent_id, status, error, "
    "created_at, updated_at, "
    "build_started_at, build_ended_at, "
    "scope_started_at, scope_ended_at, "
    "query_started_at, query_ended_at, "
    "mediator_started_at, mediator_ended_at, "
    "room_id, room_manifest_hash, scope_agent_id, "
    "prompt, output, attestation, issuer_token_id, "
    "payer_tenant_id, payer_token_id, billable_role, "
    "billing_provider, billing_model, billing_hold_micro_usd, "
    "billing_cost_micro_usd, billing_status, billing_settled_at, usage_json, "
    "output_visibility, artifacts_enabled"
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
        issuer_token_id: str | None = None,
        room_id: str | None = None,
        room_manifest_hash: str | None = None,
        prompt: str | None = None,
        payer_tenant_id: str | None = None,
        payer_token_id: str | None = None,
        billable_role: str | None = None,
        billing_provider: str | None = None,
        billing_model: str | None = None,
        billing_hold_micro_usd: int = 0,
        billing_status: str = "unbilled",
        output_visibility: str = "owner_and_querier",
        artifacts_enabled: bool = True,
    ) -> dict:
        """Create a new run record with status=pending.

        ``issuer_token_id`` is the 12-hex prefix of the capability
        token that initiated this run (None for owner-initiated runs).
        Stored so A can audit "which hmq_ token did what" via
        ``GET /v1/runs?token_id=…``.
        """
        now = time.time()
        self.db.execute_commit(
            "INSERT INTO _hivemind_query_runs "
            "(run_id, agent_id, room_id, room_manifest_hash, "
            "scope_agent_id, issuer_token_id, "
            "prompt, payer_tenant_id, payer_token_id, billable_role, "
            "billing_provider, billing_model, billing_hold_micro_usd, "
            "billing_status, output_visibility, artifacts_enabled, status, "
            "created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s)",
            [
                run_id,
                agent_id,
                room_id,
                room_manifest_hash,
                scope_agent_id,
                issuer_token_id,
                prompt,
                payer_tenant_id,
                payer_token_id,
                billable_role,
                billing_provider,
                billing_model,
                int(billing_hold_micro_usd or 0),
                billing_status,
                output_visibility,
                bool(artifacts_enabled),
                "pending",
                now,
                now,
            ],
        )
        return {
            "run_id": run_id,
            "agent_id": agent_id,
            "room_id": room_id,
            "room_manifest_hash": room_manifest_hash,
            "scope_agent_id": scope_agent_id,
            "issuer_token_id": issuer_token_id,
            "prompt": prompt,
            "payer_tenant_id": payer_tenant_id,
            "payer_token_id": payer_token_id,
            "billable_role": billable_role,
            "billing_provider": billing_provider,
            "billing_model": billing_model,
            "billing_hold_micro_usd": int(billing_hold_micro_usd or 0),
            "billing_cost_micro_usd": 0,
            "billing_status": billing_status,
            "billing_settled_at": None,
            "usage_json": None,
            "output_visibility": output_visibility,
            "artifacts_enabled": bool(artifacts_enabled),
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
        attestation: dict | None = None,
    ) -> bool:
        """Update run status. Returns True if a row was updated.

        ``attestation`` (Phase 5) is the signed completion record:
        ``{"body": <signed-body>, "signature_b64": ..., "signer_pubkey_b64": ...}``
        — written when the pipeline finishes a run inside a CVM with
        an active KMS-derived run signer. ``COALESCE`` keeps the
        existing column when the caller doesn't pass one, so the
        in-flight ``update_status(run_id, "running")`` calls are
        unaffected.
        """
        now = time.time()
        attestation_json = (
            json.dumps(attestation, sort_keys=True, separators=(",", ":"))
            if attestation is not None
            else None
        )
        rowcount = self.db.execute_commit(
            "UPDATE _hivemind_query_runs "
            "SET status = %s, "
            "error = COALESCE(%s, error), "
            "output = COALESCE(%s, output), "
            "attestation = COALESCE(%s::jsonb, attestation), "
            "updated_at = %s "
            "WHERE run_id = %s",
            [
                status,
                error,
                output,
                attestation_json,
                now,
                run_id,
            ],
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
            "SET output = NULL, error = NULL "
            "WHERE updated_at < %s "
            "AND (output IS NOT NULL OR error IS NOT NULL)",
            [cutoff],
        )

    def update_usage(
        self,
        run_id: str,
        usage: dict | None,
        *,
        billing_cost_micro_usd: int | None = None,
        billing_status: str | None = None,
        billing_settled_at: float | None = None,
    ) -> bool:
        """Persist usage and optional billing settlement metadata."""
        now = time.time()
        if usage is not None:
            rows = self.db.execute(
                "SELECT usage_json FROM _hivemind_query_runs WHERE run_id = %s",
                [run_id],
            )
            existing = rows[0].get("usage_json") if rows else None
            if isinstance(existing, str):
                try:
                    existing = json.loads(existing)
                except ValueError:
                    existing = None
            if isinstance(existing, dict):
                usage = self._merge_usage(existing, usage)
        usage_json = (
            json.dumps(usage, sort_keys=True, separators=(",", ":"))
            if usage is not None
            else None
        )
        rowcount = self.db.execute_commit(
            "UPDATE _hivemind_query_runs "
            "SET usage_json = COALESCE(%s::jsonb, usage_json), "
            "billing_cost_micro_usd = COALESCE(%s, billing_cost_micro_usd), "
            "billing_status = COALESCE(%s, billing_status), "
            "billing_settled_at = COALESCE(%s, billing_settled_at), "
            "updated_at = %s "
            "WHERE run_id = %s",
            [
                usage_json,
                billing_cost_micro_usd,
                billing_status,
                billing_settled_at,
                now,
                run_id,
            ],
        )
        return rowcount > 0

    @staticmethod
    def _merge_usage(existing: dict, new: dict) -> dict:
        stages: dict = {}
        for src in (existing.get("stages"), new.get("stages")):
            if isinstance(src, dict):
                stages.update(
                    {k: v for k, v in src.items() if isinstance(v, dict)}
                )
        merged = dict(existing)
        merged.update(new)
        debug_trace: list = []
        for src in (existing.get("debug_trace"), new.get("debug_trace")):
            if isinstance(src, list):
                debug_trace.extend(entry for entry in src if isinstance(entry, dict))
        if debug_trace:
            merged["debug_trace"] = debug_trace
        if not stages:
            return merged
        merged["stages"] = stages
        for key in ("calls", "prompt_tokens", "completion_tokens", "total_tokens"):
            merged[key] = sum(int(s.get(key) or 0) for s in stages.values())
        merged["max_tokens"] = max(
            int(existing.get("max_tokens") or 0),
            int(new.get("max_tokens") or 0),
        )
        return merged

    def update_stage(
        self,
        run_id: str,
        stage: str,
        *,
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> bool:
        """Update timing for a pipeline stage (scope/query/mediator)."""
        if stage not in ("build", "scope", "query", "mediator"):
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

    def list_by_token(
        self, issuer_token_id: str, limit: int = 50,
    ) -> list[dict]:
        """List runs initiated by a given capability token, newest first.

        Used by the owner-side audit endpoint
        (``GET /v1/runs?token_id=…``) so A can see what each
        ``hmq_`` token they minted has actually done. The token_id is
        the 12-hex prefix from ``_capability_tokens``.
        """
        return self.db.execute(
            f"SELECT {_COLUMNS} FROM _hivemind_query_runs "
            "WHERE issuer_token_id = %s "
            "ORDER BY created_at DESC LIMIT %s",
            [issuer_token_id, limit],
        )

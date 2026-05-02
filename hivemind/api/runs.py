"""Run status, listing, and artifact routes."""

from __future__ import annotations

import asyncio
import email.utils
from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response

from ..tenants import Caller


def _caller_can_access_run(caller: Caller, run: dict) -> bool:
    """Owner sees tenant runs; query tokens see only their own runs."""
    if caller.role == "owner":
        return True
    return bool(caller.token_id) and run.get("issuer_token_id") == caller.token_id


def _caller_can_access_run_payload(caller: Caller, run: dict) -> bool:
    """Whether caller may see output text and artifacts for a run."""
    if not _caller_can_access_run(caller, run):
        return False
    visibility = (run.get("output_visibility") or "owner_and_querier").strip()
    if (
        caller.role == "owner"
        and run.get("room_id")
        and run.get("issuer_token_id")
        and visibility == "querier_only"
    ):
        return False
    return True


def _redact_run_payload_for_caller(caller: Caller, run: dict) -> dict:
    if _caller_can_access_run_payload(caller, run):
        return run
    redacted = dict(run)
    redacted["output"] = None
    redacted["error"] = None
    redacted["artifacts"] = []
    redacted["payload_redacted"] = True
    return redacted


def register_run_routes(app: FastAPI, requires_role: Callable[..., Callable]) -> None:
    """Register run read and artifact endpoints."""

    @app.get("/v1/runs/{run_id}")
    async def get_agent_run(
        run_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        """Get the status and result of an agent run."""
        hm = caller.hive
        run = await asyncio.to_thread(hm.run_store.get, run_id)
        if not run or not _caller_can_access_run(caller, run):
            raise HTTPException(404, "Run not found")
        if _caller_can_access_run_payload(caller, run) and run.get(
            "artifacts_enabled", True
        ):
            run["artifacts"] = await asyncio.to_thread(
                hm.artifact_store.list_for_run, run_id
            )
        else:
            run["artifacts"] = []
        run["artifact_retention_seconds"] = hm.settings.artifact_retention_seconds
        run = _redact_run_payload_for_caller(caller, run)
        return JSONResponse(
            content=run,
            headers={"Cache-Control": "no-cache, no-store"},
        )

    @app.get("/v1/runs")
    async def list_agent_runs(
        limit: int = 20,
        token_id: str | None = None,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        """List recent agent runs."""
        capped = min(limit, 100)
        if token_id:
            tid = token_id.strip().lower()
            if caller.role != "owner":
                raise HTTPException(
                    status_code=403,
                    detail="token_id filter is owner-only",
                )
            if not tid or len(tid) < 6:
                raise HTTPException(
                    status_code=400,
                    detail="token_id must be at least 6 hex chars",
                )
            rows = await asyncio.to_thread(
                caller.hive.run_store.list_by_token, tid, capped,
            )
            return [_redact_run_payload_for_caller(caller, r) for r in rows]
        if caller.role == "query":
            rows = await asyncio.to_thread(
                caller.hive.run_store.list_by_token, caller.token_id, capped,
            )
            return [_redact_run_payload_for_caller(caller, r) for r in rows]
        rows = await asyncio.to_thread(
            caller.hive.run_store.list_recent, capped,
        )
        return [_redact_run_payload_for_caller(caller, r) for r in rows]

    @app.get(
        "/v1/runs/{run_id}/artifacts/{filename:path}",
    )
    async def get_run_artifact(
        run_id: str,
        filename: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        hm = caller.hive
        run = await asyncio.to_thread(hm.run_store.get, run_id)
        if (
            not run
            or not _caller_can_access_run_payload(caller, run)
            or not run.get("artifacts_enabled", True)
        ):
            raise HTTPException(404, "Artifact not found or expired")
        from ..sandbox.models import validate_artifact_filename

        try:
            safe_filename = validate_artifact_filename(filename)
        except ValueError:
            raise HTTPException(400, "Invalid artifact filename")
        artifact = await asyncio.to_thread(
            hm.artifact_store.get, run_id, safe_filename
        )
        if not artifact:
            raise HTTPException(404, "Artifact not found or expired")
        ttl = hm.settings.artifact_retention_seconds
        expires_at = float(artifact["created_at"]) + ttl
        content_type = artifact["content_type"] or "application/octet-stream"
        if len(content_type) > 100 or any(
            ord(ch) < 32 or ord(ch) == 127 for ch in content_type
        ):
            content_type = "application/octet-stream"
        return Response(
            content=bytes(artifact["content"]),
            media_type=content_type,
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Retention-Seconds": str(ttl),
                "Expires": email.utils.formatdate(expires_at, usegmt=True),
                "Content-Disposition": (
                    f'attachment; filename="{safe_filename}"; '
                    f"filename*=UTF-8''{quote(safe_filename)}"
                ),
            },
        )

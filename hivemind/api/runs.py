"""Run status, listing, and artifact routes."""

from __future__ import annotations

import asyncio
import email.utils
import json
from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ..tenants import Caller


def _coerce_usage(usage_json) -> dict:
    if isinstance(usage_json, dict):
        return usage_json
    if isinstance(usage_json, str) and usage_json.strip():
        try:
            data = json.loads(usage_json)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _with_run_telemetry_summary(run: dict) -> dict:
    """Add non-sensitive, client-friendly telemetry fields to a run row."""
    out = dict(run)
    usage = _coerce_usage(out.get("usage_json"))
    if usage:
        out["usage"] = usage
    stages = usage.get("stages", {})
    if not isinstance(stages, dict):
        stages = {}
    scope_stage = stages.get("scope", {})
    if not isinstance(scope_stage, dict):
        scope_stage = {}

    evidence: dict = {}
    if mode := scope_stage.get("scope_mode"):
        evidence["mode"] = mode
        out["scope_mode"] = mode
    if reason := scope_stage.get("scope_mode_reason"):
        evidence["reason"] = reason
        out["scope_mode_reason"] = reason
    if inspection := scope_stage.get("query_inspection_mode"):
        evidence["query_inspection_mode"] = inspection
        out["query_inspection_mode"] = inspection
    bridge = scope_stage.get("bridge")
    if isinstance(bridge, dict):
        if isinstance(bridge.get("tool_call_counts"), dict):
            evidence["tool_call_counts"] = bridge["tool_call_counts"]
        if isinstance(bridge.get("llm_tool_call_counts"), dict):
            evidence["llm_tool_call_counts"] = bridge["llm_tool_call_counts"]
    if evidence:
        out["scope_evidence"] = evidence
    return out


def _caller_can_access_run(
    caller: Caller,
    run: dict,
    *,
    asker_tenant_id: str | None = None,
) -> bool:
    """Owner sees tenant runs; query tokens see only their own runs.

    Share-link askers see only runs they paid for in their bound room.
    ``asker_tenant_id`` is the tenant resolved from
    ``X-Hivemind-Api-Key`` on the read request — the caller must
    re-prove identity each read so a single share token can't leak
    runs across askers. Pass ``None`` if the header was absent and the
    handler should refuse access.
    """
    if caller.role == "owner":
        return True
    if caller.role == "share":
        bound = (caller.constraints.get("room_id") or "").strip()
        if not bound or run.get("room_id") != bound:
            return False
        if not asker_tenant_id:
            return False
        return run.get("payer_tenant_id") == asker_tenant_id
    return bool(caller.token_id) and run.get("issuer_token_id") == caller.token_id


def _caller_can_access_run_payload(
    caller: Caller,
    run: dict,
    *,
    asker_tenant_id: str | None = None,
) -> bool:
    """Whether caller may see output text and artifacts for a run."""
    if not _caller_can_access_run(
        caller, run, asker_tenant_id=asker_tenant_id,
    ):
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


def _redact_run_payload_for_caller(
    caller: Caller,
    run: dict,
    *,
    asker_tenant_id: str | None = None,
) -> dict:
    if _caller_can_access_run_payload(
        caller, run, asker_tenant_id=asker_tenant_id,
    ):
        return run
    redacted = dict(run)
    redacted["output"] = None
    redacted["error"] = None
    redacted["artifacts"] = []
    redacted["payload_redacted"] = True
    return redacted


def _asker_tenant_for_share(caller: Caller, request) -> str | None:
    """For role='share', resolve X-Hivemind-Api-Key → tenant_id.

    Returns ``None`` if the header is absent or the key is invalid; the
    run-read handler treats ``None`` as forbidden (rather than 401) so
    leaking "this run exists but you can't see it" doesn't happen.
    """
    if caller.role != "share":
        return None
    payer_key = (
        request.headers.get("X-Hivemind-Api-Key")
        or request.headers.get("X-Hivemind-Payer-Key")
        or ""
    ).strip()
    if not payer_key:
        return None
    registry = request.app.state.registry
    payer = registry.resolve_payer_key(payer_key)
    if payer is None:
        return None
    return payer.get("tenant_id")


def register_run_routes(app: FastAPI, requires_role: Callable[..., Callable]) -> None:
    """Register run read and artifact endpoints."""

    @app.get("/v1/runs/{run_id}")
    async def get_agent_run(
        run_id: str,
        request: Request,
        caller: Caller = Depends(requires_role("owner", "query", "share")),
    ):
        """Get the status and result of an agent run."""
        hm = caller.hive
        run = await asyncio.to_thread(hm.run_store.get, run_id)
        asker = _asker_tenant_for_share(caller, request)
        if not run or not _caller_can_access_run(
            caller, run, asker_tenant_id=asker,
        ):
            raise HTTPException(404, "Run not found")
        run = _with_run_telemetry_summary(run)
        if _caller_can_access_run_payload(
            caller, run, asker_tenant_id=asker,
        ) and run.get("artifacts_enabled", True):
            run["artifacts"] = await asyncio.to_thread(
                hm.artifact_store.list_for_run, run_id
            )
        else:
            run["artifacts"] = []
        run["artifact_retention_seconds"] = hm.settings.artifact_retention_seconds
        run = _redact_run_payload_for_caller(
            caller, run, asker_tenant_id=asker,
        )
        return JSONResponse(
            content=run,
            headers={"Cache-Control": "no-cache, no-store"},
        )

    @app.get("/v1/runs")
    async def list_agent_runs(
        request: Request,
        limit: int = 20,
        token_id: str | None = None,
        caller: Caller = Depends(requires_role("owner", "query", "share")),
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
            return [
                _redact_run_payload_for_caller(
                    caller, _with_run_telemetry_summary(r)
                )
                for r in rows
            ]
        if caller.role == "share":
            asker = _asker_tenant_for_share(caller, request)
            if not asker:
                # No identity → return empty rather than 401, so the
                # absence of X-Hivemind-Api-Key on the share-link path
                # is a soft-fail UX: callers see "no runs yet" until
                # they wire their own hmk_ key.
                return []
            bound = (caller.constraints.get("room_id") or "").strip()
            rows = await asyncio.to_thread(
                caller.hive.run_store.list_recent, capped,
            )
            rows = [
                r for r in rows
                if r.get("room_id") == bound
                and r.get("payer_tenant_id") == asker
            ]
            return [
                _redact_run_payload_for_caller(
                    caller,
                    _with_run_telemetry_summary(r),
                    asker_tenant_id=asker,
                )
                for r in rows
            ]
        if caller.role == "query":
            rows = await asyncio.to_thread(
                caller.hive.run_store.list_by_token, caller.token_id, capped,
            )
            return [
                _redact_run_payload_for_caller(
                    caller, _with_run_telemetry_summary(r)
                )
                for r in rows
            ]
        rows = await asyncio.to_thread(
            caller.hive.run_store.list_recent, capped,
        )
        return [
            _redact_run_payload_for_caller(caller, _with_run_telemetry_summary(r))
            for r in rows
        ]

    @app.get(
        "/v1/runs/{run_id}/artifacts/{filename:path}",
    )
    async def get_run_artifact(
        run_id: str,
        filename: str,
        request: Request,
        caller: Caller = Depends(requires_role("owner", "query", "share")),
    ):
        hm = caller.hive
        run = await asyncio.to_thread(hm.run_store.get, run_id)
        asker = _asker_tenant_for_share(caller, request)
        if (
            not run
            or not _caller_can_access_run_payload(
                caller, run, asker_tenant_id=asker,
            )
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

"""Admin run-telemetry routes — cross-tenant aggregated view of runs.

Owner-side ``/v1/runs`` only sees the active tenant's runs. The operator
needs a global view: every tenant's runs joined with room name and tenant
name so they can spot pricing_missing rows, blocked outputs, slow
mediators, etc. without round-tripping through every tenant API key.

The data is read directly from each tenant's ``_hivemind_query_runs``
table via ``registry.for_tenant(...)`` (the same LRU-cached path admin
billing uses). Suspended tenants are skipped.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request

from ..tenants import TenantRegistry

logger = logging.getLogger(__name__)


def _registry(request: Request) -> TenantRegistry:
    return request.app.state.registry


def _coerce_usage(usage_json: Any) -> dict:
    if usage_json is None:
        return {}
    if isinstance(usage_json, dict):
        return usage_json
    if isinstance(usage_json, str):
        try:
            return json.loads(usage_json) or {}
        except (ValueError, TypeError):
            return {}
    return {}


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _enrich_run(
    run: dict,
    tenant_id: str,
    tenant_name: str,
    room_name_by_id: dict[str, str],
) -> dict:
    """Add tenant + room metadata + usage decoding without mutating the input."""
    out = dict(run)
    out["tenant_id"] = tenant_id
    out["tenant_name"] = tenant_name
    if out.get("room_id"):
        out["room_name"] = room_name_by_id.get(out["room_id"]) or ""
    usage = _coerce_usage(out.get("usage_json"))
    out["usage"] = usage
    out["llm_calls"] = _int_value(usage.get("calls") or usage.get("llm_calls"))
    out["prompt_tokens"] = _int_value(usage.get("prompt_tokens"))
    out["completion_tokens"] = _int_value(usage.get("completion_tokens"))
    out["total_tokens"] = _int_value(usage.get("total_tokens")) or (
        out["prompt_tokens"] + out["completion_tokens"]
    )
    # Drop the raw json blob from the wire response to keep it compact.
    out.pop("usage_json", None)
    return out


def _gather_room_names(hm: Any) -> dict[str, str]:
    """Return {room_id: name} for all rooms in this tenant.

    Best-effort: failures fall back to an empty mapping so an unhealthy
    room store doesn't take down the global runs listing.
    """
    try:
        rows = hm.db.execute(
            "SELECT room_id, name FROM _hivemind_rooms"
        )
        return {r["room_id"]: r.get("name") or "" for r in rows}
    except Exception as e:
        logger.warning("admin_runs: failed to fetch rooms for tenant: %s", e)
        return {}


def register_admin_run_routes(
    app: FastAPI,
    check_admin: Callable,
) -> None:
    """Register ``/v1/admin/runs`` (list) and ``/v1/admin/runs/{run_id}``."""

    @app.get("/v1/admin/runs", dependencies=[Depends(check_admin)])
    async def admin_list_runs(
        request: Request,
        limit: int = 200,
        per_tenant: int = 100,
        tenant_id: str | None = None,
        status: str | None = None,
        billing_status: str | None = None,
    ):
        """Cross-tenant run listing.

        Iterates each tenant (or only the requested one), pulls the most
        recent ``per_tenant`` runs from its ``_hivemind_query_runs`` table,
        joins with ``_hivemind_rooms`` for the room name, then merges and
        sorts by ``created_at`` descending. Filters apply after merge.
        """
        clean_limit = max(1, min(1000, int(limit or 200)))
        clean_per = max(1, min(500, int(per_tenant or 100)))
        registry = _registry(request)

        tenants = await asyncio.to_thread(registry.list_tenants)
        if tenant_id:
            tenants = [t for t in tenants if t.get("id") == tenant_id]

        merged: list[dict] = []
        for t in tenants:
            if t.get("suspended"):
                continue
            tid = t.get("id") or ""
            tname = t.get("name") or ""
            try:
                hm = await asyncio.to_thread(registry.for_tenant, tid)
                if hm is None:
                    continue
                runs = await asyncio.to_thread(hm.run_store.list_recent, clean_per)
                room_names = await asyncio.to_thread(_gather_room_names, hm)
            except Exception as e:
                logger.warning(
                    "admin_runs: failed for tenant %s: %s", tid, e
                )
                continue
            for r in runs:
                merged.append(_enrich_run(r, tid, tname, room_names))

        if status:
            merged = [r for r in merged if r.get("status") == status]
        if billing_status:
            merged = [r for r in merged if r.get("billing_status") == billing_status]

        merged.sort(
            key=lambda r: float(r.get("created_at") or 0),
            reverse=True,
        )
        return {"runs": merged[:clean_limit]}

    @app.get(
        "/v1/admin/runs/{run_id}",
        dependencies=[Depends(check_admin)],
    )
    async def admin_get_run(run_id: str, request: Request):
        """Look up a single run by id across all tenants.

        Iterates tenants until a match is found. Slower than the per-tenant
        endpoint, but the operator usually arrives here from the listing
        which already knows the tenant — most callers should pass
        ``tenant_id`` to scope the search via ``GET /v1/admin/runs?tenant_id=…``
        and then pick the matching row.
        """
        registry = _registry(request)
        tenants = await asyncio.to_thread(registry.list_tenants)
        for t in tenants:
            if t.get("suspended"):
                continue
            tid = t.get("id") or ""
            tname = t.get("name") or ""
            try:
                hm = await asyncio.to_thread(registry.for_tenant, tid)
                if hm is None:
                    continue
                run = await asyncio.to_thread(hm.run_store.get, run_id)
                if run is None:
                    continue
                room_names = await asyncio.to_thread(_gather_room_names, hm)
                return _enrich_run(run, tid, tname, room_names)
            except Exception as e:
                logger.warning(
                    "admin_runs(get): failed for tenant %s: %s", tid, e
                )
                continue
        raise HTTPException(404, f"run '{run_id}' not found in any tenant")

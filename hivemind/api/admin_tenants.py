"""Admin tenant-management API routes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from ..config import Settings
from ..sandbox.settings import build_sandbox_settings
from ..tenants import DuplicateTenantNameError, TenantRegistry

logger = logging.getLogger(__name__)


class AdminCreateTenantRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: Any = ""
    allow_duplicate_name: bool = False


class AdminRegisterTenantRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: Any = ""
    db_name: Any = ""
    api_key: Any = None
    tenant_id: Any = None
    allow_duplicate_name: bool = False


class AdminRenameDatabaseRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    old_name: Any = ""
    new_name: Any = ""


class AdminResetTenantKeyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    clear_seal: bool = False
    revoke_capabilities: bool = False


def _registry(request: Request) -> TenantRegistry:
    return request.app.state.registry


def register_admin_tenant_routes(
    app: FastAPI,
    settings: Settings,
    check_admin: Callable,
) -> None:
    """Register admin tenant CRUD and maintenance routes."""

    @app.post("/v1/admin/tenants", dependencies=[Depends(check_admin)])
    async def admin_create_tenant(
        payload: AdminCreateTenantRequest,
        request: Request,
    ):
        name = str(payload.name or "")
        if not name.strip():
            raise HTTPException(400, "'name' required")
        registry = _registry(request)
        try:
            result = await asyncio.to_thread(
                registry.provision,
                name,
                allow_duplicate_name=bool(payload.allow_duplicate_name),
            )
        except DuplicateTenantNameError as e:
            raise HTTPException(409, str(e))
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return result

    @app.post(
        "/v1/admin/tenants/register",
        dependencies=[Depends(check_admin)],
    )
    async def admin_register_existing(
        payload: AdminRegisterTenantRequest,
        request: Request,
    ):
        """Adopt a pre-populated Postgres database as a tenant."""
        registry = _registry(request)
        try:
            result = await asyncio.to_thread(
                registry.register_existing,
                str(payload.name or ""),
                str(payload.db_name or ""),
                payload.api_key or None,
                payload.tenant_id or None,
                allow_duplicate_name=bool(payload.allow_duplicate_name),
            )
        except DuplicateTenantNameError as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return result

    @app.post(
        "/v1/admin/rename-database",
        dependencies=[Depends(check_admin)],
    )
    async def admin_rename_database(
        payload: AdminRenameDatabaseRequest,
        request: Request,
    ):
        old_name = str(payload.old_name or "")
        new_name = str(payload.new_name or "")
        if not old_name or not new_name:
            raise HTTPException(400, "'old_name' and 'new_name' required")
        registry = _registry(request)
        admin = registry._pg_admin
        if admin is None:
            raise HTTPException(
                503,
                "rename requires HIVEMIND_SQL_PROXY_ADMIN_KEY (HTTP deploys)",
            )
        try:
            await asyncio.to_thread(admin.rename_database, old_name, new_name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        return {"status": "ok", "old_name": old_name, "new_name": new_name}

    @app.get("/v1/admin/tenants", dependencies=[Depends(check_admin)])
    async def admin_list_tenants(request: Request):
        registry = _registry(request)
        tenants = await asyncio.to_thread(registry.list_tenants)
        return {"tenants": tenants}

    @app.post(
        "/v1/admin/migrate-to-roles",
        dependencies=[Depends(check_admin)],
    )
    async def admin_migrate_to_roles(request: Request):
        registry = _registry(request)
        admin = registry._pg_admin
        if admin is None:
            raise HTTPException(
                503,
                "migrate-to-roles requires HIVEMIND_SQL_PROXY_ADMIN_KEY "
                "(HTTP deploys)",
            )
        try:
            results = await asyncio.to_thread(admin.migrate_tenants_to_roles)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        return {"status": "ok", "results": results}

    @app.post(
        "/v1/admin/tenants/{tenant_id}/reset-key",
        dependencies=[Depends(check_admin)],
    )
    async def admin_reset_tenant_key(
        tenant_id: str,
        payload: AdminResetTenantKeyRequest,
        request: Request,
    ):
        registry = _registry(request)
        try:
            return await asyncio.to_thread(
                registry.admin_reset_tenant_key,
                tenant_id,
                clear_seal=bool(payload.clear_seal),
                revoke_capabilities=bool(payload.revoke_capabilities),
            )
        except KeyError as e:
            raise HTTPException(404, str(e))
        except RuntimeError as e:
            raise HTTPException(400, str(e))

    @app.delete(
        "/v1/admin/tenants/{tenant_id}",
        dependencies=[Depends(check_admin)],
    )
    async def admin_delete_tenant(tenant_id: str, request: Request):
        registry = _registry(request)
        try:
            await asyncio.to_thread(registry.delete, tenant_id)
        except KeyError:
            raise HTTPException(404, f"tenant '{tenant_id}' not found")
        return {"status": "ok", "tenant_id": tenant_id}

    @app.post(
        "/v1/admin/agents/sweep-broken",
        dependencies=[Depends(check_admin)],
    )
    async def admin_sweep_broken_agents(request: Request, dry_run: bool = False):
        """Find agents whose images are missing from the runtime."""
        from ..sandbox.backend import _create_runner

        sandbox_settings = build_sandbox_settings(settings)
        runner = _create_runner(sandbox_settings)
        registry = _registry(request)

        orphans: list[dict] = []
        for tenant in await asyncio.to_thread(registry.list_tenants):
            if tenant.get("suspended"):
                continue
            tenant_id = tenant["id"]
            try:
                hm = await asyncio.to_thread(registry.for_tenant, tenant_id)
                if hm is None:
                    continue
                agents = await asyncio.to_thread(hm.agent_store.list_agents)
                for ag in agents:
                    try:
                        present = await asyncio.to_thread(
                            runner.image_exists, ag.image
                        )
                    except Exception as e:
                        logger.warning(
                            "image_exists(%s) raised: %s — skipping",
                            ag.image,
                            e,
                        )
                        continue
                    if present:
                        continue
                    record = {
                        "tenant_id": tenant_id,
                        "tenant_name": tenant.get("name"),
                        "agent_id": ag.agent_id,
                        "name": ag.name,
                        "agent_type": ag.agent_type,
                        "image": ag.image,
                        "deleted": False,
                    }
                    if not dry_run:
                        try:
                            await asyncio.to_thread(
                                hm.agent_store.delete, ag.agent_id
                            )
                            record["deleted"] = True
                        except Exception as e:
                            logger.warning(
                                "delete(%s) raised: %s",
                                ag.agent_id,
                                e,
                            )
                            record["error"] = str(e)
                    orphans.append(record)
            except Exception as e:
                logger.warning(
                    "sweep-broken: tenant %s skipped: %s", tenant_id, e
                )

        return {
            "dry_run": dry_run,
            "count": len(orphans),
            "deleted": sum(1 for o in orphans if o.get("deleted")),
            "orphans": orphans,
        }

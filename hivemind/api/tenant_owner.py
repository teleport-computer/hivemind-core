"""Tenant owner, identity, token, and compose-pin routes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from cryptography.hazmat.primitives import serialization
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import ValidationError

from ..compose_pin import ComposePin
from ..core import Hivemind
from ..tenant_signing import derive_signing_keypair
from ..tenants import Caller, TenantRegistry


def _registry(request: Request) -> TenantRegistry:
    return request.app.state.registry


def register_tenant_owner_routes(
    app: FastAPI,
    bearer: Callable[[Request], str],
    requires_role: Callable[..., Callable],
    get_tenant_hive: Callable,
) -> None:
    """Register tenant owner/control routes."""

    @app.post("/v1/tenant/rotate-key")
    async def tenant_rotate_key(
        request: Request,
        _hm: Hivemind = Depends(get_tenant_hive),
    ):
        tenant_id = request.state.tenant_id
        registry = _registry(request)
        try:
            result = await asyncio.to_thread(registry.rotate_key, tenant_id)
        except KeyError:
            raise HTTPException(404, f"tenant '{tenant_id}' not found")
        return result

    @app.get("/v1/tenant/tokens")
    async def list_tokens(
        request: Request,
        _hm: Hivemind = Depends(get_tenant_hive),
    ):
        registry = _registry(request)
        tenant_id = request.state.tenant_id
        rows = await asyncio.to_thread(registry.list_capabilities, tenant_id)
        return {"tokens": rows}

    @app.delete("/v1/tenant/tokens/{token_id}")
    async def revoke_token(
        token_id: str,
        request: Request,
        _hm: Hivemind = Depends(get_tenant_hive),
    ):
        registry = _registry(request)
        tenant_id = request.state.tenant_id
        try:
            ok = await asyncio.to_thread(
                registry.revoke_capability, tenant_id, token_id
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not ok:
            raise HTTPException(404, f"token '{token_id}' not found")
        return {"status": "ok", "token_id": token_id}

    @app.post("/v1/tenants/compose-pin")
    async def submit_compose_pin(
        request: Request,
        payload: dict,
        caller: Caller = Depends(requires_role("owner")),
    ):
        envelope_dict = (payload or {}).get("envelope")
        if not isinstance(envelope_dict, dict):
            raise HTTPException(
                400, "body must be {\"envelope\": {<ComposePin fields>}}"
            )
        try:
            pin = ComposePin.model_validate(envelope_dict)
        except ValidationError as e:
            raise HTTPException(400, f"envelope: {e}")
        if pin.tenant_id != caller.tenant_id:
            raise HTTPException(
                400,
                f"envelope.tenant_id ({pin.tenant_id}) does not match "
                f"caller tenant ({caller.tenant_id})",
            )
        owner_bearer = bearer(request)
        _priv, expected_pub = derive_signing_keypair(owner_bearer, caller.tenant_id)
        expected_pub_bytes = expected_pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        if not pin.verify(expected_pubkey=expected_pub_bytes):
            raise HTTPException(
                400,
                "envelope signature invalid (signer_pubkey must match the "
                "pubkey derived from your hmk_ + tenant_id)",
            )
        if pin.is_expired():
            raise HTTPException(400, "envelope already expired")
        envelope_json = pin.to_json()
        registry = _registry(request)
        try:
            result = await asyncio.to_thread(
                registry.store_compose_pin,
                caller.tenant_id,
                envelope_json,
                pin.signer_pubkey,
            )
        except KeyError as e:
            raise HTTPException(404, str(e))
        return {**result, "envelope": pin.model_dump()}

    @app.get("/v1/tenants/compose-pin")
    async def latest_compose_pin(
        request: Request,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        registry = _registry(request)
        row = await asyncio.to_thread(
            registry.latest_compose_pin, caller.tenant_id
        )
        if not row:
            raise HTTPException(404, "no compose pin")

        pin = ComposePin.from_json(row["envelope"])
        return {
            "pin_id": row["pin_id"],
            "tenant_id": caller.tenant_id,
            "envelope": pin.model_dump(),
            "created_at": row["created_at"],
            "revoked_at": row["revoked_at"],
        }

    @app.get("/v1/tenants/compose-pin/list")
    async def list_compose_pins(
        request: Request,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        registry = _registry(request)
        rows = await asyncio.to_thread(
            registry.list_compose_pins, caller.tenant_id
        )

        out: list[dict] = []
        for r in rows:
            try:
                env = ComposePin.from_json(r["envelope"]).model_dump()
            except Exception:
                env = None
            out.append(
                {
                    "pin_id": r["pin_id"],
                    "envelope": env,
                    "created_at": r["created_at"],
                    "revoked_at": r["revoked_at"],
                }
            )
        return {"pins": out}

    @app.delete("/v1/tenants/compose-pin/{pin_id}")
    async def revoke_compose_pin(
        pin_id: str,
        request: Request,
        caller: Caller = Depends(requires_role("owner")),
    ):
        registry = _registry(request)
        ok = await asyncio.to_thread(
            registry.revoke_compose_pin, caller.tenant_id, pin_id
        )
        if not ok:
            raise HTTPException(404, f"pin '{pin_id}' not found")
        return {"status": "ok", "pin_id": pin_id}

    @app.get("/v1/whoami")
    async def whoami(
        request: Request,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        # Expose the tenant display name so UIs can render "Welcome back,
        # watch-history" instead of "Welcome back, t_…". Looked up from the
        # control plane registry (same source of truth admin endpoints use).
        tenant_name = ""
        try:
            registry = request.app.state.registry
            row = await asyncio.to_thread(
                registry.get_by_id, caller.tenant_id,
            )
            if row:
                tenant_name = row.get("name") or ""
        except Exception:
            tenant_name = ""
        return {
            "tenant_id": caller.tenant_id,
            "name": tenant_name,
            "role": caller.role,
            "constraints": caller.constraints,
            "token_id": caller.token_id,
            "sealed": caller.sealed,
        }

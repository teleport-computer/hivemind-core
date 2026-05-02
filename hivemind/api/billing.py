"""Billing and credit-code API routes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from ..config import Settings
from ..tenants import Caller, TenantRegistry

logger = logging.getLogger(__name__)


class RedeemCreditCodeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    credit_code: str | None = None
    code: str | None = None

    def normalized_code(self) -> str:
        return str(self.credit_code or self.code or "").strip()


class AdminCreditCodeCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    credit_usd: Any = "0.00"
    max_redemptions: Any = None
    uses: Any = None
    expires_at: Any = None
    expires_in_seconds: Any = None
    label: str = ""


class AdminBillingPriceRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str = ""
    model: str = ""
    prompt_usd_per_million: Any = None
    completion_usd_per_million: Any = None
    source: str = "admin"


class AdminCreditGrantRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    amount_usd: Any = None
    note: str = ""


def _registry(request: Request) -> TenantRegistry:
    return request.app.state.registry


async def redeem_credit_code_for_tenant(
    registry: TenantRegistry,
    *,
    tenant_id: str,
    credit_code: str,
) -> dict:
    if not credit_code:
        raise HTTPException(400, "'credit_code' required")
    try:
        redemption = await asyncio.to_thread(
            registry.redeem_credit_code,
            credit_code,
            tenant_id,
        )
    except ValueError as e:
        raise HTTPException(403, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))

    credit_micro_usd = int(redemption.get("credit_micro_usd") or 0)
    if credit_micro_usd <= 0:
        balance = await asyncio.to_thread(
            registry.billing_balance_micro_usd,
            tenant_id,
        )
        return {
            "code_id": redemption.get("code_id"),
            "redemption_id": redemption.get("redemption_id"),
            "credit_micro_usd": 0,
            "balance_micro_usd": balance,
        }
    try:
        grant = await asyncio.to_thread(
            registry.billing_grant_credit_micro,
            tenant_id,
            credit_micro_usd,
            note="credit code",
            actor="credit_code",
            metadata={
                "code_id": redemption.get("code_id"),
                "redemption_id": redemption.get("redemption_id"),
            },
        )
    except Exception as e:
        try:
            await asyncio.to_thread(
                registry.release_credit_code,
                redemption["code_id"],
                tenant_id,
            )
        except Exception as release_err:
            logger.error(
                "Failed to release credit code %s after grant failure: %s",
                redemption.get("code_id"),
                release_err,
            )
        logger.error("Credit code grant failed: %s", e)
        raise HTTPException(500, "credit code grant failed")
    return {
        "code_id": redemption.get("code_id"),
        "redemption_id": redemption.get("redemption_id"),
        "credit_micro_usd": credit_micro_usd,
        "balance_micro_usd": int(grant.get("balance_micro_usd") or 0),
    }


def register_owner_billing_routes(
    app: FastAPI,
    requires_role: Callable[..., Callable],
) -> None:
    """Register tenant-owner billing routes."""

    @app.get("/v1/billing")
    async def billing_account(
        request: Request,
        caller: Caller = Depends(requires_role("owner")),
        limit: int = 25,
    ):
        registry = _registry(request)
        return await asyncio.to_thread(
            registry.billing_account,
            caller.tenant_id,
            limit=limit,
        )

    @app.post("/v1/billing/credit-codes/redeem")
    async def redeem_credit_code(
        payload: RedeemCreditCodeRequest,
        request: Request,
        caller: Caller = Depends(requires_role("owner")),
    ):
        registry = _registry(request)
        return await redeem_credit_code_for_tenant(
            registry,
            tenant_id=caller.tenant_id,
            credit_code=payload.normalized_code(),
        )


def register_admin_billing_routes(
    app: FastAPI,
    check_admin: Callable,
    settings: Settings | None = None,
) -> None:
    """Register admin billing and credit-code routes."""

    @app.post("/v1/admin/credit-codes", dependencies=[Depends(check_admin)])
    async def admin_create_credit_code(
        payload: AdminCreditCodeCreateRequest,
        request: Request,
    ):
        registry = _registry(request)
        try:
            expires_at = payload.expires_at
            if payload.expires_in_seconds not in (None, ""):
                expires_at = time.time() + float(payload.expires_in_seconds)
            return await asyncio.to_thread(
                registry.create_credit_code,
                credit_usd=payload.credit_usd,
                max_redemptions=int(
                    payload.max_redemptions or payload.uses or 1
                ),
                expires_at=expires_at,
                label=str(payload.label or ""),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/v1/admin/credit-codes", dependencies=[Depends(check_admin)])
    async def admin_list_credit_codes(
        request: Request,
        include_revoked: bool = False,
        limit: int = 100,
    ):
        registry = _registry(request)
        codes = await asyncio.to_thread(
            registry.list_credit_codes,
            include_revoked=include_revoked,
            limit=limit,
        )
        return {"credit_codes": codes}

    @app.post(
        "/v1/admin/credit-codes/{code_id}/revoke",
        dependencies=[Depends(check_admin)],
    )
    async def admin_revoke_credit_code(code_id: str, request: Request):
        registry = _registry(request)
        ok = await asyncio.to_thread(registry.revoke_credit_code, code_id)
        if not ok:
            raise HTTPException(404, "credit code not found or already revoked")
        return {"status": "ok", "code_id": code_id}

    @app.get("/v1/admin/billing", dependencies=[Depends(check_admin)])
    async def admin_billing_accounts(request: Request):
        registry = _registry(request)
        accounts = await asyncio.to_thread(registry.billing_accounts)
        return {
            "accounts": accounts,
            "enforce_credits": bool(
                settings.billing_enforce_credits
            ) if settings is not None else False,
        }

    @app.get("/v1/admin/billing/ledger", dependencies=[Depends(check_admin)])
    async def admin_billing_ledger(
        request: Request,
        tenant_id: str | None = None,
        limit: int = 100,
    ):
        registry = _registry(request)
        rows = await asyncio.to_thread(
            registry.billing_ledger_all,
            tenant_id=tenant_id,
            limit=limit,
        )
        return {"ledger": rows}

    @app.get("/v1/admin/billing/prices", dependencies=[Depends(check_admin)])
    async def admin_billing_prices(request: Request):
        registry = _registry(request)
        prices = await asyncio.to_thread(registry.billing_list_prices)
        return {"prices": prices}

    @app.delete(
        "/v1/admin/billing/prices/{provider}/{model:path}",
        dependencies=[Depends(check_admin)],
    )
    async def admin_billing_delete_price(
        provider: str,
        model: str,
        request: Request,
    ):
        """Remove a (provider, model) row from the price table.

        After deletion, the next run requesting this provider/model is
        treated as missing-price: rejected in enforce mode, free in
        non-enforce mode. Use to roll back a bad seed.
        """
        registry = _registry(request)
        try:
            removed = await asyncio.to_thread(
                registry.billing_delete_price,
                provider,
                model,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not removed:
            raise HTTPException(
                404,
                f"price row not found for {provider}/{model}",
            )
        return {"status": "ok", "provider": provider, "model": model}

    @app.post("/v1/admin/billing/prices", dependencies=[Depends(check_admin)])
    async def admin_billing_set_price(
        payload: AdminBillingPriceRequest,
        request: Request,
    ):
        registry = _registry(request)
        try:
            return await asyncio.to_thread(
                registry.billing_set_price,
                payload.provider,
                payload.model,
                prompt_usd_per_million=payload.prompt_usd_per_million,
                completion_usd_per_million=payload.completion_usd_per_million,
                source=str(payload.source or "admin"),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/v1/admin/billing/{tenant_id}", dependencies=[Depends(check_admin)])
    async def admin_billing_account(
        tenant_id: str,
        request: Request,
        limit: int = 25,
    ):
        registry = _registry(request)
        try:
            return await asyncio.to_thread(
                registry.billing_account,
                tenant_id,
                limit=limit,
            )
        except KeyError as e:
            raise HTTPException(404, str(e))

    @app.post(
        "/v1/admin/billing/{tenant_id}/credits",
        dependencies=[Depends(check_admin)],
    )
    async def admin_billing_grant_credit(
        tenant_id: str,
        payload: AdminCreditGrantRequest,
        request: Request,
    ):
        if payload.amount_usd is None:
            raise HTTPException(400, "'amount_usd' required")
        registry = _registry(request)
        try:
            return await asyncio.to_thread(
                registry.billing_grant_credit,
                tenant_id,
                payload.amount_usd,
                note=str(payload.note or ""),
                actor="admin",
            )
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

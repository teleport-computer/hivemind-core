"""Billing ledger and model-price operations for tenants."""

from __future__ import annotations

import json as _json
import secrets
import time
from typing import Any

from .tenant_keys import (
    API_KEY_PREFIX,
    charge_for_tokens,
    hash_api_key,
    token_id,
    usd_per_mtok_to_micro,
    usd_to_micro_usd,
)


class BillingRegistryMixin:
    """Control-plane billing accounts, holds, charges, and price snapshots."""

    def resolve_payer_key(self, api_key: str) -> dict | None:
        """Resolve an ``hmk_`` key for payer attribution without thawing.

        This deliberately does not return a Hivemind instance and does not
        touch tenant seals. It only proves that the query caller also controls
        the payer tenant's owner key.
        """
        if not api_key or not api_key.startswith(API_KEY_PREFIX):
            return None
        api_key_hash = hash_api_key(api_key)
        rows = self._control_db.execute(
            "SELECT id, suspended FROM _tenants WHERE api_key_hash = %s",
            [api_key_hash],
        )
        if not rows or rows[0]["suspended"]:
            return None
        return {
            "tenant_id": rows[0]["id"],
            "payer_token_id": token_id(api_key_hash),
        }

    def billing_balance_micro_usd(self, tenant_id: str) -> int:
        rows = self._control_db.execute(
            "SELECT COALESCE(SUM(amount_micro_usd), 0) AS balance "
            "FROM _billing_ledger WHERE tenant_id = %s",
            [tenant_id],
        )
        return int(rows[0]["balance"] or 0) if rows else 0

    def billing_account(self, tenant_id: str, *, limit: int = 25) -> dict:
        tenant = self.get_by_id(tenant_id)
        if tenant is None:
            raise KeyError(f"tenant '{tenant_id}' not found")
        return {
            "tenant_id": tenant_id,
            "balance_micro_usd": self.billing_balance_micro_usd(tenant_id),
            "ledger": self.billing_ledger(tenant_id, limit=limit),
        }

    def billing_accounts(self) -> list[dict]:
        rows = self._control_db.execute(
            "SELECT t.id AS tenant_id, t.name, t.created_at, t.suspended, "
            "COALESCE(SUM(l.amount_micro_usd), 0) AS balance_micro_usd, "
            "COALESCE(SUM(CASE WHEN l.kind = 'credit_grant' "
            "THEN l.amount_micro_usd ELSE 0 END), 0) "
            "AS total_credit_micro_usd, "
            "COALESCE(SUM(CASE WHEN l.kind = 'usage_charge' "
            "THEN -l.amount_micro_usd ELSE 0 END), 0) "
            "AS total_spent_micro_usd, "
            "COALESCE(SUM(CASE WHEN l.kind = 'usage_hold' "
            "THEN -l.amount_micro_usd ELSE 0 END), 0) "
            "AS total_held_micro_usd "
            "FROM _tenants t "
            "LEFT JOIN _billing_ledger l ON l.tenant_id = t.id "
            "GROUP BY t.id, t.name, t.created_at, t.suspended "
            "ORDER BY t.created_at DESC"
        )
        return [dict(r) for r in rows]

    def billing_ledger(self, tenant_id: str, *, limit: int = 50) -> list[dict]:
        rows = self._control_db.execute(
            "SELECT entry_id, tenant_id, created_at, kind, run_id, "
            "amount_micro_usd, metadata FROM _billing_ledger "
            "WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s",
            [tenant_id, min(max(1, int(limit)), 500)],
        )
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = _json.loads(item.get("metadata") or "{}")
            except (TypeError, ValueError):
                item["metadata"] = {}
            out.append(item)
        return out

    def billing_ledger_all(
        self,
        *,
        limit: int = 100,
        tenant_id: str | None = None,
    ) -> list[dict]:
        params: list[Any] = []
        where = ""
        if tenant_id:
            where = "WHERE l.tenant_id = %s"
            params.append(tenant_id)
        params.append(min(max(1, int(limit)), 1000))
        rows = self._control_db.execute(
            "SELECT l.entry_id, l.tenant_id, t.name, l.created_at, l.kind, "
            "l.run_id, l.amount_micro_usd, l.metadata "
            "FROM _billing_ledger l "
            "JOIN _tenants t ON t.id = l.tenant_id "
            f"{where} "
            "ORDER BY l.created_at DESC LIMIT %s",
            params,
        )
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = _json.loads(item.get("metadata") or "{}")
            except (TypeError, ValueError):
                item["metadata"] = {}
            out.append(item)
        return out

    def billing_grant_credit(
        self,
        tenant_id: str,
        amount_usd: Any,
        *,
        note: str = "",
        actor: str = "admin",
        metadata: dict | None = None,
    ) -> dict:
        amount = usd_to_micro_usd(amount_usd)
        return self.billing_grant_credit_micro(
            tenant_id,
            amount,
            note=note,
            actor=actor,
            metadata=metadata,
        )

    def billing_grant_credit_micro(
        self,
        tenant_id: str,
        amount_micro_usd: Any,
        *,
        note: str = "",
        actor: str = "admin",
        metadata: dict | None = None,
    ) -> dict:
        rows = self._control_db.execute(
            "SELECT id FROM _tenants WHERE id = %s", [tenant_id]
        )
        if not rows:
            raise KeyError(f"tenant '{tenant_id}' not found")
        amount = int(amount_micro_usd or 0)
        if amount <= 0:
            raise ValueError("credit amount must be positive")
        entry_metadata = {"note": note, "actor": actor}
        if metadata:
            entry_metadata.update(metadata)
        entry = self._insert_billing_entry(
            tenant_id,
            kind="credit_grant",
            amount_micro_usd=amount,
            metadata=entry_metadata,
        )
        entry["balance_micro_usd"] = self.billing_balance_micro_usd(tenant_id)
        return entry

    def billing_delete_price(self, provider: str, model: str) -> bool:
        """Hard-delete a (provider, model) row from the price table.

        Returns True if a row was removed, False if no matching row existed.
        Use to roll back a bad/hallucinated entry: with the row gone, the
        next run requesting this provider/model is treated as missing
        price (rejected in enforce mode, free in non-enforce mode).
        """
        clean_provider = (provider or "").strip().lower()
        clean_model = (model or "").strip()
        if not clean_provider or not clean_model:
            raise ValueError("provider and model required")
        rowcount = self._control_db.execute_commit(
            "DELETE FROM _billing_model_prices "
            "WHERE provider = %s AND model = %s",
            [clean_provider, clean_model],
        )
        return rowcount > 0

    def billing_list_prices(self) -> list[dict]:
        rows = self._control_db.execute(
            "SELECT provider, model, prompt_microusd_per_mtok, "
            "completion_microusd_per_mtok, updated_at, source "
            "FROM _billing_model_prices ORDER BY provider, model"
        )
        return [dict(r) for r in rows]

    def billing_set_price(
        self,
        provider: str,
        model: str,
        *,
        prompt_usd_per_million: Any,
        completion_usd_per_million: Any,
        source: str = "admin",
    ) -> dict:
        provider = (provider or "").strip().lower()
        model = (model or "").strip()
        if not provider or not model:
            raise ValueError("provider and model are required")
        prompt_price = usd_per_mtok_to_micro(prompt_usd_per_million)
        completion_price = usd_per_mtok_to_micro(completion_usd_per_million)
        now = time.time()
        self._control_db.execute_commit(
            "INSERT INTO _billing_model_prices "
            "(provider, model, prompt_microusd_per_mtok, "
            "completion_microusd_per_mtok, updated_at, source) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (provider, model) DO UPDATE SET "
            "prompt_microusd_per_mtok = EXCLUDED.prompt_microusd_per_mtok, "
            "completion_microusd_per_mtok = "
            "EXCLUDED.completion_microusd_per_mtok, "
            "updated_at = EXCLUDED.updated_at, source = EXCLUDED.source",
            [provider, model, prompt_price, completion_price, now, source],
        )
        return {
            "provider": provider,
            "model": model,
            "prompt_microusd_per_mtok": prompt_price,
            "completion_microusd_per_mtok": completion_price,
            "updated_at": now,
            "source": source,
        }

    def billing_get_price(self, provider: str, model: str) -> dict | None:
        provider = (provider or "").strip().lower()
        model = (model or "").strip()
        if not provider or not model:
            return None
        rows = self._control_db.execute(
            "SELECT provider, model, prompt_microusd_per_mtok, "
            "completion_microusd_per_mtok, updated_at, source "
            "FROM _billing_model_prices WHERE provider = %s AND model = %s",
            [provider, model],
        )
        return dict(rows[0]) if rows else None

    def billing_hold_for_run(
        self,
        *,
        tenant_id: str | None,
        payer_token_id: str | None,
        run_id: str,
        provider: str | None,
        models: list[str],
        max_tokens: int,
        billable_role: str,
        enforce: bool,
    ) -> dict:
        """Create a preflight hold for the maximum requested token budget."""
        if not tenant_id:
            return {"hold_micro_usd": 0, "status": "unbilled"}
        provider_key = (provider or "").strip().lower()
        clean_models = [m.strip() for m in models if str(m).strip()]
        if not provider_key or not clean_models or max_tokens <= 0:
            return {"hold_micro_usd": 0, "status": "not_billable"}

        missing: list[str] = []
        max_rate = 0
        for model in clean_models:
            price = self.billing_get_price(provider_key, model)
            if not price:
                missing.append(model)
                continue
            max_rate = max(
                max_rate,
                int(price["prompt_microusd_per_mtok"] or 0),
                int(price["completion_microusd_per_mtok"] or 0),
            )
        if missing:
            if enforce:
                raise ValueError(
                    "billing price is not configured for "
                    + ", ".join(f"{provider_key}/{m}" for m in missing)
                )
            return {
                "hold_micro_usd": 0,
                "status": "pricing_missing",
                "missing_prices": missing,
            }
        hold = charge_for_tokens(max_tokens, 0, max_rate, 0)
        if hold <= 0:
            return {"hold_micro_usd": 0, "status": "not_billable"}
        balance = self.billing_balance_micro_usd(tenant_id)
        if enforce and balance < hold:
            raise ValueError(
                "insufficient billing credit: "
                f"balance_micro_usd={balance}, required_hold_micro_usd={hold}"
            )
        self._insert_billing_entry(
            tenant_id,
            kind="usage_hold",
            run_id=run_id,
            amount_micro_usd=-hold,
            metadata={
                "payer_token_id": payer_token_id or "",
                "provider": provider_key,
                "models": clean_models,
                "max_tokens": int(max_tokens),
                "billable_role": billable_role,
            },
        )
        return {"hold_micro_usd": hold, "status": "held"}

    def settle_run(
        self,
        *,
        payer_tenant_id: str | None,
        payer_token_id: str | None,
        run_id: str,
        usage: dict,
        hold_micro_usd: int = 0,
        billable_role: str = "query",
        default_provider: str | None = None,
        default_model: str | None = None,
    ) -> dict:
        """Release any hold and charge exact metered usage for a run."""
        now = time.time()
        if not payer_tenant_id:
            return {
                "billing_status": "unbilled",
                "cost_micro_usd": 0,
                "settled_at": now,
            }
        cost, missing = self._cost_for_usage(
            usage,
            default_provider=default_provider,
            default_model=default_model,
        )
        hold = max(0, int(hold_micro_usd or 0))
        metadata = {
            "payer_token_id": payer_token_id or "",
            "billable_role": billable_role,
            "usage": usage,
            "missing_prices": missing,
        }
        if hold:
            self._insert_billing_entry(
                payer_tenant_id,
                kind="usage_release",
                run_id=run_id,
                amount_micro_usd=hold,
                metadata=metadata,
            )
        if cost:
            self._insert_billing_entry(
                payer_tenant_id,
                kind="usage_charge",
                run_id=run_id,
                amount_micro_usd=-cost,
                metadata=metadata,
            )
        status = "settled" if not missing else "pricing_missing"
        return {
            "billing_status": status,
            "cost_micro_usd": cost,
            "settled_at": now,
            "missing_prices": missing,
        }

    def _insert_billing_entry(
        self,
        tenant_id: str,
        *,
        kind: str,
        amount_micro_usd: int,
        run_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        entry_id = "ble_" + secrets.token_hex(12)
        now = time.time()
        metadata_json = _json.dumps(
            metadata or {}, sort_keys=True, separators=(",", ":")
        )
        self._control_db.execute_commit(
            "INSERT INTO _billing_ledger "
            "(entry_id, tenant_id, created_at, kind, run_id, "
            "amount_micro_usd, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [
                entry_id,
                tenant_id,
                now,
                kind,
                run_id,
                int(amount_micro_usd),
                metadata_json,
            ],
        )
        return {
            "entry_id": entry_id,
            "tenant_id": tenant_id,
            "created_at": now,
            "kind": kind,
            "run_id": run_id,
            "amount_micro_usd": int(amount_micro_usd),
            "metadata": metadata or {},
        }

    def _cost_for_usage(
        self,
        usage: dict,
        *,
        default_provider: str | None,
        default_model: str | None,
    ) -> tuple[int, list[str]]:
        stages = usage.get("stages") if isinstance(usage, dict) else None
        items: list[dict]
        if isinstance(stages, dict) and stages:
            items = [v for v in stages.values() if isinstance(v, dict)]
        else:
            items = [usage] if isinstance(usage, dict) else []
        total = 0
        missing: list[str] = []
        for item in items:
            provider = (item.get("provider") or default_provider or "").strip().lower()
            model = (item.get("model") or default_model or "").strip()
            prompt = int(item.get("prompt_tokens") or 0)
            completion = int(item.get("completion_tokens") or 0)
            if not provider or not model or (prompt + completion) <= 0:
                continue
            price = self.billing_get_price(provider, model)
            if not price:
                key = f"{provider}/{model}"
                if key not in missing:
                    missing.append(key)
                continue
            total += charge_for_tokens(
                prompt,
                completion,
                int(price["prompt_microusd_per_mtok"] or 0),
                int(price["completion_microusd_per_mtok"] or 0),
            )
        return total, missing

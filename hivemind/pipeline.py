import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable

import httpx
from openai import AsyncOpenAI

from .config import Settings
from .db import Database
from .models import (
    QueryRequest,
    QueryResponse,
    StoreRequest,
    StoreResponse,
)
from .sandbox.agents import AgentStore
from .sandbox.backend import SandboxBackend
from .sandbox.bridge import _render_markdown_to_simple_pdf
from .sandbox.models import AgentConfig
from .sandbox.models import MAX_ARTIFACT_BYTES, validate_artifact_filename
from .sandbox.settings import build_sandbox_settings
from .scope import compile_scope_fn
from .tools import (
    AccessLevel,
    build_agent_file_tools,
    build_room_vault_tools,
    build_sql_tools,
)

logger = logging.getLogger(__name__)

MEDIATOR_MIN_TOKENS = 128
# A single Claude Code CLI call to the mediator sends ~15-25k tokens
# (system prompt + raw_output + question). Reserving only 512 tokens
# starves the mediator and triggers a 429 → SDK crash. 30k gives
# comfortable headroom for at least one full mediator call.
MEDIATOR_TOKEN_RESERVE = 30_000
# Below this fraction of remaining budget the flat reserve gets downsized
# so we never hand more than this share to the mediator and leave the
# query agent with too little to make progress.
MEDIATOR_RESERVE_FRACTION = 0.3
# Scope is capped to a fraction of the global budget so it cannot starve
# the downstream query + mediator agents. A single Claude Code CLI call
# typically sends ~20-25k tokens in its first request (system prompt + tools
# + context) — if scope consumes the whole budget the bridge returns 429 to
# query's very first call and the SDK subprocess exits with code 1.
SCOPE_BUDGET_FRACTION = 0.5
DEFAULT_ROOM_LLM_PROVIDER = "openrouter"


def _looks_like_report_output(prompt: str, output: str) -> bool:
    text = (output or "").strip()
    if len(re.findall(r"\S+", text)) < 500:
        return False
    prompt_lower = (prompt or "").lower()
    if not any(
        marker in prompt_lower
        for marker in (
            "report",
            "research",
            "study",
            "memo",
            "analysis",
            "pdf",
            "file",
        )
    ):
        return False
    output_lower = text.lower()
    markers = (
        "executive summary",
        "methodology",
        "findings",
        "limitations",
        "implications",
    )
    return text.startswith("#") or sum(marker in output_lower for marker in markers) >= 2


def _artifact_stem_from_prompt(prompt: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", (prompt or "report").lower())
    stem = stem.strip("._-")[:80] or "report"
    if not re.match(r"^[A-Za-z0-9]", stem):
        stem = f"report_{stem}"
    return validate_artifact_filename(stem[:100])


def _store_report_artifacts_if_needed(
    *,
    artifact_store,
    artifacts_enabled: bool,
    run_id: str,
    prompt: str,
    final_output: str,
) -> None:
    if not artifact_store or not artifacts_enabled:
        return
    markdown = (final_output or "").strip()
    if not _looks_like_report_output(prompt, markdown):
        return
    stem = _artifact_stem_from_prompt(prompt)
    markdown_bytes = markdown.encode("utf-8")
    if len(markdown_bytes) <= MAX_ARTIFACT_BYTES:
        artifact_store.put(
            run_id,
            f"{stem}.md",
            markdown_bytes,
            "text/markdown; charset=utf-8",
        )
    pdf_bytes = _render_markdown_to_simple_pdf(markdown)
    if len(pdf_bytes) <= MAX_ARTIFACT_BYTES:
        artifact_store.put(run_id, f"{stem}.pdf", pdf_bytes, "application/pdf")


def _extract_scope_agent_json(raw: str) -> dict:
    """Return the last JSON object containing scope_fn from agent stdout.

    Hermes can write provider retry diagnostics to stdout before its final
    machine-readable JSON line. Keep the scope contract strict by accepting
    only a decoded JSON object that contains ``scope_fn``.
    """
    text = (raw or "").strip()
    decoder = json.JSONDecoder()
    found: dict | None = None
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "scope_fn" in obj:
            found = obj
    if found is None:
        raise json.JSONDecodeError("no scope_fn JSON object found", text, 0)
    return found


def _mediator_reserve(remaining: int) -> int:
    """Return how many tokens to reserve from the query stage for mediator.

    Proportional to remaining budget (capped by MEDIATOR_TOKEN_RESERVE),
    floored at MEDIATOR_MIN_TOKENS, and never so large that the query
    stage would drop below MEDIATOR_MIN_TOKENS itself.
    """
    desired = min(MEDIATOR_TOKEN_RESERVE, int(remaining * MEDIATOR_RESERVE_FRACTION))
    desired = max(MEDIATOR_MIN_TOKENS, desired)
    return min(desired, max(0, remaining - MEDIATOR_MIN_TOKENS))


def _new_usage_summary(max_tokens: int = 0) -> dict:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "max_tokens": int(max_tokens or 0),
        "stages": {},
    }


def _add_stage_usage(
    summary: dict,
    stage: str,
    usage: dict | None,
    *,
    provider: str | None,
    model: str | None,
) -> None:
    if not isinstance(usage, dict):
        usage = {}
    item = {
        "calls": int(usage.get("calls") or 0),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "max_calls": int(usage.get("max_calls") or 0),
        "max_tokens": int(usage.get("max_tokens") or 0),
        "provider": (provider or "").strip().lower(),
        "model": (model or "").strip(),
    }
    if isinstance(usage.get("bridge"), dict):
        item["bridge"] = usage["bridge"]
    summary["calls"] += item["calls"]
    summary["prompt_tokens"] += item["prompt_tokens"]
    summary["completion_tokens"] += item["completion_tokens"]
    summary["total_tokens"] += item["total_tokens"]
    summary.setdefault("stages", {})[stage] = item


class Pipeline:
    """Orchestrates store and query pipelines using Docker agent sandboxes."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        agent_store: AgentStore,
        *,
        billing_meter=None,
    ):
        self.settings = settings
        self.db = db
        self.agent_store = agent_store
        self.billing_meter = billing_meter
        timeout = httpx.Timeout(
            connect=5.0,
            read=float(settings.llm_timeout_seconds),
            write=float(settings.llm_timeout_seconds),
            pool=float(settings.llm_timeout_seconds),
        )
        # OpenRouter is the default provider and is always built. Tinfoil
        # is built only when its API key is configured; callers asking for
        # it without a key get a clear error from ``_client_for``.
        self.llm_clients: dict[str, AsyncOpenAI] = {
            "openrouter": AsyncOpenAI(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                timeout=timeout,
                max_retries=0,
            ),
        }
        if settings.tinfoil_api_key:
            self.llm_clients["tinfoil"] = AsyncOpenAI(
                base_url=settings.tinfoil_base_url,
                api_key=settings.tinfoil_api_key,
                timeout=timeout,
                max_retries=0,
            )
        self.llm_model = settings.llm_model
        self._role_models = {
            "scope": (settings.scope_model or settings.llm_model),
            "query": (settings.query_model or settings.llm_model),
            "mediator": (settings.mediator_model or settings.llm_model),
        }
        self._disabled_llm_providers = {
            p.strip().lower()
            for p in (settings.disabled_llm_providers or "").split(",")
            if p.strip()
        }
        self._disabled_llm_routes = self._parse_disabled_llm_routes(
            settings.disabled_llm_routes
        )
        self._sandbox_settings = build_sandbox_settings(settings)

    def _model_for(self, role: str, override: str | None = None) -> str:
        """Resolve the LLM model for a given role.

        Caller-supplied ``override`` wins; otherwise per-role config; otherwise
        the global ``llm_model``.
        """
        if override:
            return override
        return self._role_models.get(role, self.llm_model)

    @staticmethod
    def _parse_disabled_llm_routes(raw: str) -> set[tuple[str, str]]:
        routes: set[tuple[str, str]] = set()
        for item in (raw or "").split(","):
            entry = item.strip()
            if not entry:
                continue
            provider, sep, model = entry.partition(":")
            if sep and provider.strip() and model.strip():
                routes.add((provider.strip().lower(), model.strip().lower()))
        return routes

    def _client_for(self, provider: str | None) -> AsyncOpenAI:
        """Resolve which AsyncOpenAI client to use for a request.

        ``None`` / empty / ``"openrouter"`` → default client. ``"tinfoil"``
        → tinfoil client when configured. Any other value, or tinfoil
        without an API key, raises ``ValueError`` so the failure is loud
        at request boundaries instead of silently falling back.
        """
        key = (provider or "").strip().lower()
        resolved_key = key or "openrouter"
        if resolved_key in self._disabled_llm_providers:
            raise ValueError(
                f"LLM provider '{resolved_key}' is disabled by operator "
                "configuration. Omit provider or choose an enabled provider."
            )
        if not key or key == "openrouter":
            return self.llm_clients["openrouter"]
        if key in self.llm_clients:
            return self.llm_clients[key]
        if key == "tinfoil":
            raise ValueError(
                "provider='tinfoil' requires HIVEMIND_TINFOIL_API_KEY on "
                "the server. Configure it and redeploy, or omit the field."
            )
        raise ValueError(
            f"Unknown provider '{provider}'. Valid: "
            f"{sorted(self.llm_clients)}"
        )

    def _provider_key(self, provider: str | None) -> str:
        key = (provider or "").strip().lower()
        return key or "openrouter"

    def _ensure_llm_route_enabled(self, provider: str | None, model: str) -> None:
        provider_key = self._provider_key(provider)
        model_key = (model or "").strip().lower()
        if model_key and (provider_key, model_key) in self._disabled_llm_routes:
            raise ValueError(
                f"LLM route '{provider_key}:{model}' is disabled by operator "
                "configuration. Choose a different provider or model."
            )

    def validate_llm_route(
        self,
        provider: str | None,
        allowed_llm_providers: list[str] | None,
        models: list[str],
    ) -> tuple[str | None, bool]:
        """Validate provider/model egress before billing or sandbox startup."""
        selected, enabled = self._resolve_provider_for_egress(
            provider,
            allowed_llm_providers,
        )
        if enabled:
            for model in models:
                self._ensure_llm_route_enabled(selected, model)
        return selected, enabled

    async def _record_run_usage(
        self,
        run_store,
        run_id: str,
        usage: dict,
        *,
        payer_tenant_id: str | None = None,
        payer_token_id: str | None = None,
        billable_role: str = "query",
        billing_hold_micro_usd: int = 0,
        default_provider: str | None = None,
        default_model: str | None = None,
    ) -> None:
        """Persist usage and settle ledger charges when a payer is known."""
        if not hasattr(run_store, "update_usage"):
            return
        billing_status = "unbilled"
        cost_micro_usd = 0
        settled_at = None
        if payer_tenant_id and self.billing_meter is not None:
            try:
                settlement = await asyncio.to_thread(
                    self.billing_meter.settle_run,
                    payer_tenant_id=payer_tenant_id,
                    payer_token_id=payer_token_id,
                    run_id=run_id,
                    usage=usage,
                    hold_micro_usd=billing_hold_micro_usd,
                    billable_role=billable_role,
                    default_provider=default_provider,
                    default_model=default_model,
                )
                billing_status = settlement.get("billing_status", "settled")
                cost_micro_usd = int(settlement.get("cost_micro_usd") or 0)
                settled_at = settlement.get("settled_at")
            except Exception as e:
                logger.warning("billing settlement failed for run %s: %s", run_id, e)
                billing_status = "billing_error"
        elif payer_tenant_id:
            billing_status = "metered"
        await asyncio.to_thread(
            run_store.update_usage,
            run_id,
            usage,
            billing_cost_micro_usd=cost_micro_usd,
            billing_status=billing_status,
            billing_settled_at=settled_at,
        )

    def _resolve_provider_for_egress(
        self,
        provider: str | None,
        allowed_llm_providers: list[str] | None,
    ) -> tuple[str | None, bool]:
        """Return ``(provider, llm_egress_enabled)`` for a run.

        ``allowed_llm_providers=None`` is unrestricted internal behavior.
        ``[]`` means no external LLM egress; the bridge still starts, but
        all LLM endpoints return 403 so non-LLM agents can still use SQL
        tools and produce deterministic output.
        """
        if allowed_llm_providers is None:
            selected = self._provider_key(provider)
            self._client_for(selected)
            return selected, True

        allowed = []
        for raw in allowed_llm_providers:
            key = self._provider_key(raw)
            if key not in allowed:
                allowed.append(key)
        if not allowed:
            return provider, False

        if provider:
            selected = self._provider_key(provider)
        elif DEFAULT_ROOM_LLM_PROVIDER in allowed:
            selected = DEFAULT_ROOM_LLM_PROVIDER
        else:
            selected = allowed[0]
        if selected not in allowed:
            raise ValueError(
                f"provider '{selected}' is not allowed by this room; "
                f"allowed_llm_providers={allowed}"
            )
        self._client_for(selected)
        return selected, True

    # -- Store pipeline --

    async def run_store(self, req: StoreRequest) -> StoreResponse:
        """Execute raw SQL via the store pipeline.

        If the SQL is a SELECT, return rows. Otherwise commit and return rowcount.
        Optionally runs an index agent for pre-processing.
        """
        from .tools import _is_select_only, _references_internal_tables

        sql = req.sql
        params = req.params

        if _references_internal_tables(sql):
            raise ValueError("Access to internal tables is denied via store endpoint")

        try:
            if _is_select_only(sql):
                rows = await asyncio.to_thread(self.db.execute, sql, params)
                return StoreResponse(rows=rows, rowcount=len(rows))
            else:
                rowcount = await asyncio.to_thread(
                    self.db.execute_commit, sql, params
                )
                return StoreResponse(rowcount=rowcount)
        except Exception as e:
            raise ValueError(f"SQL execution failed: {e}")

    # -- Query pipeline --

    async def run_query(self, req: QueryRequest) -> QueryResponse:
        # Resolve effective budget
        global_max = self._sandbox_settings.global_max_tokens
        requested_max = req.max_tokens or self.settings.default_query_max_tokens
        effective_max = min(requested_max, global_max)
        remaining = effective_max
        total_tokens = 0
        mediator_agent_id = req.mediator_agent_id or self.settings.default_mediator_agent

        # Stage 0: Scope resolution → produces a scope_fn
        scope_fn = None
        scope_fn_source = ""
        scope_budget = max(1, int(remaining * SCOPE_BUDGET_FRACTION))

        req_timeout = req.timeout_seconds
        req_max_calls = req.max_llm_calls
        req_scope_model = req.scope_model or req.model
        req_query_model = req.query_model or req.model
        req_mediator_model = req.mediator_model or req.model
        req_provider = req.provider
        # Eagerly resolve so an unknown or operator-disabled LLM route fails
        # the whole request before we burn scope-stage budget.
        self.validate_llm_route(
            req_provider,
            None,
            [
                self._model_for("scope", req_scope_model),
                self._model_for("query", req_query_model),
                *(
                    [self._model_for("mediator", req_mediator_model)]
                    if mediator_agent_id
                    else []
                ),
            ],
        )

        query_agent_id = req.query_agent_id or self.settings.default_query_agent
        if not query_agent_id:
            raise ValueError(
                "No query agent specified and no default configured"
            )

        if not (req.scope_agent_id or self.settings.default_scope_agent):
            raise ValueError(
                "scope_agent_id is required (no default scope agent configured)"
            )

        if req.scope_agent_id:
            scope_fn, scope_fn_source, scope_usage = await self._run_scope_agent(
                req, max_tokens=scope_budget,
                max_calls=req_max_calls, timeout_seconds=req_timeout,
                model=req_scope_model, provider=req_provider,
            )
            used = scope_usage.get("total_tokens", 0)
            total_tokens += used
            remaining = max(1, remaining - used)
        elif self.settings.default_scope_agent:
            scope_fn, scope_fn_source, scope_usage = await self._run_scope_agent(
                req, max_tokens=scope_budget,
                max_calls=req_max_calls, timeout_seconds=req_timeout,
                model=req_scope_model, provider=req_provider,
            )
            used = scope_usage.get("total_tokens", 0)
            total_tokens += used
            remaining = max(1, remaining - used)

        # Stage 1: Query agent
        query_max_tokens = remaining
        if mediator_agent_id and remaining > MEDIATOR_MIN_TOKENS:
            query_max_tokens = max(1, remaining - _mediator_reserve(remaining))

        output, query_usage = await self._run_query_agent(
            query_agent_id=query_agent_id,
            prompt=req.query,
            scope_fn=scope_fn,
            scope_fn_source=scope_fn_source,
            max_calls=req_max_calls,
            max_tokens=query_max_tokens,
            timeout_seconds=req_timeout,
            return_usage=True,
            model=req_query_model,
            provider=req_provider,
        )
        used = query_usage.get("total_tokens", 0)
        total_tokens += used
        remaining = max(1, remaining - used)

        # Stage 2: Optional mediator
        mediated = False
        if mediator_agent_id:
            if remaining < MEDIATOR_MIN_TOKENS:
                raise ValueError(
                    f"Mediator '{mediator_agent_id}' could not run: "
                    f"insufficient remaining budget ({remaining} < "
                    f"{MEDIATOR_MIN_TOKENS}). Refusing to return "
                    "unmediated output."
                )
            try:
                output, mediator_usage = await self._run_mediator_agent(
                    mediator_agent_id=mediator_agent_id,
                    raw_output=output,
                    prompt=req.query,
                    max_tokens=remaining,
                    max_calls=req_max_calls,
                    timeout_seconds=req_timeout,
                    policy=req.policy,
                    model=req_mediator_model,
                    provider=req_provider,
                )
            except ValueError as e:
                if "not found" in str(e).lower():
                    raise
                raise ValueError(
                    f"Mediator '{mediator_agent_id}' failed; refusing to "
                    "return unmediated output"
                ) from e
            used = mediator_usage.get("total_tokens", 0)
            total_tokens += used
            mediated = True

        return QueryResponse(
            output=output,
            mediated=mediated,
            usage={"total_tokens": total_tokens, "max_tokens": effective_max},
        )

    # -- Internal: run agents --

    async def _run_agent(
        self,
        agent_config: AgentConfig,
        role: str,
        env: dict[str, str],
        tools: list,
        on_tool_call: Callable,
        agent_store_for_bridge=None,
        run_query_fn=None,
        scope_query_agent_id: str | None = None,
        max_calls: int | None = None,
        max_tokens: int | None = None,
        timeout_seconds: int | None = None,
        return_budget_summary: bool = False,
        replay_tape: list[dict] | None = None,
        return_tape: bool = False,
        extra_volumes: dict[str, dict[str, str]] | None = None,
        model: str | None = None,
        provider: str | None = None,
        llm_egress_enabled: bool = True,
    ):
        """Run a Docker agent with tools and return its stdout."""
        backend = SandboxBackend(
            self._client_for(provider),
            self._model_for(role, model),
            self._sandbox_settings,
            agent_config,
            agent_store=self.agent_store,
        )

        return await backend.run(
            role=role,
            env=env,
            tools=tools,
            on_tool_call=on_tool_call,
            agent_store=agent_store_for_bridge,
            run_query_fn=run_query_fn,
            scope_query_agent_id=scope_query_agent_id,
            max_calls=max_calls,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            return_budget_summary=return_budget_summary,
            replay_tape=replay_tape,
            return_tape=return_tape,
            extra_volumes=extra_volumes,
            llm_egress_enabled=llm_egress_enabled,
        )

    async def _run_scope_agent(
        self,
        req: QueryRequest,
        max_tokens: int | None = None,
        max_calls: int | None = None,
        timeout_seconds: int | None = None,
        model: str | None = None,
        provider: str | None = None,
        llm_egress_enabled: bool = True,
        room_vault_items: list[dict] | None = None,
        allowed_tables: list[str] | None = None,
    ) -> tuple[Callable, str, dict]:
        """Run scope agent to produce a scope function.

        Returns (scope_fn, scope_fn_source, usage). The source text is
        forwarded to the query agent so it knows what the privacy filter
        expects and can write SQL that matches — without this, query
        keeps guessing patterns and hitting allow=False.
        """
        scope_agent_id = req.scope_agent_id or self.settings.default_scope_agent
        agent_config = await asyncio.to_thread(
            self.agent_store.get, scope_agent_id
        )
        if agent_config is None:
            raise ValueError(f"Scope agent '{scope_agent_id}' not found")

        query_agent_id = req.query_agent_id or self.settings.default_query_agent
        allowed_query_agent_id = query_agent_id

        # Build simulation function for the scope agent
        async def run_query_fn(
            query_agent_id: str,
            prompt: str,
            scope_fn_source: str,
            max_calls: int,
            max_tokens: int,
            replay_tape: list[dict] | None = None,
        ) -> tuple[str, dict] | tuple[str, dict, list[dict] | None]:
            if not allowed_query_agent_id:
                raise ValueError("No query agent is configured for scope simulation")
            if query_agent_id != allowed_query_agent_id:
                raise ValueError(
                    f"Simulation is restricted to query agent '{allowed_query_agent_id}'"
                )
            # Compile the scope function from source
            sim_scope_fn = compile_scope_fn(scope_fn_source)
            return await self._run_query_agent(
                query_agent_id=query_agent_id,
                prompt=prompt,
                scope_fn=sim_scope_fn,
                scope_fn_source=scope_fn_source,
                max_calls=max_calls,
                max_tokens=max_tokens,
                return_usage=True,
                replay_tape=replay_tape,
                return_tape=True,
                llm_egress_enabled=llm_egress_enabled,
                room_vault_items=room_vault_items,
            )

        env = {
            "QUERY_PROMPT": req.query,
            "QUERY_AGENT_ID": query_agent_id or "",
            # Pass the privacy/utility policy through so scope can design
            # its scope_fn around the caller's intent. Empty string when
            # no policy is specified.
            "POLICY_CONTEXT": (req.policy or "") if hasattr(req, "policy") else "",
        }
        # Forward ablation/feature toggles into the scope container so that
        # env-var-based experiments set on the server process actually take effect.
        for _toggle in ("HIVEMIND_DISABLE_SIMULATE", "HIVEMIND_DISABLE_SEMLIFT",
                        "HIVEMIND_SCOPE_MAX_ATTEMPTS", "HIVEMIND_SCOPE_MULTI",
                        "HIVEMIND_SCOPE_CI"):
            _val = os.environ.get(_toggle)
            if _val is not None:
                env[_toggle] = _val

        # Scope agents get FULL_READ access, restricted to the room's
        # allowed_tables (None = legacy unrestricted).
        scope_tools = build_sql_tools(
            self.db, AccessLevel.FULL_READ, allowed_tables=allowed_tables,
        )
        scope_tools.extend(
            build_room_vault_tools(
                room_vault_items or [],
                AccessLevel.FULL_READ,
            )
        )

        # Add agent file inspection tools if query agent exists
        if query_agent_id:
            scope_tools.extend(
                build_agent_file_tools(self.agent_store, query_agent_id)
            )

        tool_handlers = {t.name: t.handler for t in scope_tools}

        async def on_tool_call(name: str, args: dict) -> str:
            if name not in tool_handlers:
                return f"Error: unknown tool '{name}'. Available: {', '.join(tool_handlers)}"
            return await asyncio.to_thread(tool_handlers[name], **args)

        # Claude-code default scope agents can read bundled default query
        # source via /workspace/query-agent/ (RO). Hermes scope agents use
        # native bridge file tools instead; mounting a path from inside the
        # core container fails on Phala because the sibling Docker daemon
        # resolves bind sources on the CVM host, not in the core container.
        scope_volumes: dict[str, dict[str, str]] | None = None
        if (
            query_agent_id
            and query_agent_id.startswith("default-")
            and getattr(agent_config, "harness", "claude_code") != "hermes"
        ):
            repo_root = Path(__file__).resolve().parent.parent
            src_dir = repo_root / "agents" / query_agent_id
            if src_dir.is_dir():
                scope_volumes = {
                    str(src_dir): {"bind": "/workspace/query-agent", "mode": "ro"}
                }
                logger.info(
                    "Mounting query agent source %s -> /workspace/query-agent (ro) for scope",
                    src_dir,
                )

        raw, usage = await self._run_agent(
            agent_config=agent_config,
            role="scope",
            env=env,
            tools=scope_tools,
            on_tool_call=on_tool_call,
            agent_store_for_bridge=self.agent_store,
            run_query_fn=run_query_fn,
            scope_query_agent_id=allowed_query_agent_id,
            max_calls=max_calls,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            return_budget_summary=True,
            extra_volumes=scope_volumes,
            model=model,
            provider=provider,
            llm_egress_enabled=llm_egress_enabled,
        )

        try:
            data = _extract_scope_agent_json(raw)

            if "scope_fn" in data:
                source = data["scope_fn"]
                if not isinstance(source, str):
                    raise ValueError("scope_fn must be a string")
                fn = compile_scope_fn(source)
                return fn, source, usage

            raise ValueError(
                "Scope agent must return 'scope_fn'"
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            # Hard-fail on invalid scope_fn. Do NOT fall back to a default
            # policy — the whole point of this agent is to produce a valid
            # scope function, and below-100% validity means the privacy
            # model is broken. Surface the first 600 chars of the offending
            # output for diagnosis.
            _src = ""
            try:
                _parsed = json.loads(raw.strip())
                if isinstance(_parsed, dict):
                    _src = str(_parsed.get("scope_fn", ""))[:600]
            except Exception:
                _src = raw.strip()[:600]
            logger.error(
                "Scope agent output invalid (%s, %d chars). scope_fn preview:\n%s",
                e,
                len(raw),
                _src,
            )
            raise ValueError(f"Scope agent failed: {e}")

    async def _run_query_agent(
        self,
        query_agent_id: str,
        prompt: str,
        scope_fn: Callable | None = None,
        scope_fn_source: str = "",
        max_calls: int | None = None,
        max_tokens: int | None = None,
        timeout_seconds: int | None = None,
        return_usage: bool = False,
        replay_tape: list[dict] | None = None,
        return_tape: bool = False,
        model: str | None = None,
        provider: str | None = None,
        allowed_tables: list[str] | None = None,
        llm_egress_enabled: bool = True,
        room_vault_items: list[dict] | None = None,
    ):
        """Run query agent with SCOPED access, return output and optionally usage/tape."""
        agent_config = await asyncio.to_thread(
            self.agent_store.get, query_agent_id
        )
        if agent_config is None:
            raise ValueError(f"Query agent '{query_agent_id}' not found")

        # Build scoped tools. Pass scope_fn_source so execute_sql routes the
        # scope check through apply_scope_fn — gives the LLM-supplied function
        # a multiprocessing isolation boundary with a hard timeout.
        tools = build_sql_tools(
            self.db,
            AccessLevel.SCOPED,
            scope_fn=scope_fn,
            scope_fn_source=scope_fn_source or None,
            allowed_tables=allowed_tables,
        )
        tools.extend(
            build_room_vault_tools(
                room_vault_items or [],
                AccessLevel.SCOPED,
                scope_fn=scope_fn,
                scope_fn_source=scope_fn_source or None,
            )
        )
        tool_handlers = {t.name: t.handler for t in tools}

        async def on_tool_call(name: str, args: dict) -> str:
            if name not in tool_handlers:
                return f"Error: unknown tool '{name}'. Available: {', '.join(tool_handlers)}"
            return await asyncio.to_thread(tool_handlers[name], **args)

        env = {
            "QUERY_PROMPT": prompt,
            "SCOPE_FN_SOURCE": scope_fn_source or "",
        }

        backend = SandboxBackend(
            self._client_for(provider),
            self._model_for("query", model),
            self._sandbox_settings,
            agent_config,
            agent_store=self.agent_store,
        )

        run_result = await backend.run(
            role="query",
            env=env,
            tools=tools,
            on_tool_call=on_tool_call,
            max_calls=max_calls,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            return_budget_summary=return_usage,
            replay_tape=replay_tape,
            return_tape=return_tape,
            llm_egress_enabled=llm_egress_enabled,
        )

        if return_usage and return_tape:
            output, usage, tape = run_result
            return output, usage, tape
        elif return_usage:
            output, usage = run_result
            return output, usage
        elif return_tape:
            output, tape = run_result
            return output, {}, tape
        return run_result

    async def _run_mediator_agent(
        self,
        mediator_agent_id: str,
        raw_output: str,
        prompt: str,
        max_tokens: int | None = None,
        max_calls: int | None = None,
        timeout_seconds: int | None = None,
        policy: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        llm_egress_enabled: bool = True,
    ) -> tuple[str, dict]:
        """Run mediator agent to filter/audit output. Returns (output, usage)."""
        agent_config = await asyncio.to_thread(
            self.agent_store.get, mediator_agent_id
        )
        if agent_config is None:
            raise ValueError(f"Mediator agent '{mediator_agent_id}' not found")

        env = {
            "RAW_OUTPUT": raw_output,
            "QUERY_PROMPT": prompt,
            "MEDIATION_POLICY": policy or "",
            "HIVEMIND_MEDIATOR_ALWAYS_LLM": (
                "true" if self.settings.mediator_always_llm else "false"
            ),
        }

        # Mediator has NO data access tools
        backend = SandboxBackend(
            self._client_for(provider),
            self._model_for("mediator", model),
            self._sandbox_settings,
            agent_config,
            agent_store=self.agent_store,
        )

        async def noop_tool_call(name: str, args: dict) -> str:
            return "Error: mediator agents have no tool access"

        output, usage = await backend.run(
            role="mediator",
            env=env,
            tools=[],
            on_tool_call=noop_tool_call,
            max_calls=max_calls,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            return_budget_summary=True,
            llm_egress_enabled=llm_egress_enabled,
        )
        return output, usage

    # -- Phase 5: signed run attestation --

    def _sha256_hex(self, value: str) -> str:
        """sha256 over UTF-8 bytes, hex-encoded. Used for the prompt and
        output hashes inside the signed payload — keeps the signature body
        small while still cryptographically committing to the actual text.
        """
        return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()

    def _digests_for(self, agent_id: str | None) -> tuple[str, str]:
        """Return ``(files_digest, attested_files_digest)`` for an agent.

        Empty strings when the agent doesn't exist or has no files.
        Computed on the same content the runtime reads, so a recipient
        re-fetching ``/v1/room-agents/{id}/files`` reproduces both digests.
        """
        if not agent_id:
            return "", ""
        try:
            d = self.agent_store.compute_digests(agent_id)
            return d.get("files_digest", ""), d.get("attested_files_digest", "")
        except Exception:
            return "", ""

    def _build_run_attestation(
        self,
        *,
        run_id: str,
        status: str,
        query_agent_id: str,
        scope_agent_id: str | None,
        prompt: str,
        output: str,
        error: str | None,
        room_id: str | None = None,
        room_manifest_hash: str | None = None,
        output_visibility: str | None = None,
        allowed_llm_providers: list[str] | None = None,
        artifacts_enabled: bool | None = True,
        room_vault_item_count: int = 0,
    ) -> dict | None:
        """Build the signed run attestation envelope, or ``None`` if the
        run signer isn't available (e.g. local dev without dstack).

        Envelope shape:
          {
            "body": {
              "schema_version": 1,
              "run_id": "...",
              "status": "completed" | "failed",
              "compose_hash": "...",
              "scope_agent_id": "...",
              "scope_files_digest": "...",
              "scope_attested_files_digest": "...",
              "query_agent_id": "...",
              "query_files_digest": "...",
              "query_attested_files_digest": "...",
              "prompt_hash": "<sha256>",
              "output_hash": "<sha256>",
              "error_hash": "<sha256>" | "",
              "timestamp": <unix>,
              "signer_pubkey_b64": "..."
            },
            "signature_b64": "...",
            "signer_pubkey_b64": "..."
          }

        ``body`` is what gets canonical-JSON'd and signed; the outer
        envelope re-publishes the pubkey at top level so a recipient
        with the JSONB row alone can reconstruct everything without
        re-fetching ``/v1/attestation``.
        """
        from . import attestation as _att
        from . import run_signer as _rs

        signer = _att.get_run_signer()
        if signer is None:
            return None
        priv, pub_bytes = signer

        bundle = _att.get_bundle()
        compose_hash = ""
        if bundle.get("ready"):
            compose_hash = (bundle.get("attestation") or {}).get(
                "compose_hash", ""
            ) or ""

        scope_full, scope_att = self._digests_for(scope_agent_id)
        query_full, query_att = self._digests_for(query_agent_id)
        pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

        body = {
            "schema_version": 1,
            "run_id": run_id,
            "status": status,
            "compose_hash": compose_hash,
            "room_id": room_id or "",
            "room_manifest_hash": room_manifest_hash or "",
            "scope_agent_id": scope_agent_id or "",
            "scope_files_digest": scope_full,
            "scope_attested_files_digest": scope_att,
            "query_agent_id": query_agent_id,
            "query_files_digest": query_full,
            "query_attested_files_digest": query_att,
            "output_visibility": output_visibility or "",
            "allowed_llm_providers": list(allowed_llm_providers or []),
            "artifacts_enabled": bool(artifacts_enabled),
            "room_vault_item_count": int(room_vault_item_count or 0),
            "prompt_hash": self._sha256_hex(prompt or ""),
            "output_hash": self._sha256_hex(output or ""),
            "error_hash": self._sha256_hex(error) if error else "",
            "timestamp": int(time.time()),
            "signer_pubkey_b64": pub_b64,
        }

        sig_bytes, _ = _rs.sign_payload(priv, body)
        return {
            "body": body,
            "signature_b64": base64.b64encode(sig_bytes).decode("ascii"),
            "signer_pubkey_b64": pub_b64,
        }

    # -- Tracked query agent run (background) --

    async def run_query_agent_tracked(
        self,
        agent_id: str,
        run_id: str,
        run_store,
        artifact_store=None,
        artifact_retention_seconds: int = 86400,
        prompt: str = "",
        scope_agent_id: str | None = None,
        mediator_agent_id: str | None = None,
        max_tokens: int | None = None,
        max_calls: int | None = None,
        timeout_seconds: int | None = None,
        model: str | None = None,
        scope_model: str | None = None,
        query_model: str | None = None,
        mediator_model: str | None = None,
        provider: str | None = None,
        policy: str | None = None,
        room_id: str | None = None,
        room_manifest_hash: str | None = None,
        output_visibility: str = "owner_and_querier",
        allowed_llm_providers: list[str] | None = None,
        artifacts_enabled: bool = True,
        room_vault_items: list[dict] | None = None,
        allowed_tables: list[str] | None = None,
        payer_tenant_id: str | None = None,
        payer_token_id: str | None = None,
        billable_role: str = "query",
        billing_provider: str | None = None,
        billing_model: str | None = None,
        billing_hold_micro_usd: int = 0,
    ) -> None:
        """Run the full 3-stage pipeline with run tracking and artifact upload.

        Stage 0: Scope agent    → produces scope_fn to filter SQL results
        Stage 1: Query agent    → executes with scoped DB access.
        Stage 2: Mediator agent → audits/redacts query output
        Stage 3: Final artifacts → report artifacts are written from the
                                  mediated output so artifacts cannot bypass
                                  the mediator.

        Updates run_store status through the lifecycle:
          pending → running → completed/failed
        """
        usage_total = _new_usage_summary()
        provider_for_billing = billing_provider or provider
        model_for_billing = billing_model or model or self.llm_model
        try:
            await asyncio.to_thread(
                run_store.update_status, run_id, "running"
            )

            # Eagerly resolve egress so a forbidden provider fails before
            # any container starts. ``llm_egress_enabled=False`` keeps
            # non-LLM agents runnable while making bridge LLM endpoints
            # return 403.
            provider, llm_egress_enabled = self._resolve_provider_for_egress(
                provider,
                allowed_llm_providers,
            )
            provider_for_billing = billing_provider or provider
            room_vault_items = list(room_vault_items or [])

            global_max = self._sandbox_settings.global_max_tokens
            requested_max = max_tokens or self.settings.default_query_max_tokens
            effective_max = min(requested_max, global_max)
            remaining = effective_max
            usage_total = _new_usage_summary(effective_max)
            scope_model_override = scope_model or model
            query_model_override = query_model or model
            mediator_model_override = mediator_model or model
            if llm_egress_enabled:
                self._ensure_llm_route_enabled(
                    provider,
                    self._model_for("scope", scope_model_override),
                )
                self._ensure_llm_route_enabled(
                    provider,
                    self._model_for("query", query_model_override),
                )

            # -- Stage 0: Scope resolution --
            scope_fn = None
            scope_fn_source = ""
            resolved_scope_id = scope_agent_id or self.settings.default_scope_agent
            if not resolved_scope_id:
                raise ValueError(
                    "scope_agent_id is required "
                    "(no default scope agent configured)"
                )
            if resolved_scope_id:
                scope_t0 = time.time()
                await asyncio.to_thread(
                    run_store.update_stage, run_id, "scope", started_at=scope_t0,
                )
                req_for_scope = QueryRequest(
                    query=prompt or "analyse data",
                    query_agent_id=agent_id,
                    scope_agent_id=resolved_scope_id,
                    policy=policy,
                )
                try:
                    scope_budget = max(1, int(remaining * SCOPE_BUDGET_FRACTION))
                    resolved_scope_model = self._model_for(
                        "scope", scope_model_override
                    )
                    scope_fn, scope_fn_source, scope_usage = await self._run_scope_agent(
                        req_for_scope, max_tokens=scope_budget,
                        max_calls=max_calls, timeout_seconds=timeout_seconds,
                        model=scope_model_override, provider=provider,
                        llm_egress_enabled=llm_egress_enabled,
                        room_vault_items=room_vault_items,
                        allowed_tables=allowed_tables,
                    )
                    used = scope_usage.get("total_tokens", 0)
                    remaining = max(1, remaining - used)
                    _add_stage_usage(
                        usage_total,
                        "scope",
                        scope_usage,
                        provider=provider,
                        model=resolved_scope_model,
                    )
                finally:
                    await asyncio.to_thread(
                        run_store.update_stage, run_id, "scope",
                        ended_at=time.time(),
                    )
                # Fail-closed: if the operator configured a scope agent for
                # this run, refuse to fall through to SCOPED tools with
                # scope_fn=None — that path passes rows back unfiltered
                # (build_sql_tools.execute_sql gates filtering on
                # ``scope_fn is not None``). A flaky scope LLM, exhausted
                # budget, or malformed scope_fn output must surface as a
                # failed run rather than a silent privacy regression.
                if scope_fn is None:
                    raise ValueError(
                        f"Scope agent '{resolved_scope_id}' did not produce "
                        f"a usable scope_fn; refusing to run query agent "
                        f"with unscoped tool access (fail-closed)."
                    )

            # -- Stage 1: Query agent --
            query_t0 = time.time()
            await asyncio.to_thread(
                run_store.update_stage, run_id, "query", started_at=query_t0,
            )
            agent_config = await asyncio.to_thread(
                self.agent_store.get, agent_id
            )
            if agent_config is None:
                raise ValueError(f"Query agent '{agent_id}' not found")

            resolved_mediator_id = (
                mediator_agent_id or self.settings.default_mediator_agent
            )
            query_max_tokens = remaining
            if resolved_mediator_id and remaining > MEDIATOR_MIN_TOKENS:
                query_max_tokens = max(1, remaining - _mediator_reserve(remaining))

            tools = build_sql_tools(
                self.db,
                AccessLevel.SCOPED,
                scope_fn=scope_fn,
                scope_fn_source=scope_fn_source or None,
                allowed_tables=allowed_tables,
            )
            tools.extend(
                build_room_vault_tools(
                    room_vault_items,
                    AccessLevel.SCOPED,
                    scope_fn=scope_fn,
                    scope_fn_source=scope_fn_source or None,
                )
            )
            tool_handlers = {t.name: t.handler for t in tools}

            async def on_tool_call(name: str, args: dict) -> str:
                if name not in tool_handlers:
                    return (
                        f"Error: unknown tool '{name}'. "
                        f"Available: {', '.join(tool_handlers)}"
                    )
                return await asyncio.to_thread(tool_handlers[name], **args)

            env: dict[str, str] = {}
            if prompt:
                env["QUERY_PROMPT"] = prompt
            env["SCOPE_FN_SOURCE"] = scope_fn_source or ""
            env["HIVEMIND_QUERY_UPLOAD_ARTIFACTS"] = (
                "true"
                if artifacts_enabled and not resolved_mediator_id
                else "false"
            )

            backend = SandboxBackend(
                self._client_for(provider),
                self._model_for("query", query_model_override),
                self._sandbox_settings,
                agent_config,
                agent_store=self.agent_store,
            )

            query_output, query_usage = await backend.run(
                role="query",
                env=env,
                tools=tools,
                on_tool_call=on_tool_call,
                max_calls=max_calls,
                max_tokens=query_max_tokens,
                timeout_seconds=timeout_seconds,
                artifact_store=(
                    artifact_store
                    if artifacts_enabled and not resolved_mediator_id
                    else None
                ),
                artifact_retention_seconds=artifact_retention_seconds,
                run_id=run_id,
                run_store=run_store,
                return_budget_summary=True,
                llm_egress_enabled=llm_egress_enabled,
            )
            used = query_usage.get("total_tokens", 0)
            remaining = max(1, remaining - used)
            _add_stage_usage(
                usage_total,
                "query",
                query_usage,
                provider=provider,
                model=self._model_for("query", query_model_override),
            )
            await asyncio.to_thread(
                run_store.update_stage, run_id, "query",
                ended_at=time.time(),
            )

            # -- Stage 2: Mediator --
            #
            # Observable skip reasons. The fail-closed assertion below
            # turns a "skipped despite pinned" into a hard error, but the
            # operator wants the *why* in the logs even when the skip is
            # legitimate (no mediator pinned, or query produced nothing).
            if not resolved_mediator_id:
                logger.info(
                    "run %s: skipping mediator stage — no mediator pinned "
                    "(req.mediator_agent_id=%r, settings.default_mediator_agent=%r)",
                    run_id, mediator_agent_id, self.settings.default_mediator_agent,
                )
            elif not query_output:
                logger.info(
                    "run %s: skipping mediator stage — query agent produced "
                    "empty output (resolved_mediator_id=%r)",
                    run_id, resolved_mediator_id,
                )
            if resolved_mediator_id and query_output:
                if remaining < MEDIATOR_MIN_TOKENS:
                    raise ValueError(
                        f"Mediator '{resolved_mediator_id}' could not run "
                        f"for run {run_id}: insufficient remaining budget "
                        f"({remaining} < {MEDIATOR_MIN_TOKENS}). Refusing "
                        "to return unmediated output."
                    )
                mediator_t0 = time.time()
                await asyncio.to_thread(
                    run_store.update_stage, run_id, "mediator",
                    started_at=mediator_t0,
                )
                try:
                    resolved_mediator_model = self._model_for(
                        "mediator", mediator_model_override
                    )
                    if llm_egress_enabled:
                        self._ensure_llm_route_enabled(
                            provider,
                            resolved_mediator_model,
                        )
                    query_output, mediator_usage = await self._run_mediator_agent(
                        mediator_agent_id=resolved_mediator_id,
                        raw_output=query_output,
                        prompt=prompt,
                        max_tokens=remaining,
                        max_calls=max_calls,
                        timeout_seconds=timeout_seconds,
                        policy=policy,
                        model=mediator_model_override,
                        provider=provider,
                        llm_egress_enabled=llm_egress_enabled,
                    )
                    _add_stage_usage(
                        usage_total,
                        "mediator",
                        mediator_usage,
                        provider=provider,
                        model=resolved_mediator_model,
                    )
                except ValueError as e:
                    if "not found" in str(e).lower():
                        raise
                    raise ValueError(
                        f"Mediator '{resolved_mediator_id}' failed for run "
                        f"{run_id}; refusing to return unmediated output"
                    ) from e
                finally:
                    await asyncio.to_thread(
                        run_store.update_stage, run_id, "mediator",
                        ended_at=time.time(),
                    )

            # Defense in depth: if a mediator was pinned for this run
            # (room manifest mediator.agent_id, or the operator-side
            # default_mediator_agent), it MUST have run before we reach
            # this point. The mediator is the load-bearing output filter
            # — if it silently skipped (resolved_mediator_id was None at
            # the resolution step above, or the guard at the Stage-2
            # block fell through), we are about to release unfiltered
            # query output. Refuse.
            #
            # Reaching this assertion = a real bug, not an edge case.
            # Possible root causes that have shown up in practice:
            #   - room.manifest.mediator.agent_id was set, but
            #     apply_room_to_query_request didn't propagate it into
            #     qreq.mediator_agent_id (e.g., manifest schema drift).
            #   - settings.default_mediator_agent was unset and the room
            #     was created without an explicit --mediator-agent.
            # Either way, fail closed: the user pinned a mediator; we owe
            # them mediation or an explicit error.
            mediator_pinned = bool(
                mediator_agent_id or self.settings.default_mediator_agent
            )
            if mediator_pinned:
                run_check = await asyncio.to_thread(
                    run_store.get, run_id,
                )
                if not run_check or not run_check.get("mediator_started_at"):
                    raise ValueError(
                        f"Mediator '{resolved_mediator_id}' was pinned but "
                        f"did not run for run {run_id}. Refusing to release "
                        "unmediated output. This is a server-side bug — "
                        "include the run_id and the room manifest hash when "
                        "filing it."
                    )

            await asyncio.to_thread(
                _store_report_artifacts_if_needed,
                artifact_store=artifact_store,
                artifacts_enabled=artifacts_enabled,
                run_id=run_id,
                prompt=prompt,
                final_output=query_output or "",
            )

            output_cap = max(0, int(self.settings.max_run_output_chars))
            final_output = (query_output or "")[:output_cap]
            attestation_envelope = await asyncio.to_thread(
                self._build_run_attestation,
                run_id=run_id,
                status="completed",
                query_agent_id=agent_id,
                scope_agent_id=resolved_scope_id,
                prompt=prompt,
                output=final_output,
                error=None,
                room_id=room_id,
                room_manifest_hash=room_manifest_hash,
                output_visibility=output_visibility,
                allowed_llm_providers=allowed_llm_providers,
                artifacts_enabled=artifacts_enabled,
                room_vault_item_count=len(room_vault_items),
            )
            await self._record_run_usage(
                run_store,
                run_id,
                usage_total,
                payer_tenant_id=payer_tenant_id,
                payer_token_id=payer_token_id,
                billable_role=billable_role,
                billing_hold_micro_usd=billing_hold_micro_usd,
                default_provider=provider_for_billing,
                default_model=model_for_billing,
            )
            await asyncio.to_thread(
                run_store.update_status, run_id, "completed",
                output=final_output,
                attestation=attestation_envelope,
            )

        except Exception as e:
            logger.error("Tracked query run %s failed: %s", run_id, e)
            try:
                err_str = str(e)[:500]
                attestation_envelope = await asyncio.to_thread(
                    self._build_run_attestation,
                    run_id=run_id,
                    status="failed",
                    query_agent_id=agent_id,
                    scope_agent_id=scope_agent_id
                    or self.settings.default_scope_agent,
                    prompt=prompt,
                    output="",
                    error=err_str,
                    room_id=room_id,
                    room_manifest_hash=room_manifest_hash,
                    output_visibility=output_visibility,
                    allowed_llm_providers=allowed_llm_providers,
                    artifacts_enabled=artifacts_enabled,
                    room_vault_item_count=len(room_vault_items or []),
                )
                await asyncio.to_thread(
                    run_store.update_status, run_id, "failed",
                    error=err_str,
                    attestation=attestation_envelope,
                )
                await self._record_run_usage(
                    run_store,
                    run_id,
                    usage_total,
                    payer_tenant_id=payer_tenant_id,
                    payer_token_id=payer_token_id,
                    billable_role=billable_role,
                    billing_hold_micro_usd=billing_hold_micro_usd,
                    default_provider=provider_for_billing,
                    default_model=model_for_billing,
                )
            except Exception:
                logger.warning("Failed to update run %s to failed", run_id)

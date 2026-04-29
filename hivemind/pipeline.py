import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

import httpx
from openai import AsyncOpenAI

from .config import Settings
from .db import Database
from .models import (
    IndexRequest,
    IndexResponse,
    QueryRequest,
    QueryResponse,
    StoreRequest,
    StoreResponse,
)
from .sandbox.agents import AgentStore
from .sandbox.backend import SandboxBackend
from .sandbox.models import AgentConfig
from .sandbox.settings import build_sandbox_settings
from .scope import compile_scope_fn
from .tools import AccessLevel, build_agent_file_tools, build_sql_tools

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


def _mediator_reserve(remaining: int) -> int:
    """Return how many tokens to reserve from the query stage for mediator.

    Proportional to remaining budget (capped by MEDIATOR_TOKEN_RESERVE),
    floored at MEDIATOR_MIN_TOKENS, and never so large that the query
    stage would drop below MEDIATOR_MIN_TOKENS itself.
    """
    desired = min(MEDIATOR_TOKEN_RESERVE, int(remaining * MEDIATOR_RESERVE_FRACTION))
    desired = max(MEDIATOR_MIN_TOKENS, desired)
    return min(desired, max(0, remaining - MEDIATOR_MIN_TOKENS))


class Pipeline:
    """Orchestrates store and query pipelines using Docker agent sandboxes."""

    def __init__(self, settings: Settings, db: Database, agent_store: AgentStore):
        self.settings = settings
        self.db = db
        self.agent_store = agent_store
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
            "index": (settings.index_model or settings.llm_model),
        }
        self._sandbox_settings = build_sandbox_settings(settings)

    def _model_for(self, role: str, override: str | None = None) -> str:
        """Resolve the LLM model for a given role.

        Caller-supplied ``override`` wins; otherwise per-role config; otherwise
        the global ``llm_model``.
        """
        if override:
            return override
        return self._role_models.get(role, self.llm_model)

    def _client_for(self, provider: str | None) -> AsyncOpenAI:
        """Resolve which AsyncOpenAI client to use for a request.

        ``None`` / empty / ``"openrouter"`` → default client. ``"tinfoil"``
        → tinfoil client when configured. Any other value, or tinfoil
        without an API key, raises ``ValueError`` so the failure is loud
        at request boundaries instead of silently falling back.
        """
        key = (provider or "").strip().lower()
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
        effective_max = min(req.max_tokens or global_max, global_max)
        remaining = effective_max
        total_tokens = 0
        mediator_agent_id = req.mediator_agent_id or self.settings.default_mediator_agent

        # Stage 0: Scope resolution → produces a scope_fn
        scope_fn = None
        scope_fn_source = ""
        scope_budget = max(1, int(remaining * SCOPE_BUDGET_FRACTION))

        req_timeout = req.timeout_seconds
        req_max_calls = req.max_llm_calls
        req_model = req.model
        req_provider = req.provider
        # Eagerly resolve so an unknown provider fails the whole request
        # before we burn scope-stage budget.
        self._client_for(req_provider)

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
                model=req_model, provider=req_provider,
            )
            used = scope_usage.get("total_tokens", 0)
            total_tokens += used
            remaining = max(1, remaining - used)
        elif self.settings.default_scope_agent:
            scope_fn, scope_fn_source, scope_usage = await self._run_scope_agent(
                req, max_tokens=scope_budget,
                max_calls=req_max_calls, timeout_seconds=req_timeout,
                model=req_model, provider=req_provider,
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
            model=req_model,
            provider=req_provider,
        )
        used = query_usage.get("total_tokens", 0)
        total_tokens += used
        remaining = max(1, remaining - used)

        # Stage 2: Optional mediator
        mediated = False
        if mediator_agent_id:
            if remaining < MEDIATOR_MIN_TOKENS:
                logger.info(
                    "Skipping mediator '%s': insufficient remaining budget (%d < %d)",
                    mediator_agent_id,
                    remaining,
                    MEDIATOR_MIN_TOKENS,
                )
            else:
                try:
                    output, mediator_usage = await self._run_mediator_agent(
                        mediator_agent_id=mediator_agent_id,
                        raw_output=output,
                        prompt=req.query,
                        max_tokens=remaining,
                        max_calls=req_max_calls,
                        timeout_seconds=req_timeout,
                        policy=req.policy,
                        model=req_model,
                        provider=req_provider,
                    )
                except ValueError as e:
                    if "not found" in str(e).lower():
                        raise
                    logger.warning(
                        "Mediator '%s' failed; returning unmediated output: %s",
                        mediator_agent_id,
                        e,
                    )
                else:
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
        )

    async def _run_scope_agent(
        self,
        req: QueryRequest,
        max_tokens: int | None = None,
        max_calls: int | None = None,
        timeout_seconds: int | None = None,
        model: str | None = None,
        provider: str | None = None,
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

        # Scope agents get FULL_READ access
        scope_tools = build_sql_tools(self.db, AccessLevel.FULL_READ)

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

        # iter 19 experiment: filesystem mount RE-ENABLED.
        # Scope can read query agent source via /workspace/query-agent/ (RO).
        # Enables the save/load-NPC workflow: read the query agent's prompt,
        # draft a scope_fn, simulate the query agent via play.py, revise.
        scope_volumes: dict[str, dict[str, str]] | None = None
        if query_agent_id and query_agent_id.startswith("default-"):
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
        )

        try:
            data = json.loads(raw.strip())

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
        )
        return output, usage

    # -- Index pipeline --

    async def run_index(self, req: IndexRequest) -> IndexResponse:
        """Run the index pipeline: index agent processes document data."""
        global_max = self._sandbox_settings.global_max_tokens
        effective_max = min(req.max_tokens or global_max, global_max)
        # Eager validation so an unknown provider fails fast.
        self._client_for(req.provider)

        index_text, metadata, usage = await self._run_index_agent(
            req=req,
            max_tokens=effective_max,
            max_calls=req.max_llm_calls,
            timeout_seconds=req.timeout_seconds,
            model=req.model,
            provider=req.provider,
        )

        usage["max_tokens"] = effective_max
        return IndexResponse(index_text=index_text, metadata=metadata, usage=usage)

    async def _run_index_agent(
        self,
        req: IndexRequest,
        max_tokens: int | None = None,
        max_calls: int | None = None,
        timeout_seconds: int | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> tuple[str, dict, dict]:
        """Run index agent with FULL_READWRITE access. Returns (index_text, metadata, usage)."""
        index_agent_id = req.index_agent_id or self.settings.default_index_agent
        if not index_agent_id:
            raise ValueError(
                "No index agent specified and no default configured"
            )

        agent_config = await asyncio.to_thread(
            self.agent_store.get, index_agent_id
        )
        if agent_config is None:
            raise ValueError(f"Index agent '{index_agent_id}' not found")

        tools = build_sql_tools(self.db, AccessLevel.FULL_READWRITE)
        tool_handlers = {t.name: t.handler for t in tools}

        async def on_tool_call(name: str, args: dict) -> str:
            if name not in tool_handlers:
                return f"Error: unknown tool '{name}'. Available: {', '.join(tool_handlers)}"
            return await asyncio.to_thread(tool_handlers[name], **args)

        env = {
            "DOCUMENT_DATA": req.data,
            "DOCUMENT_METADATA": json.dumps(req.metadata),
        }

        raw, usage = await self._run_agent(
            agent_config=agent_config,
            role="index",
            env=env,
            tools=tools,
            on_tool_call=on_tool_call,
            max_calls=max_calls,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            return_budget_summary=True,
            model=model,
            provider=provider,
        )

        try:
            data = json.loads(raw.strip())
            index_text = data.get("index_text")
            metadata = data.get("metadata")
            if not isinstance(index_text, str) or index_text == "":
                raise ValueError("index_text must be a non-empty string")
            if not isinstance(metadata, dict):
                raise ValueError("metadata must be a dict")
            return index_text, metadata, usage
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(
                "Index agent output not valid JSON (%s, %d chars)", e, len(raw)
            )
            raise ValueError(f"Index agent failed: {e}")
        except ValueError:
            raise

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
        re-fetching ``/v1/agents/{id}/files`` reproduces both digests.
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
            "scope_agent_id": scope_agent_id or "",
            "scope_files_digest": scope_full,
            "scope_attested_files_digest": scope_att,
            "query_agent_id": query_agent_id,
            "query_files_digest": query_full,
            "query_attested_files_digest": query_att,
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
        provider: str | None = None,
    ) -> None:
        """Run the full 3-stage pipeline with run tracking and artifact upload.

        Stage 0: Scope agent    → produces scope_fn to filter SQL results
        Stage 1: Query agent    → executes with scoped DB access; may POST
                                  /sandbox/artifact-upload → writes straight to
                                  _hivemind_query_artifacts (no S3)
        Stage 2: Mediator agent → audits/redacts query output

        Updates run_store status through the lifecycle:
          pending → running → completed/failed
        """
        try:
            await asyncio.to_thread(
                run_store.update_status, run_id, "running"
            )

            # Eager validation so an unknown provider fails the whole run
            # before any container starts.
            self._client_for(provider)

            global_max = self._sandbox_settings.global_max_tokens
            effective_max = min(max_tokens or global_max, global_max)
            remaining = effective_max

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
                )
                try:
                    scope_budget = max(1, int(remaining * SCOPE_BUDGET_FRACTION))
                    scope_fn, scope_fn_source, scope_usage = await self._run_scope_agent(
                        req_for_scope, max_tokens=scope_budget,
                        max_calls=max_calls, timeout_seconds=timeout_seconds,
                        model=model, provider=provider,
                    )
                    used = scope_usage.get("total_tokens", 0)
                    remaining = max(1, remaining - used)
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

            backend = SandboxBackend(
                self._client_for(provider),
                self._model_for("query", model),
                self._sandbox_settings,
                agent_config,
            )

            query_output, query_usage = await backend.run(
                role="query",
                env=env,
                tools=tools,
                on_tool_call=on_tool_call,
                max_calls=max_calls,
                max_tokens=query_max_tokens,
                timeout_seconds=timeout_seconds,
                artifact_store=artifact_store,
                artifact_retention_seconds=artifact_retention_seconds,
                run_id=run_id,
                run_store=run_store,
                return_budget_summary=True,
            )
            used = query_usage.get("total_tokens", 0)
            remaining = max(1, remaining - used)
            await asyncio.to_thread(
                run_store.update_stage, run_id, "query",
                ended_at=time.time(),
            )

            # -- Stage 2: Mediator --
            if resolved_mediator_id and query_output:
                if remaining < MEDIATOR_MIN_TOKENS:
                    logger.info(
                        "Skipping mediator '%s' for run %s: "
                        "insufficient budget (%d < %d)",
                        resolved_mediator_id, run_id,
                        remaining, MEDIATOR_MIN_TOKENS,
                    )
                else:
                    mediator_t0 = time.time()
                    await asyncio.to_thread(
                        run_store.update_stage, run_id, "mediator",
                        started_at=mediator_t0,
                    )
                    try:
                        query_output, _ = await self._run_mediator_agent(
                            mediator_agent_id=resolved_mediator_id,
                            raw_output=query_output,
                            prompt=prompt,
                            max_tokens=remaining,
                            max_calls=max_calls,
                            timeout_seconds=timeout_seconds,
                            model=model,
                            provider=provider,
                        )
                    except ValueError as e:
                        if "not found" in str(e).lower():
                            raise
                        logger.warning(
                            "Mediator '%s' failed for run %s; "
                            "returning unmediated output: %s",
                            resolved_mediator_id, run_id, e,
                        )
                    finally:
                        await asyncio.to_thread(
                            run_store.update_stage, run_id, "mediator",
                            ended_at=time.time(),
                        )

            # Artifacts (if any) were written straight to Postgres during
            # the query-agent turn via /sandbox/artifact-upload. No post-
            # mediator commit needed — failed runs are swept by the TTL
            # job in hivemind.core.
            final_output = (query_output or "")[:10000]
            attestation_envelope = await asyncio.to_thread(
                self._build_run_attestation,
                run_id=run_id,
                status="completed",
                query_agent_id=agent_id,
                scope_agent_id=resolved_scope_id,
                prompt=prompt,
                output=final_output,
                error=None,
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
                )
                await asyncio.to_thread(
                    run_store.update_status, run_id, "failed",
                    error=err_str,
                    attestation=attestation_envelope,
                )
            except Exception:
                logger.warning("Failed to update run %s to failed", run_id)

    # -- Tracked index agent run (background) --

    async def run_index_tracked(
        self,
        index_agent_id: str,
        run_id: str,
        run_store,
        document_data: str,
        document_metadata: str,
        max_tokens: int | None = None,
        max_calls: int | None = None,
        timeout_seconds: int | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> None:
        """Run index agent with tracking. Updates run_store through lifecycle."""
        try:
            index_t0 = time.time()
            await asyncio.to_thread(
                run_store.update_stage, run_id, "index", started_at=index_t0,
            )

            req = IndexRequest(
                data=document_data,
                metadata=json.loads(document_metadata) if document_metadata else {},
                index_agent_id=index_agent_id,
                max_tokens=max_tokens,
                max_llm_calls=max_calls,
                timeout_seconds=timeout_seconds,
                model=model,
                provider=provider,
            )
            # Eager validation so an unknown provider fails before container start.
            self._client_for(provider)
            index_text, metadata, usage = await self._run_index_agent(
                req=req, max_tokens=max_tokens,
                max_calls=max_calls, timeout_seconds=timeout_seconds,
                model=model, provider=provider,
            )

            await asyncio.to_thread(
                run_store.update_stage, run_id, "index", ended_at=time.time(),
            )
            await asyncio.to_thread(
                run_store.update_index_output, run_id,
                json.dumps({"index_text": index_text, "metadata": metadata})[:10000],
            )
        except Exception as e:
            logger.error("Tracked index run %s failed: %s", run_id, e)
            await asyncio.to_thread(
                run_store.update_stage, run_id, "index", ended_at=time.time(),
            )
            raise

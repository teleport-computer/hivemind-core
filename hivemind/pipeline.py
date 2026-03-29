import asyncio
import json
import logging
import time
from typing import Callable

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
MEDIATOR_TOKEN_RESERVE = 512


class Pipeline:
    """Orchestrates store and query pipelines using Docker agent sandboxes."""

    def __init__(self, settings: Settings, db: Database, agent_store: AgentStore):
        self.settings = settings
        self.db = db
        self.agent_store = agent_store
        self.llm_client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_seconds,
        )
        self.llm_model = settings.llm_model
        self._sandbox_settings = build_sandbox_settings(settings)

    # -- Store pipeline --

    async def run_store(self, req: StoreRequest) -> StoreResponse:
        """Execute raw SQL via the store pipeline.

        If the SQL is a SELECT, return rows. Otherwise commit and return rowcount.
        Optionally runs an index agent for pre-processing.
        """
        from .tools import _is_select_only

        sql = req.sql
        params = req.params

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

        if req.scope_agent_id:
            scope_fn, scope_usage = await self._run_scope_agent(
                req, max_tokens=remaining,
            )
            used = scope_usage.get("total_tokens", 0)
            total_tokens += used
            remaining = max(1, remaining - used)
        elif self.settings.default_scope_agent:
            scope_fn, scope_usage = await self._run_scope_agent(
                req, max_tokens=remaining,
            )
            used = scope_usage.get("total_tokens", 0)
            total_tokens += used
            remaining = max(1, remaining - used)

        # Stage 1: Query agent
        query_agent_id = req.query_agent_id or self.settings.default_query_agent
        if not query_agent_id:
            raise ValueError(
                "No query agent specified and no default configured"
            )

        query_max_tokens = remaining
        if mediator_agent_id and remaining > MEDIATOR_MIN_TOKENS:
            reserve = min(
                MEDIATOR_TOKEN_RESERVE,
                max(0, remaining - MEDIATOR_MIN_TOKENS),
            )
            query_max_tokens = max(1, remaining - reserve)

        output, query_usage = await self._run_query_agent(
            query_agent_id=query_agent_id,
            prompt=req.query,
            scope_fn=scope_fn,
            max_tokens=query_max_tokens,
            return_usage=True,
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
        return_budget_summary: bool = False,
        replay_tape: list[dict] | None = None,
        return_tape: bool = False,
    ):
        """Run a Docker agent with tools and return its stdout."""
        backend = SandboxBackend(
            self.llm_client,
            self.llm_model,
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
            return_budget_summary=return_budget_summary,
            replay_tape=replay_tape,
            return_tape=return_tape,
        )

    async def _run_scope_agent(
        self,
        req: QueryRequest,
        max_tokens: int | None = None,
    ) -> tuple[Callable, dict]:
        """Run scope agent to produce a scope function. Returns (scope_fn, usage)."""
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
                max_calls=max_calls,
                max_tokens=max_tokens,
                return_usage=True,
                replay_tape=replay_tape,
                return_tape=True,
            )

        env = {
            "QUERY_PROMPT": req.query,
            "QUERY_AGENT_ID": query_agent_id or "",
        }

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

        raw, usage = await self._run_agent(
            agent_config=agent_config,
            role="scope",
            env=env,
            tools=scope_tools,
            on_tool_call=on_tool_call,
            agent_store_for_bridge=self.agent_store,
            run_query_fn=run_query_fn,
            scope_query_agent_id=allowed_query_agent_id,
            max_tokens=max_tokens,
            return_budget_summary=True,
        )

        try:
            data = json.loads(raw.strip())

            if "scope_fn" in data:
                source = data["scope_fn"]
                if not isinstance(source, str):
                    raise ValueError("scope_fn must be a string")
                fn = compile_scope_fn(source)
                return fn, usage

            raise ValueError(
                "Scope agent must return 'scope_fn'"
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.error(
                "Scope agent output not valid (%s, %d chars)",
                e,
                len(raw),
            )
            raise ValueError(f"Scope agent failed: {e}")

    async def _run_query_agent(
        self,
        query_agent_id: str,
        prompt: str,
        scope_fn: Callable | None = None,
        max_calls: int | None = None,
        max_tokens: int | None = None,
        return_usage: bool = False,
        replay_tape: list[dict] | None = None,
        return_tape: bool = False,
    ):
        """Run query agent with SCOPED access, return output and optionally usage/tape."""
        agent_config = await asyncio.to_thread(
            self.agent_store.get, query_agent_id
        )
        if agent_config is None:
            raise ValueError(f"Query agent '{query_agent_id}' not found")

        # Build scoped tools
        tools = build_sql_tools(self.db, AccessLevel.SCOPED, scope_fn=scope_fn)
        tool_handlers = {t.name: t.handler for t in tools}

        async def on_tool_call(name: str, args: dict) -> str:
            if name not in tool_handlers:
                return f"Error: unknown tool '{name}'. Available: {', '.join(tool_handlers)}"
            return await asyncio.to_thread(tool_handlers[name], **args)

        env = {
            "QUERY_PROMPT": prompt,
        }

        backend = SandboxBackend(
            self.llm_client,
            self.llm_model,
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
        }

        # Mediator has NO data access tools
        backend = SandboxBackend(
            self.llm_client,
            self.llm_model,
            self._sandbox_settings,
            agent_config,
            agent_store=self.agent_store,
        )

        async def noop_tool_call(name: str, args: dict) -> str:
            return "Error: mediator agents have no tool access"

        return await backend.run(
            role="mediator",
            env=env,
            tools=[],
            on_tool_call=noop_tool_call,
            max_tokens=max_tokens,
            return_budget_summary=True,
        )

    # -- Index pipeline --

    async def run_index(self, req: IndexRequest) -> IndexResponse:
        """Run the index pipeline: index agent processes document data."""
        global_max = self._sandbox_settings.global_max_tokens
        effective_max = min(req.max_tokens or global_max, global_max)

        index_text, metadata, usage = await self._run_index_agent(
            req=req,
            max_tokens=effective_max,
        )

        usage["max_tokens"] = effective_max
        return IndexResponse(index_text=index_text, metadata=metadata, usage=usage)

    async def _run_index_agent(
        self,
        req: IndexRequest,
        max_tokens: int | None = None,
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
            max_tokens=max_tokens,
            return_budget_summary=True,
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

    # -- Tracked query agent run (background) --

    async def run_query_agent_tracked(
        self,
        agent_id: str,
        run_id: str,
        run_store,
        s3_uploader=None,
        prompt: str = "",
        scope_agent_id: str | None = None,
        mediator_agent_id: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        """Run the full 3-stage pipeline with run tracking and S3 upload.

        Stage 0: Scope agent   → produces scope_fn to filter SQL results
        Stage 1: Query agent   → executes with scoped DB access + S3 upload
        Stage 2: Mediator agent → audits/redacts query output

        Updates run_store status through the lifecycle:
          pending → running → completed/failed
        """
        try:
            await asyncio.to_thread(
                run_store.update_status, run_id, "running"
            )

            global_max = self._sandbox_settings.global_max_tokens
            effective_max = min(max_tokens or global_max, global_max)
            remaining = effective_max

            # -- Stage 0: Scope resolution --
            scope_fn = None
            resolved_scope_id = scope_agent_id or self.settings.default_scope_agent
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
                    scope_fn, scope_usage = await self._run_scope_agent(
                        req_for_scope, max_tokens=remaining,
                    )
                    used = scope_usage.get("total_tokens", 0)
                    remaining = max(1, remaining - used)
                except ValueError as e:
                    logger.warning(
                        "Scope agent '%s' failed for tracked run %s; "
                        "continuing without scope: %s",
                        resolved_scope_id, run_id, e,
                    )
                finally:
                    await asyncio.to_thread(
                        run_store.update_stage, run_id, "scope",
                        ended_at=time.time(),
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
                reserve = min(
                    MEDIATOR_TOKEN_RESERVE,
                    max(0, remaining - MEDIATOR_MIN_TOKENS),
                )
                query_max_tokens = max(1, remaining - reserve)

            tools = build_sql_tools(
                self.db, AccessLevel.SCOPED, scope_fn=scope_fn,
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

            backend = SandboxBackend(
                self.llm_client,
                self.llm_model,
                self._sandbox_settings,
                agent_config,
            )

            result = await backend.run(
                role="query",
                env=env,
                tools=tools,
                on_tool_call=on_tool_call,
                max_tokens=query_max_tokens,
                s3_uploader=s3_uploader,
                run_id=run_id,
                run_store=run_store,
                return_budget_summary=True,
            )
            query_output, query_usage = result
            used = query_usage.get("total_tokens", 0)
            remaining = max(1, remaining - used)
            await asyncio.to_thread(
                run_store.update_stage, run_id, "query",
                ended_at=time.time(),
            )

            # -- Stage 2: Mediator --
            mediator_ran = False
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
                    mediator_ran = True
                    try:
                        query_output, _ = await self._run_mediator_agent(
                            mediator_agent_id=resolved_mediator_id,
                            raw_output=query_output,
                            prompt=prompt,
                            max_tokens=remaining,
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

            # Mark completed + save output.
            # If agent already uploaded to S3 (bridge set status=completed),
            # just update output. Otherwise mark completed.
            current = await asyncio.to_thread(run_store.get, run_id)
            if current and current["status"] == "running":
                await asyncio.to_thread(
                    run_store.update_status, run_id, "completed",
                    output=(query_output or "")[:10000],
                )
            elif current and query_output:
                # S3 upload already marked completed; just save output
                await asyncio.to_thread(
                    run_store.update_status, run_id, current["status"],
                    output=(query_output or "")[:10000],
                )

        except Exception as e:
            logger.error("Tracked query run %s failed: %s", run_id, e)
            try:
                await asyncio.to_thread(
                    run_store.update_status, run_id, "failed",
                    error=str(e)[:500],
                )
            except Exception:
                logger.warning("Failed to update run %s to failed", run_id)

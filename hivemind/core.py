import json
import logging
from datetime import datetime
from uuid import uuid4

from openai import AsyncOpenAI

from .backends import create_backend
from .config import Settings
from .enclave import run_query
from .indexing import generate_index, hyde_expand
from .prompts import build_query_system
from .models import (
    IndexEntry,
    QueryRequest,
    QueryResponse,
    Scope,
    SoftConstraints,
    StoreRequest,
    StoreResponse,
)
from .storage import Storage

logger = logging.getLogger(__name__)


class Hivemind:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.storage = Storage(settings.db_path, settings.encryption_key)
        self.client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=settings.openrouter_api_key,
        )
        self.backend = create_backend(settings, self.client)

        # Sandbox (optional)
        self.agent_store = None
        if settings.sandbox_enabled:
            from .sandbox.agents import AgentStore

            self.agent_store = AgentStore(self.storage._conn)

    async def store(self, req: StoreRequest) -> StoreResponse:
        record_id = uuid4().hex[:12]
        ts = datetime.now()

        self.storage.write_record(
            id=record_id,
            text=req.text,
            space_id=req.space_id,
            user_id=req.user_id,
            timestamp=ts.timestamp(),
            metadata=req.metadata,
        )

        if req.index_agent_id and self.agent_store:
            agent_config = self.agent_store.get(req.index_agent_id)
            if agent_config is None:
                raise ValueError(f"Indexing agent '{req.index_agent_id}' not found")
            index = await self._run_indexing_agent(
                req.text, req.user_id, agent_config
            )
        elif req.index:
            index = req.index
        else:
            index = await generate_index(
                self.client, req.text, self.settings.index_model
            )

        self.storage.write_index(
            record_id=record_id,
            title=index.title,
            summary=index.summary,
            tags=",".join(index.tags),
            key_claims=",".join(index.key_claims),
            extra=json.dumps(index.extra),
            timestamp=ts.timestamp(),
        )

        return StoreResponse(record_id=record_id, timestamp=ts, index=index)

    async def query(self, req: QueryRequest) -> QueryResponse:
        # Resolve query agent config (needed by scoping agent)
        query_agent_config = None
        if req.query_agent_id and self.agent_store:
            query_agent_config = self.agent_store.get(req.query_agent_id)
            if query_agent_config is None:
                raise ValueError(f"Agent '{req.query_agent_id}' not found")

        # Stage 0: Scope resolution (scoping agent can see query agent info)
        scope = await self._resolve_scope(req, query_agent_config)

        # Determine backend + system prompt
        if query_agent_config:
            from .sandbox import SandboxBackend

            backend = SandboxBackend(
                self.client, self.settings.openrouter_model,
                self._sandbox_settings(), query_agent_config,
            )
            system = ""  # sandbox agents control their own LLM interactions
        else:
            backend = self.backend
            system = await self._build_default_system(req)

        # Stages 1-3: tools → agent → mediator
        return await run_query(
            question=req.question,
            context=req.context,
            scope=scope,
            system=system,
            soft=req.soft,
            querier_id=req.querier_id,
            storage=self.storage,
            backend=backend,
            mediator_client=self.client,
            mediator_model=self.settings.mediator_model,
        )

    async def _build_default_system(self, req: QueryRequest) -> str:
        """Build system prompt for the default query agent (with HyDE)."""
        system = build_query_system(req.soft)

        if self.settings.hyde_enabled:
            try:
                hyde_hint = await hyde_expand(
                    self.client, req.question, req.context,
                    self.settings.mediator_model,
                )
                system += (
                    "\n\nSearch hint (use vocabulary from this when "
                    "formulating search queries): " + hyde_hint
                )
            except Exception as e:
                logger.warning("HyDE expansion failed, skipping: %s", e)

        return system

    async def _resolve_scope(
        self, req: QueryRequest, query_agent_config=None
    ) -> Scope:
        """Resolve query scope: custom agent or static.

        Priority:
          1. scope_agent_id set → run sandboxed scoping agent
          2. otherwise → use scope from request (default: Scope() = see all)
        """
        if req.scope_agent_id:
            if not self.agent_store:
                raise ValueError("Scoping agents require sandbox to be enabled")
            agent_config = self.agent_store.get(req.scope_agent_id)
            if agent_config is None:
                raise ValueError(
                    f"Scoping agent '{req.scope_agent_id}' not found"
                )
            return await self._run_scoping_agent(
                req.question, req.querier_id, agent_config,
                query_agent_config,
            )

        return req.scope

    async def _run_scoping_agent(
        self, question: str, querier_id: str | None,
        agent_config, query_agent_config=None,
    ) -> Scope:
        """Run a sandboxed scoping agent to determine query scope.

        The scoping agent receives:
          - PROMPT: the query question
          - QUERIER_ID: who is asking
          - QUERY_AGENT_ID: which query agent will process the data
          - QUERY_AGENT_IMAGE: Docker image of the query agent
          - Full DB access via tools (Scope() — no restrictions)
          - LLM access via /llm/chat
          - Source inspection tools: list_query_agent_files, read_query_agent_file

        It outputs JSON to stdout: {"record_ids": ["abc123", "def456", ...]}
        This whitelist becomes the query agent's entire visible universe.
        """
        query_agent_id = query_agent_config.agent_id if query_agent_config else None
        backend, tools, on_tool_call = self._build_sandbox(
            scope=Scope(), agent_config=agent_config,
            query_agent_id=query_agent_id,
        )

        extra_env = {}
        if querier_id:
            extra_env["QUERIER_ID"] = querier_id

        # Let the scoping agent see the query agent's identity
        if query_agent_config:
            extra_env["QUERY_AGENT_ID"] = query_agent_config.agent_id
            extra_env["QUERY_AGENT_IMAGE"] = query_agent_config.image
        else:
            extra_env["QUERY_AGENT_ID"] = "default"

        raw_output = await backend.run(
            question, "", tools, on_tool_call, extra_env=extra_env or None
        )

        try:
            data = json.loads(raw_output.strip())
            record_ids = data.get("record_ids", [])
            if not isinstance(record_ids, list):
                raise ValueError("record_ids must be a list")
            return Scope(record_ids=record_ids)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.error(
                "Scoping agent output is not valid JSON: %s", raw_output[:200]
            )
            raise ValueError(f"Scoping agent failed to produce valid JSON: {e}")

    async def _run_indexing_agent(
        self, text: str, user_id: str | None, agent_config
    ) -> IndexEntry:
        """Run a sandboxed indexing agent with access to the user's data.

        The agent receives DOCUMENT_TEXT (the text being indexed) and scoped
        tools (search_index, read_record, list_index) limited to the user's
        records. It outputs JSON to stdout: {title, summary, tags, key_claims, extra}.
        """
        scope = Scope(user_ids=[user_id]) if user_id else Scope()
        backend, tools, on_tool_call = self._build_sandbox(
            scope=scope, agent_config=agent_config
        )

        raw_output = await backend.run(text, "", tools, on_tool_call)

        try:
            data = json.loads(raw_output.strip())
            return IndexEntry(
                title=data.get("title", "Untitled"),
                summary=data.get("summary", ""),
                tags=data.get("tags", []),
                key_claims=data.get("key_claims", []),
                extra=data.get("extra", {}),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error(
                "Indexing agent output is not valid JSON: %s", raw_output[:200]
            )
            raise ValueError(f"Indexing agent failed to produce valid JSON: {e}")

    def _sandbox_settings(self):
        """Build SandboxSettings from config. Single source of truth."""
        from .sandbox import SandboxSettings

        return SandboxSettings(
            bridge_host=self.settings.sandbox_bridge_host,
            docker_network_name=self.settings.sandbox_docker_network,
            container_memory_mb=self.settings.sandbox_container_memory_mb,
            container_cpu_quota=self.settings.sandbox_container_cpu_quota,
            global_max_llm_calls=self.settings.sandbox_max_llm_calls,
            global_max_tokens=self.settings.sandbox_max_tokens,
            global_timeout_seconds=self.settings.sandbox_timeout,
        )

    def _build_sandbox(
        self, scope: Scope, agent_config, query_agent_id: str | None = None
    ):
        """Build a sandbox backend + tools for an agent.

        When query_agent_id is set, the agent gets extra tools to inspect
        the query agent's source code (for scoping agents only).

        Returns (backend, tools, on_tool_call).
        """
        from .sandbox import SandboxBackend
        from .tools import build_agent_file_tools, build_tools

        tools = build_tools(self.storage, scope)

        if query_agent_id and self.agent_store:
            tools.extend(build_agent_file_tools(self.agent_store, query_agent_id))

        tool_handlers = {t.name: t.handler for t in tools}

        async def on_tool_call(name: str, args: dict) -> str:
            if name not in tool_handlers:
                return f"Error: unknown tool '{name}'. Available: {', '.join(tool_handlers)}"
            return tool_handlers[name](**args)

        backend = SandboxBackend(
            self.client, self.settings.openrouter_model,
            self._sandbox_settings(), agent_config,
        )

        return backend, tools, on_tool_call

    def health(self) -> dict:
        return {
            "status": "ok",
            "record_count": self.storage.count_records(),
            "version": "0.1.0",
        }

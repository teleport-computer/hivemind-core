import asyncio
import base64
import io
import logging
import secrets
import tarfile
from typing import Callable

from openai import AsyncOpenAI

from ..tools import Tool
from .bridge import BridgeServer
from .budget import Budget
from .models import AgentConfig, SandboxSettings

logger = logging.getLogger(__name__)

# Image used for ephemeral query CVMs when source is bundled via env var
PHALA_QUERY_BASE_IMAGE = "ghcr.io/account-link/hivemind-query-base:latest"


def _bundle_source_b64(files: dict[str, str]) -> str:
    """Create a base64-encoded tar.gz archive from a dict of {path: content}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode("ascii")


# Role → SandboxSettings URL field mapping for persistent CVMs
_PERSISTENT_ROLE_URL_FIELDS = {
    "scope": "phala_scope_url",
    "index": "phala_index_url",
    "mediator": "phala_mediator_url",
}


def _create_runner(settings: SandboxSettings, role: str = "query"):
    """Create the appropriate runner based on backend setting and role.

    In Phala mode:
      - scope/index/mediator with a configured URL → PersistentCvmRunner
      - query (or roles without a URL) → PhalaRunner (ephemeral CVM)
    In Docker mode:
      - Always DockerRunner
    """
    if settings.backend == "phala":
        url_field = _PERSISTENT_ROLE_URL_FIELDS.get(role, "")
        persistent_url = getattr(settings, url_field, "") if url_field else ""
        if persistent_url:
            from .persistent_runner import PersistentCvmRunner

            return PersistentCvmRunner(persistent_url)
        else:
            from .phala_runner import PhalaRunner

            return PhalaRunner(settings)
    else:
        from .docker_runner import DockerRunner

        return DockerRunner(settings)


class SandboxBackend:
    """Runs agent images in isolated containers (Docker or Phala CVM).

    Each invocation:
      1. Starts a BridgeServer (LLM proxy + tool endpoints)
      2. Runs the agent container via the configured backend
      3. Captures stdout as the agent's output
      4. Tears everything down
    """

    def __init__(
        self,
        llm_client: AsyncOpenAI,
        llm_model: str,
        settings: SandboxSettings,
        agent: AgentConfig,
        agent_store=None,
    ):
        self.llm_client = llm_client
        self.llm_model = llm_model
        self.settings = settings
        self.agent = agent
        self.agent_store = agent_store

    async def _llm_caller(
        self, messages: list[dict], max_tokens: int,
        model: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> dict:
        """Passthrough proxy: forward agent's LLM calls to the provider."""
        kwargs: dict = {
            "model": model or self.llm_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
        if tools is not None:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice

        resp = await self.llm_client.chat.completions.create(**kwargs)

        choice = resp.choices[0]
        result: dict = {
            "content": choice.message.content or "",
            "usage": {},
            "finish_reason": choice.finish_reason,
        }

        if choice.message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in choice.message.tool_calls
            ]

        if hasattr(resp, "usage") and resp.usage:
            result["usage"] = {
                "prompt_tokens": resp.usage.prompt_tokens or 0,
                "completion_tokens": resp.usage.completion_tokens or 0,
            }

        return result

    async def run(
        self,
        role: str,
        env: dict[str, str],
        tools: list[Tool],
        on_tool_call: Callable,
        agent_store=None,
        run_query_fn: Callable | None = None,
        scope_query_agent_id: str | None = None,
        max_calls: int | None = None,
        max_tokens: int | None = None,
        return_budget_summary: bool = False,
        replay_tape: list[dict] | None = None,
        return_tape: bool = False,
        s3_uploader=None,
        run_id: str | None = None,
        run_store=None,
    ) -> str | tuple:
        """Run the agent container and return its stdout output."""
        agent = self.agent
        is_phala = self.settings.backend == "phala"
        runner = _create_runner(self.settings, role)

        # Resolve budget: min of agent config and global caps
        resolved_max_calls = min(
            max_calls or agent.max_llm_calls,
            self.settings.global_max_llm_calls,
        )
        resolved_max_tokens = min(
            max_tokens or agent.max_tokens,
            self.settings.global_max_tokens,
        )
        timeout = min(agent.timeout_seconds, self.settings.global_timeout_seconds)
        # Phala CVMs can take 3-5min to boot; enforce minimum 600s timeout
        if is_phala:
            timeout = max(timeout, 600)
        memory_mb = min(agent.memory_mb, self.settings.container_memory_mb)

        agent = agent.model_copy(
            update={"timeout_seconds": timeout, "memory_mb": memory_mb}
        )

        budget = Budget(max_calls=resolved_max_calls, max_tokens=resolved_max_tokens)
        session_token = secrets.token_urlsafe(32)

        # Bridge mode: "mounted" for Phala (via dispatcher), "standalone" for Docker
        bridge_mode = "mounted" if is_phala else "standalone"

        bridge = BridgeServer(
            session_token=session_token,
            tools=tools,
            on_tool_call=on_tool_call,
            llm_caller=self._llm_caller,
            budget=budget,
            host=self.settings.bridge_host,
            role=role,
            agent_store=agent_store,
            run_query_fn=run_query_fn,
            scope_query_agent_id=scope_query_agent_id,
            replay_tape=replay_tape,
            s3_uploader=s3_uploader,
            run_id=run_id,
            run_store=run_store,
            mode=bridge_mode,
        )

        try:
            port = await bridge.start()

            if is_phala:
                # In Phala mode, bridge is mounted on the main server.
                # The public URL serves as the bridge URL for the CVM.
                public_url = self.settings.phala_public_url.rstrip("/")
                bridge_url = f"{public_url}/bridge"
            else:
                bridge_url = f"http://{self.settings.bridge_host}:{port}"

            # In Phala mode with ephemeral CVM: bundle agent source into env
            # so the query-base image can extract and run it at boot time.
            if is_phala and role == "query" and self.agent_store:
                from .phala_runner import PhalaRunner

                if isinstance(runner, PhalaRunner):
                    files = await asyncio.to_thread(
                        self.agent_store.get_files, agent.agent_id,
                    )
                    if files:
                        source_b64 = _bundle_source_b64(files)
                        env = {
                            **env,
                            "AGENT_SOURCE_B64": source_b64,
                            "AGENT_ENTRYPOINT": agent.entrypoint or "python agent.py",
                        }
                        # Override image to the query-base (has boot.sh + SDK deps)
                        agent = agent.model_copy(
                            update={"image": PHALA_QUERY_BASE_IMAGE}
                        )

            # Add bridge connection info to env
            full_env = {
                "BRIDGE_URL": bridge_url,
                "SESSION_TOKEN": session_token,
                "AGENT_ROLE": role,
                "BUDGET_MAX_TOKENS": str(resolved_max_tokens),
                "BUDGET_MAX_CALLS": str(resolved_max_calls),
                # OpenAI SDK auto-routing
                "OPENAI_BASE_URL": f"{bridge_url}/v1",
                "OPENAI_API_KEY": session_token,
                # Anthropic SDK auto-routing
                "ANTHROPIC_BASE_URL": bridge_url,
                "ANTHROPIC_API_KEY": session_token,
                **({"RUN_ID": run_id} if run_id else {}),
                **env,
            }

            result = await runner.run_agent(
                agent=agent,
                bridge_url=bridge_url,
                session_token=session_token,
                env=full_env,
            )

            logger.info(
                "Sandbox agent %s finished: exit=%d, timed_out=%s, budget=%s",
                agent.agent_id,
                result.exit_code,
                result.timed_out,
                budget.summary(),
            )

            if result.exit_code != 0:
                if result.timed_out:
                    raise ValueError(
                        f"Agent '{agent.agent_id}' timed out after {agent.timeout_seconds}s"
                    )
                details = (result.stderr or result.stdout or "").strip()
                details = details[:400] if details else "no details"
                raise ValueError(
                    f"Agent '{agent.agent_id}' failed (exit_code={result.exit_code}): {details}"
                )

            if result.stderr:
                logger.warning("Agent stderr: %s", result.stderr[:500])

            output = result.stdout.strip()
            if not output:
                if result.timed_out:
                    output = "(Agent timed out without producing output)"
                else:
                    output = "(Agent produced no output)"

            tape_data = bridge.get_recorded_tape() if return_tape else None

            if return_budget_summary and return_tape:
                return output, budget.summary(), tape_data
            elif return_budget_summary:
                return output, budget.summary()
            elif return_tape:
                return output, tape_data
            return output

        finally:
            await bridge.stop()

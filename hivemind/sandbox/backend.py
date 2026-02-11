import logging
import secrets
import tempfile
from typing import Callable

from openai import AsyncOpenAI

from ..tools import Tool
from .bridge import BridgeServer
from .budget import Budget
from .docker_runner import DockerRunner
from .models import AgentConfig, SandboxSettings

logger = logging.getLogger(__name__)


class SandboxBackend:
    """Backend that runs user-uploaded agent Docker images in isolated containers.

    Implements the same run() interface as OpenRouterBackend:
        async def run(prompt, system, tools, on_tool_call) -> str

    Each invocation:
      1. Starts a BridgeServer (LLM proxy + tool endpoints) on an ephemeral port
      2. Runs the agent as a Docker container on an internal network
      3. Captures stdout as the agent's output
      4. Tears everything down

    The agent can only communicate via the bridge — the Docker network is
    internal (no internet access) and the container has no host filesystem access.
    """

    def __init__(
        self,
        llm_client: AsyncOpenAI,
        llm_model: str,
        settings: SandboxSettings,
        agent: AgentConfig,
    ):
        self.llm_client = llm_client
        self.llm_model = llm_model
        self.settings = settings
        self.agent = agent
        self.runner = DockerRunner(settings)

    async def _llm_caller(
        self, messages: list[dict], max_tokens: int,
        model: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> dict:
        """Passthrough proxy: forward agent's LLM calls to OpenRouter.

        The agent controls model, messages, and parameters.
        We just forward and return the result.
        """
        kwargs: dict = {
            "model": model or self.llm_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p

        resp = await self.llm_client.chat.completions.create(**kwargs)

        choice = resp.choices[0]
        result: dict = {
            "content": choice.message.content or "",
            "usage": {},
        }

        if hasattr(resp, "usage") and resp.usage:
            result["usage"] = {
                "prompt_tokens": resp.usage.prompt_tokens or 0,
                "completion_tokens": resp.usage.completion_tokens or 0,
            }

        return result

    async def run(
        self,
        prompt: str,
        system: str,
        tools: list[Tool],
        on_tool_call: Callable,
        extra_env: dict[str, str] | None = None,
    ) -> str:
        agent = self.agent

        # Resolve budget: min of agent config and global caps
        max_calls = min(agent.max_llm_calls, self.settings.global_max_llm_calls)
        max_tokens = min(agent.max_tokens, self.settings.global_max_tokens)
        timeout = min(agent.timeout_seconds, self.settings.global_timeout_seconds)

        # Override agent timeout with resolved value
        agent = agent.model_copy(update={"timeout_seconds": timeout})

        budget = Budget(max_calls=max_calls, max_tokens=max_tokens)
        session_token = secrets.token_urlsafe(32)
        work_dir = tempfile.mkdtemp(prefix="hm-")

        bridge = BridgeServer(
            session_token=session_token,
            tools=tools,
            on_tool_call=on_tool_call,
            llm_caller=self._llm_caller,
            budget=budget,
            host=self.settings.bridge_host,
        )

        try:
            port = await bridge.start()
            bridge_url = f"http://{self.settings.bridge_host}:{port}"

            result = await self.runner.run_agent(
                agent=agent,
                prompt=prompt,
                bridge_url=bridge_url,
                session_token=session_token,
                work_dir=work_dir,
                extra_env=extra_env,
            )

            logger.info(
                "Sandbox agent %s finished: exit=%d, timed_out=%s, budget=%s",
                agent.agent_id,
                result.exit_code,
                result.timed_out,
                budget.summary(),
            )

            if result.stderr:
                logger.warning("Agent stderr: %s", result.stderr[:500])

            output = result.stdout.strip()
            if not output:
                if result.timed_out:
                    output = "(Agent timed out without producing output)"
                else:
                    output = "(Agent produced no output)"

            return output

        finally:
            await bridge.stop()

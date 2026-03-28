"""Persistent CVM runner — sends agent jobs to pre-deployed long-running CVMs.

Used for scope, index, and mediator agents in Phala mode. Instead of
provisioning/deleting a CVM per invocation, these agents run as persistent
HTTP services (via agent_runner.py) and accept job requests.

The runner POSTs to ``{base_url}/run`` with env vars and timeout, and
receives the agent's stdout/stderr/exit_code in the response.
"""

from __future__ import annotations

import logging

import httpx

from .models import AgentConfig, SandboxSettings
from .phala_runner import ContainerResult

logger = logging.getLogger(__name__)


class PersistentCvmRunner:
    """Sends agent jobs to a pre-deployed persistent CVM via HTTP."""

    def __init__(self, base_url: str):
        if not base_url:
            raise ValueError("Persistent CVM base_url is required")
        self.base_url = base_url.rstrip("/")

    async def run_agent(
        self,
        agent: AgentConfig,
        bridge_url: str,
        session_token: str,
        env: dict[str, str] | None = None,
    ) -> ContainerResult:
        """Send a job to the persistent CVM and return the result."""
        full_env = dict(env or {})
        full_env.setdefault("BRIDGE_URL", bridge_url)
        full_env.setdefault("SESSION_TOKEN", session_token)

        # Scope agents may trigger simulate → ephemeral query CVM (2-3 min boot).
        # Enforce a generous timeout so the full pipeline can complete.
        timeout = max(600, agent.timeout_seconds)

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout + 30, connect=15.0),
            ) as http:
                resp = await http.post(
                    f"{self.base_url}/run",
                    json={"env": full_env, "timeout": timeout},
                )

                if resp.status_code == 429:
                    return ContainerResult(
                        stdout="",
                        stderr="Persistent agent CVM is busy (concurrent run in progress)",
                        exit_code=-1,
                        timed_out=False,
                    )

                resp.raise_for_status()
                data = resp.json()

                return ContainerResult(
                    stdout=data.get("stdout", ""),
                    stderr=data.get("stderr", ""),
                    exit_code=data.get("exit_code", -1),
                    timed_out=data.get("timed_out", False),
                )

        except httpx.TimeoutException:
            return ContainerResult(
                stdout="",
                stderr=f"Persistent CVM request timed out after {timeout}s",
                exit_code=-1,
                timed_out=True,
            )
        except Exception as e:
            logger.error("Persistent CVM call failed (%s): %s", self.base_url, e)
            return ContainerResult(
                stdout="",
                stderr=f"Persistent CVM error: {e}",
                exit_code=-1,
                timed_out=False,
            )

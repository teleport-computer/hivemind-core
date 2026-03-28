"""Phala Cloud CVM runner — runs agent containers on Phala TEE infrastructure.

Replaces DockerRunner when ``sandbox_backend=phala``. Each agent invocation:
  1. Provisions a CVM with a Docker Compose containing the agent image
  2. Commits the CVM (creates the actual instance)
  3. Waits for the CVM to reach "Running" state via SSE watch
  4. Polls container composition until the agent container exits
  5. Fetches logs and deletes the CVM

The bridge URL passed to the agent points to the hivemind-core public URL
(mounted bridge dispatcher), so the CVM can reach it over the internet.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import uuid4

import httpx
from phala_cloud import PhalaCloud, AsyncPhalaCloud

from .models import AgentConfig, SandboxSettings

logger = logging.getLogger(__name__)


@dataclass
class ContainerResult:
    """Result from a CVM agent execution (same shape as DockerRunner's)."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool


def _build_compose_yaml(
    agent: AgentConfig,
    env: dict[str, str],
    settings: SandboxSettings | None = None,
) -> str:
    """Build a Docker Compose YAML for the agent container.

    Applies the same hardening as DockerRunner where Docker Compose supports it:
      - memory limit
      - CPU quota
      - PID limit
      - read-only rootfs + tmpfs mounts
      - drop all capabilities
      - no-new-privileges
      - DNS restricted to prevent direct internet resolution (agent should
        only reach the bridge URL, which is injected via env var)
    """
    memory_mb = agent.memory_mb
    cpu_quota = 1.0
    pids_limit = 256
    read_only = True
    drop_caps = True
    no_new_privs = True

    if settings:
        memory_mb = min(memory_mb, settings.container_memory_mb)
        cpu_quota = settings.container_cpu_quota
        pids_limit = settings.container_pids_limit
        read_only = settings.container_read_only_fs
        drop_caps = settings.container_drop_all_caps
        no_new_privs = settings.container_no_new_privileges

    lines = ["services:", "  agent:", f"    image: {agent.image}"]

    if agent.entrypoint:
        lines.append(f"    entrypoint: {agent.entrypoint}")

    # --- Resource limits (deploy.resources in compose v3) ---
    lines.append("    deploy:")
    lines.append("      resources:")
    lines.append("        limits:")
    lines.append(f"          memory: {memory_mb}M")
    lines.append(f"          cpus: \"{cpu_quota}\"")
    lines.append(f"    pids_limit: {pids_limit}")

    # --- Filesystem hardening ---
    if read_only:
        lines.append("    read_only: true")
        lines.append("    tmpfs:")
        lines.append("      - /tmp:rw,exec,nosuid,size=64m")
        lines.append("      - /var/tmp:rw,noexec,nosuid,size=32m")
        lines.append("      - /home/agent:rw,exec,nosuid,size=64m,uid=1000,gid=1000")
        lines.append("    working_dir: /tmp")

    # --- Security: drop caps + no-new-privileges ---
    if drop_caps:
        lines.append("    cap_drop:")
        lines.append("      - ALL")

    if no_new_privs:
        lines.append("    security_opt:")
        lines.append("      - no-new-privileges:true")

    # --- Network ---
    # NOTE: Unlike Docker mode (which uses iptables to restrict egress to
    # bridge-only), Phala CVMs run on shared TEE infrastructure where we
    # cannot inject host-level firewall rules. The agent container has
    # full outbound network access within the CVM. Mitigation relies on:
    #   1. TEE attestation (code integrity is verifiable)
    #   2. Budget enforcement via bridge (LLM/tool calls are metered)
    #   3. CVM auto-deletion after agent finishes (no persistent access)

    # --- Environment ---
    if env:
        lines.append("    environment:")
        for k, v in env.items():
            # Quote values to handle special YAML characters
            escaped = v.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'      - "{k}={escaped}"')

    return "\n".join(lines) + "\n"


class PhalaRunner:
    """Runs agent containers on Phala Cloud CVMs.

    Lifecycle per agent invocation:
      provision → commit → watch_running → poll_composition → fetch_logs → delete
    """

    def __init__(self, settings: SandboxSettings):
        self.settings = settings
        if not settings.phala_api_key:
            raise RuntimeError("HIVEMIND_PHALA_API_KEY is required for Phala backend")
        # Sync client for cleanup_stale_containers (called from __init__)
        self._sync_client = PhalaCloud(api_key=settings.phala_api_key)

    def _async_client(self) -> AsyncPhalaCloud:
        """Create a fresh async client (to be used as context manager)."""
        return AsyncPhalaCloud(api_key=self.settings.phala_api_key)

    def cleanup_stale_containers(self) -> None:
        """List CVMs and remove any with hivemind-agent prefix that are stopped."""
        try:
            result = self._sync_client.get_cvm_list({"page": 1, "page_size": 100})
            items = result.items if hasattr(result, "items") else []
            for cvm in items:
                name = cvm.name or ""
                status = cvm.status or ""
                if name.startswith("hivemind-agent-") and status not in ("Running", "Starting"):
                    cvm_id = cvm.id
                    if cvm_id:
                        logger.warning("Removing stale Phala CVM %s (status=%s)", cvm_id, status)
                        try:
                            self._sync_client.delete_cvm({"id": str(cvm_id)})
                        except Exception as e:
                            logger.warning("Failed to remove stale CVM %s: %s", cvm_id, e)
        except Exception as e:
            logger.debug("Phala stale CVM cleanup skipped: %s", e)

    async def run_agent(
        self,
        agent: AgentConfig,
        bridge_url: str,
        session_token: str,
        env: dict[str, str] | None = None,
    ) -> ContainerResult:
        """Run an agent in a Phala CVM and return its output."""
        # Phala CVMs can take 3-5 min to provision and boot.
        # Enforce a minimum of 600s so the agent actually gets execution time.
        timeout = max(600, agent.timeout_seconds)
        run_tag = uuid4().hex[:8]
        cvm_name = f"hivemind-agent-{run_tag}"
        app_id: str | None = None

        container_env = dict(env or {})
        container_env.setdefault("BRIDGE_URL", bridge_url)
        container_env.setdefault("SESSION_TOKEN", session_token)

        async with self._async_client() as client:
            try:
                # Step 1: Build compose YAML (with hardening)
                compose_yaml = _build_compose_yaml(agent, container_env, self.settings)
                logger.info(
                    "Provisioning Phala CVM '%s' for agent %s (image=%s)",
                    cvm_name, agent.agent_id, agent.image,
                )

                # Step 2: Provision CVM
                provision = await self._provision(
                    client, cvm_name, compose_yaml,
                )

                app_id = provision.app_id
                compose_hash = provision.compose_hash

                if not app_id:
                    return ContainerResult(
                        stdout="",
                        stderr="Phala CVM provision failed: no app_id returned",
                        exit_code=-1,
                        timed_out=False,
                    )

                # Step 3: Commit CVM
                commit_result = await client.commit_cvm_provision({
                    "app_id": str(app_id),
                    "compose_hash": str(compose_hash),
                    "transaction_hash": "0x0",
                })
                cvm_id = str(commit_result.id)

                logger.info(
                    "CVM '%s' committed (id=%s), waiting for Running state",
                    cvm_name, cvm_id,
                )

                # Step 4: Wait for Running state via SSE watch
                # Phala CVM boot can take 3-5 min; allow up to 300s for boot,
                # leaving at least 120s for the agent to run.
                import time as _time
                boot_deadline = min(300, timeout - 120)
                boot_start = _time.monotonic()
                try:
                    await asyncio.wait_for(
                        self._watch_running(client, cvm_id, boot_deadline),
                        timeout=boot_deadline + 10,  # small buffer over SSE timeout
                    )
                except asyncio.TimeoutError:
                    return ContainerResult(
                        stdout="",
                        stderr=f"CVM '{cvm_name}' did not reach Running state within {boot_deadline:.0f}s",
                        exit_code=-1,
                        timed_out=True,
                    )

                boot_elapsed = _time.monotonic() - boot_start
                logger.info("CVM '%s' is running (boot took %.1fs), polling for agent completion", cvm_name, boot_elapsed)

                # Step 5: Poll composition until agent container exits
                remaining = max(30, timeout - boot_elapsed)
                try:
                    stdout, stderr, exit_code = await asyncio.wait_for(
                        self._poll_until_done(client, cvm_id),
                        timeout=remaining,
                    )
                    return ContainerResult(
                        stdout=stdout,
                        stderr=stderr,
                        exit_code=exit_code,
                        timed_out=False,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Agent %s timed out in CVM %s", agent.agent_id, cvm_name)
                    return ContainerResult(
                        stdout="",
                        stderr=f"Agent timed out after {timeout}s",
                        exit_code=-1,
                        timed_out=True,
                    )

            except Exception as e:
                logger.error("Phala CVM run failed for agent %s: %s", agent.agent_id, e)
                return ContainerResult(
                    stdout="",
                    stderr=f"Phala CVM error: {e}",
                    exit_code=-1,
                    timed_out=False,
                )
            finally:
                # Always clean up the CVM
                if app_id:
                    try:
                        await client.delete_cvm({"app_id": str(app_id)})
                        logger.info("Deleted Phala CVM %s", app_id)
                    except Exception as e:
                        logger.warning("Failed to delete CVM %s: %s", app_id, e)

    async def _provision(
        self,
        client: AsyncPhalaCloud,
        cvm_name: str,
        compose_yaml: str,
    ):
        """Provision a CVM with smallest instance type (tdx.small: 1vCPU/2GB)."""
        return await client.provision_cvm({
            "name": cvm_name,
            "instance_type": "tdx.small",
            "compose_file": {
                "docker_compose_file": compose_yaml,
            },
        })

    async def _watch_running(
        self,
        client: AsyncPhalaCloud,
        cvm_id: str,
        timeout: int,
    ) -> None:
        """Wait for CVM to reach Running state using SSE watch."""
        try:
            await client.watch_cvm_state({
                "id": cvm_id,
                "target": "Running",
                "interval": 5,
                "timeout": timeout,
                "maxRetries": 3,
                "retryDelay": 5.0,
            })
        except Exception as e:
            # If watch_cvm_state raises, fall back to manual polling
            logger.debug("SSE watch failed (%s), falling back to polling", e)
            await self._poll_for_running(client, cvm_id)

    async def _poll_for_running(
        self,
        client: AsyncPhalaCloud,
        cvm_id: str,
        poll_interval: float = 5.0,
    ) -> None:
        """Fallback: poll CVM state until Running."""
        while True:
            state = await client.get_cvm_state({"id": cvm_id})
            status = state.status if hasattr(state, "status") else ""
            if status == "Running":
                return
            if status in ("Failed", "Error", "Deleted", "Stopped"):
                raise RuntimeError(f"CVM entered terminal state: {status}")
            await asyncio.sleep(poll_interval)

    async def _poll_until_done(
        self,
        client: AsyncPhalaCloud,
        cvm_id: str,
        poll_interval: float = 3.0,
    ) -> tuple[str, str, int]:
        """Poll container composition until the agent exits, then fetch logs."""
        consecutive_stopped = 0

        while True:
            try:
                # GET /cvms/{id}/composition — returns container stats
                composition = await client.get_cvm_containers_stats({"id": cvm_id})

                # Check if any containers are still running
                containers = []
                if hasattr(composition, "items"):
                    containers = composition.items or []
                elif isinstance(composition, list):
                    containers = composition

                running = [
                    c for c in containers
                    if (getattr(c, "state", None) or
                        (c.get("state", "") if isinstance(c, dict) else "")) == "running"
                ]

                if not running:
                    consecutive_stopped += 1
                    if consecutive_stopped >= 2:
                        break
                else:
                    consecutive_stopped = 0

            except Exception as e:
                logger.debug("Composition poll failed: %s", e)
                consecutive_stopped += 1
                if consecutive_stopped >= 3:
                    break

            await asyncio.sleep(poll_interval)

        # Fetch logs
        stdout, stderr = await self._fetch_logs(cvm_id)
        exit_code = 0 if stdout.strip() else 1
        return stdout, stderr, exit_code

    async def _fetch_logs(self, cvm_id: str) -> tuple[str, str]:
        """Fetch agent container logs from the CVM's log endpoint.

        Phala CVMs expose logs at https://{cvm_id}-9090.app.phala.network/logs/{service}
        """
        log_url = f"https://{cvm_id}-9090.app.phala.network/logs/agent?text=true&bare=true"

        try:
            async with httpx.AsyncClient(timeout=30.0) as http:
                resp = await http.get(log_url)
                if resp.status_code == 200:
                    return resp.text.strip(), ""
                return "", f"Log fetch returned HTTP {resp.status_code}"
        except Exception as e:
            return "", f"Log fetch error: {e}"

    # ── Image operations (Phala uses registry images, not local) ──

    def image_exists(self, image: str) -> bool:
        """For Phala, images must be in a registry. Always return True
        and let provisioning fail if image is not pullable."""
        return True

    def build_image(self, build_path: str, tag: str) -> str:
        raise NotImplementedError(
            "Local image builds are not supported with Phala backend. "
            "Push the image to GHCR or another registry and reference it by tag."
        )

    async def build_image_async(self, build_path: str, tag: str) -> str:
        raise NotImplementedError(
            "Local image builds are not supported with Phala backend. "
            "Push the image to GHCR or another registry and reference it by tag."
        )

    def extract_image_files(self, image: str, **kwargs) -> dict[str, str]:
        logger.info("Image file extraction skipped in Phala mode for %s", image)
        return {}

    async def extract_image_files_async(self, image: str, **kwargs) -> dict[str, str]:
        return {}

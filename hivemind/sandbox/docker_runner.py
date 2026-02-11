import asyncio
import io
import logging
import platform
import tarfile
from dataclasses import dataclass

import docker
import docker.errors

from .models import AgentConfig, SandboxSettings

logger = logging.getLogger(__name__)

CONTAINER_LABEL = "managed-by"
CONTAINER_LABEL_VALUE = "hivemind"


@dataclass
class ContainerResult:
    """Result from a Docker container agent execution."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool


class DockerRunner:
    """Runs agent Docker containers with network isolation and resource limits.

    The container:
      - Joins an internal-only Docker network (no internet)
      - Receives bridge URL + session token via env vars
      - Has memory and CPU limits enforced
      - Is always cleaned up (removed) after execution
    """

    def __init__(self, settings: SandboxSettings):
        self.settings = settings
        self._client: docker.DockerClient | None = None
        self._network_id: str | None = None
        self._cleanup_orphans()

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def _cleanup_orphans(self):
        """Remove any containers from previous crashes."""
        try:
            client = self._get_client()
            orphans = client.containers.list(
                all=True,
                filters={"label": f"{CONTAINER_LABEL}={CONTAINER_LABEL_VALUE}"},
            )
            for c in orphans:
                logger.warning("Removing orphan container %s", c.short_id)
                c.remove(force=True)
        except Exception as e:
            logger.debug("Orphan cleanup skipped: %s", e)

    def _ensure_network(self) -> str:
        """Create or get the internal Docker network. Returns network name."""
        client = self._get_client()
        name = self.settings.docker_network_name

        if self._network_id:
            return name

        try:
            network = client.networks.get(name)
            self._network_id = network.id
        except docker.errors.NotFound:
            network = client.networks.create(
                name,
                driver="bridge",
                internal=True,
                labels={CONTAINER_LABEL: CONTAINER_LABEL_VALUE},
            )
            self._network_id = network.id
            logger.info("Created Docker network %s (internal=True)", name)

        return name

    def _resolve_bridge_url(self, port: int) -> str:
        """Build the bridge URL that containers can reach.

        On macOS/Windows: host.docker.internal resolves to the host.
        On Linux: use the Docker network gateway IP.
        """
        if platform.system() in ("Darwin", "Windows"):
            return f"http://host.docker.internal:{port}"

        # Linux: get gateway IP from the network
        try:
            client = self._get_client()
            network = client.networks.get(self.settings.docker_network_name)
            gateway = network.attrs["IPAM"]["Config"][0]["Gateway"]
            return f"http://{gateway}:{port}"
        except (KeyError, IndexError, docker.errors.NotFound):
            # Fallback — 172.17.0.1 is the default Docker bridge gateway
            return f"http://172.17.0.1:{port}"

    async def run_agent(
        self,
        agent: AgentConfig,
        prompt: str,
        bridge_url: str,
        session_token: str,
        work_dir: str,
        extra_env: dict[str, str] | None = None,
    ) -> ContainerResult:
        """Run an agent Docker container.

        The bridge_url passed in is the host-side URL. We resolve a
        container-reachable URL internally.
        """
        # Parse port from bridge_url
        from urllib.parse import urlparse

        parsed = urlparse(bridge_url)
        port = parsed.port or 80
        container_bridge_url = self._resolve_bridge_url(port)

        env = {
            "BRIDGE_URL": container_bridge_url,
            "SESSION_TOKEN": session_token,
            "PROMPT": prompt,
            "DOCUMENT_TEXT": prompt,  # alias for indexing agents
        }
        if extra_env:
            env.update(extra_env)

        network_name = await asyncio.to_thread(self._ensure_network)

        # Resource limits
        mem_limit = f"{agent.memory_mb}m"
        nano_cpus = int(self.settings.container_cpu_quota * 1e9)

        # Build container kwargs
        run_kwargs: dict = {
            "image": agent.image,
            "environment": env,
            "network": network_name,
            "mem_limit": mem_limit,
            "nano_cpus": nano_cpus,
            "labels": {CONTAINER_LABEL: CONTAINER_LABEL_VALUE},
            "detach": True,
            "stdout": True,
            "stderr": True,
        }

        if agent.entrypoint:
            run_kwargs["entrypoint"] = agent.entrypoint

        # Add extra host for macOS/Windows so host.docker.internal resolves
        if platform.system() in ("Darwin", "Windows"):
            run_kwargs["extra_hosts"] = {"host.docker.internal": "host-gateway"}

        container = None
        try:
            container = await asyncio.to_thread(
                self._get_client().containers.run, **run_kwargs
            )

            logger.info(
                "Started container %s for agent %s (image=%s, timeout=%ds)",
                container.short_id,
                agent.agent_id,
                agent.image,
                agent.timeout_seconds,
            )

            # Wait for container to finish
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(container.wait),
                    timeout=agent.timeout_seconds,
                )
                exit_code = result.get("StatusCode", -1)
                timed_out = False
            except asyncio.TimeoutError:
                logger.warning(
                    "Agent %s timed out after %ds, killing container %s",
                    agent.agent_id,
                    agent.timeout_seconds,
                    container.short_id,
                )
                await asyncio.to_thread(container.kill)
                exit_code = -1
                timed_out = True

            # Capture output
            stdout = await asyncio.to_thread(
                container.logs, stdout=True, stderr=False
            )
            stderr = await asyncio.to_thread(
                container.logs, stdout=False, stderr=True
            )

            stdout_str = stdout.decode(errors="replace") if isinstance(stdout, bytes) else str(stdout)
            stderr_str = stderr.decode(errors="replace") if isinstance(stderr, bytes) else str(stderr)

            # Detect OOM kill
            if exit_code == 137 and not timed_out:
                stderr_str += "\n(Container was killed — likely OOM)"

            return ContainerResult(
                stdout=stdout_str,
                stderr=stderr_str,
                exit_code=exit_code,
                timed_out=timed_out,
            )

        except docker.errors.ImageNotFound:
            return ContainerResult(
                stdout="",
                stderr=f"Docker image not found: {agent.image}",
                exit_code=-1,
                timed_out=False,
            )

        finally:
            if container:
                try:
                    await asyncio.to_thread(container.remove, force=True)
                except Exception as e:
                    logger.warning("Failed to remove container: %s", e)

    # ── Image filesystem extraction ──

    # Directories to skip when extracting from /
    _SYSTEM_DIRS = frozenset((
        "bin", "dev", "etc", "lib", "lib64", "proc", "run",
        "sbin", "sys", "tmp", "usr", "var", "root",
    ))

    # Directories to skip everywhere
    _JUNK_DIRS = frozenset((
        "__pycache__", ".git", "node_modules", ".venv",
        "site-packages", ".mypy_cache", ".pytest_cache",
    ))

    def extract_image_files(
        self,
        image: str,
        extract_dir: str = "/app",
        max_file_size: int = 512_000,
        max_total_size: int = 5_000_000,
    ) -> dict[str, str]:
        """Extract source files from a Docker image without running it.

        Creates a container (doesn't start it), copies out the filesystem,
        parses the tar, and returns a dict of path → content. Skips binary
        files, oversized files, and known junk directories.

        Args:
            image: Docker image reference (e.g. "myorg/agent:v1").
            extract_dir: Directory to extract from (default "/app").
                If it doesn't exist, falls back to "/" with system dir exclusion.
            max_file_size: Skip individual files larger than this (bytes).
            max_total_size: Stop extracting after this total (bytes).

        Returns:
            Dict mapping relative file paths to their text content.
        """
        client = self._get_client()
        container = client.containers.create(
            image, command="/bin/true",
            labels={CONTAINER_LABEL: CONTAINER_LABEL_VALUE},
        )

        try:
            return self._extract_from_container(
                container, extract_dir, max_file_size, max_total_size,
            )
        finally:
            try:
                container.remove(force=True)
            except Exception as e:
                logger.warning("Failed to remove extraction container: %s", e)

    def _extract_from_container(
        self,
        container,
        extract_dir: str,
        max_file_size: int,
        max_total_size: int,
    ) -> dict[str, str]:
        """Extract files from a created (not started) container."""
        try:
            archive_stream, _ = container.get_archive(extract_dir)
        except docker.errors.NotFound:
            if extract_dir == "/":
                return {}
            logger.info(
                "%s not found in image, falling back to /", extract_dir
            )
            return self._extract_from_container(
                container, "/", max_file_size, max_total_size,
            )

        tar_bytes = b"".join(chunk for chunk in archive_stream)
        tar = tarfile.open(fileobj=io.BytesIO(tar_bytes))

        files: dict[str, str] = {}
        total_size = 0
        extracting_from_root = extract_dir == "/"

        for member in tar.getmembers():
            if not member.isfile():
                continue
            if member.size > max_file_size:
                continue
            if total_size + member.size > max_total_size:
                break

            path = member.name
            parts = path.split("/")

            # Skip system dirs when extracting from /
            if extracting_from_root and parts and parts[0] in self._SYSTEM_DIRS:
                continue

            # Skip junk dirs everywhere
            if any(p in self._JUNK_DIRS for p in parts):
                continue

            # Try to read as text — skip binary files
            f = tar.extractfile(member)
            if f is None:
                continue
            raw = f.read()
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue

            files[path] = content
            total_size += member.size

        return files

    async def extract_image_files_async(
        self, image: str, **kwargs
    ) -> dict[str, str]:
        """Async wrapper around extract_image_files()."""
        return await asyncio.to_thread(
            self.extract_image_files, image, **kwargs
        )

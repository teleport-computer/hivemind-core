import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import tempfile
import tarfile
from dataclasses import dataclass
from uuid import uuid4

import docker
import docker.errors

from .models import AgentConfig, SandboxSettings

logger = logging.getLogger(__name__)

CONTAINER_LABEL = "managed-by"
CONTAINER_LABEL_VALUE = "hivemind"
CONTAINER_RUN_LABEL = "hivemind-run-id"


@dataclass
class ContainerResult:
    """Result from a Docker container agent execution."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool


class DockerRunner:
    """Runs agent Docker containers with configurable network isolation and limits.

    The container:
      - Joins a managed Docker network (internal mode configurable)
      - Receives bridge URL + session token via env vars
      - Has memory and CPU limits enforced
      - Is always cleaned up (removed) after execution
    """

    def __init__(self, settings: SandboxSettings):
        self.settings = settings
        self._client: docker.DockerClient | None = None
        self._network_id: str | None = None
        # When hivemind-core itself runs inside a container and spawns
        # sibling containers via the host Docker socket (e.g., Phala CVM),
        # the spawned containers cannot reach the parent's bridge port via
        # the sandbox network's gateway — the gateway routes to the host,
        # not into the parent container. Workaround: attach self to the
        # sandbox network at startup and use self's IP on that network as
        # the bridge host. Populated lazily in _ensure_network().
        self._self_sandbox_ip: str | None = None
        self._network_internal_effective: bool | None = None

    def _client_from_base_url(self, base_url: str) -> docker.DockerClient:
        return docker.DockerClient(base_url=base_url)

    def _validate_client(self, client: docker.DockerClient) -> None:
        ping = getattr(client, "ping", None)
        if callable(ping):
            ping()

    def _docker_host_from_context(self) -> str | None:
        try:
            shown = subprocess.run(
                ["docker", "context", "show"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            context = shown.stdout.strip()
            if not context:
                return None
            inspected = subprocess.run(
                ["docker", "context", "inspect", context],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            payload = json.loads(inspected.stdout)
            if not isinstance(payload, list) or not payload:
                return None
            endpoint = payload[0].get("Endpoints", {}).get("docker", {})
            host = endpoint.get("Host")
            if isinstance(host, str) and host.strip():
                return host.strip()
        except Exception as e:
            logger.debug("Docker context host lookup failed: %s", e)
        return None

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            attempts: list[tuple[str, str | None]] = []
            explicit = (self.settings.docker_host or "").strip()
            if explicit:
                attempts.append(("settings", explicit))

            env_host = os.getenv("DOCKER_HOST", "").strip()
            if env_host and env_host != explicit:
                attempts.append(("env", env_host))

            attempts.append(("from_env", None))

            errors: list[str] = []
            tried_hosts: set[str] = {
                host for _, host in attempts if host is not None
            }

            for label, host in attempts:
                try:
                    client = (
                        docker.from_env()
                        if host is None
                        else self._client_from_base_url(host)
                    )
                    self._validate_client(client)
                    self._client = client
                    break
                except Exception as e:
                    host_desc = host or "(docker.from_env)"
                    errors.append(f"{label}:{host_desc} -> {e}")

            if self._client is None:
                context_host = self._docker_host_from_context()
                if context_host and context_host not in tried_hosts:
                    try:
                        client = self._client_from_base_url(context_host)
                        self._validate_client(client)
                        self._client = client
                    except Exception as e:
                        errors.append(f"context:{context_host} -> {e}")

            if self._client is None:
                details = "; ".join(errors) if errors else "no attempts made"
                raise RuntimeError(
                    "Unable to connect to Docker daemon. "
                    f"Attempted: {details}"
                )
        return self._client

    def cleanup_stale_containers(self):
        """Remove non-running managed containers from previous crashes."""
        try:
            client = self._get_client()
            managed = client.containers.list(
                all=True,
                filters={"label": f"{CONTAINER_LABEL}={CONTAINER_LABEL_VALUE}"},
            )
            for c in managed:
                status = getattr(c, "status", "") or ""
                if status == "running":
                    continue
                logger.warning(
                    "Removing stale managed container %s (status=%s)",
                    c.short_id,
                    status or "unknown",
                )
                c.remove(force=True)
        except Exception as e:
            logger.debug("Stale container cleanup skipped: %s", e)

    def _ensure_network(self) -> str:
        """Create or get the internal Docker network. Returns network name."""
        client = self._get_client()
        name = self.settings.docker_network_name

        if self._network_id:
            return name

        internal = self.settings.docker_network_internal
        # Docker Desktop (macOS/Windows) cannot reach host bridge from an
        # internal network, so force a compatible network mode.
        if internal and platform.system() in ("Darwin", "Windows"):
            logger.warning(
                "docker_network_internal=true is incompatible with host bridge on %s; using internal=false",
                platform.system(),
            )
            internal = False

        try:
            network = client.networks.get(name)
            existing_internal = bool(network.attrs.get("Internal", False))
            if existing_internal != internal:
                attached = network.attrs.get("Containers") or {}
                if attached:
                    logger.warning(
                        "Network %s has internal=%s but desired=%s and has %d attached containers; reusing existing network",
                        name,
                        existing_internal,
                        internal,
                        len(attached),
                    )
                else:
                    logger.warning(
                        "Recreating network %s with internal=%s (was internal=%s)",
                        name,
                        internal,
                        existing_internal,
                    )
                    network.remove()
                    network = client.networks.create(
                        name,
                        driver="bridge",
                        internal=internal,
                        labels={CONTAINER_LABEL: CONTAINER_LABEL_VALUE},
                    )
            self._network_id = network.id
        except docker.errors.NotFound:
            network = client.networks.create(
                name,
                driver="bridge",
                internal=internal,
                labels={CONTAINER_LABEL: CONTAINER_LABEL_VALUE},
            )
            self._network_id = network.id
            logger.info("Created Docker network %s (internal=%s)", name, internal)

        self._network_internal_effective = bool(
            (getattr(network, "attrs", {}) or {}).get("Internal", internal)
        )

        # When running inside a container (e.g., Phala CVM), attach self to
        # the sandbox network so spawned siblings can reach our bridge.
        self._attach_self_to_network(client, network)

        return name

    def _detect_self_container_id(self) -> str | None:
        """Return our own Docker container short-id if running in a container.

        HOSTNAME inside a container is the container's 12-char short-id by
        default. Fall back to /etc/hostname. Returns None on host/bare-metal.
        """
        hostname = os.environ.get("HOSTNAME", "").strip()
        if not hostname:
            try:
                with open("/etc/hostname", "r", encoding="utf-8") as f:
                    hostname = f.read().strip()
            except OSError:
                return None
        # Plain-host hostnames usually contain dots or are short/long words;
        # docker container IDs are 12 hex chars. Accept anything the daemon
        # can resolve.
        return hostname or None

    def _attach_self_to_network(self, client, network) -> None:
        """Connect the hivemind-core container to the sandbox network.

        No-op when we can't find ourselves via the daemon (e.g., hivemind
        runs on the host directly, or the mounted socket belongs to a
        different daemon). Safe to call repeatedly.
        """
        if self._self_sandbox_ip:
            return
        hostname = self._detect_self_container_id()
        if not hostname:
            return
        try:
            self_container = client.containers.get(hostname)
        except docker.errors.NotFound:
            return
        except Exception as e:
            logger.debug("self-container lookup failed: %s", e)
            return
        try:
            self_container.reload()
            networks = (
                (self_container.attrs or {})
                .get("NetworkSettings", {})
                .get("Networks", {})
            )
            net_name = network.name
            if net_name not in networks:
                try:
                    network.connect(self_container)
                    logger.info(
                        "Attached self (%s) to sandbox network %s",
                        hostname,
                        net_name,
                    )
                except docker.errors.APIError as e:
                    # Race: another worker might have connected us.
                    logger.info("self connect to %s noted: %s", net_name, e)
                self_container.reload()
                networks = (
                    (self_container.attrs or {})
                    .get("NetworkSettings", {})
                    .get("Networks", {})
                )
            ip = (networks.get(net_name) or {}).get("IPAddress", "").strip()
            if ip:
                self._self_sandbox_ip = ip
                logger.info(
                    "Self IP on sandbox network %s = %s", net_name, ip
                )
        except Exception as e:
            logger.warning(
                "Failed to attach self to sandbox network: %s", e
            )

    def _resolve_bridge_url(self, port: int) -> str:
        """Build the bridge URL that containers can reach.

        Priority:
          1. If hivemind is in a container and we've attached ourselves to
             the sandbox network, use our own IP on that network. This is
             the only path that works when the host Docker daemon is
             shared (e.g., Phala CVM): the sandbox-network gateway routes
             to the host, not into the parent container.
          2. macOS/Windows desktop: host.docker.internal.
          3. Linux host: sandbox-network gateway IP.
        """
        if self._self_sandbox_ip:
            return f"http://{self._self_sandbox_ip}:{port}"

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

    def _resolve_ipv4(self, host: str) -> str:
        if not host:
            raise RuntimeError("Bridge host is empty")
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            return socket.gethostbyname(host)

    def _container_ipv4(self, container, network_name: str) -> str:
        if hasattr(container, "reload"):
            container.reload()
        attrs = getattr(container, "attrs", {}) or {}
        networks = attrs.get("NetworkSettings", {}).get("Networks", {})
        network = networks.get(network_name)
        if not network and networks:
            network = next(iter(networks.values()))
        ip = (network or {}).get("IPAddress", "").strip()
        if not ip:
            raise RuntimeError(
                f"Could not determine container IP for network '{network_name}'"
            )
        return ip

    def _install_bridge_only_egress_rules(
        self,
        container,
        network_name: str,
        bridge_host: str,
        bridge_port: int,
    ) -> list[list[str]]:
        src_ip = self._container_ipv4(container, network_name)
        dst_ip = self._resolve_ipv4(bridge_host)
        marker = f"hivemind:{getattr(container, 'id', '')[:12] or getattr(container, 'short_id', 'agent')}"

        commands = [
            [
                "iptables",
                "-I",
                "DOCKER-USER",
                "1",
                "-s",
                src_ip,
                "-d",
                dst_ip,
                "-p",
                "tcp",
                "--dport",
                str(bridge_port),
                "-m",
                "comment",
                "--comment",
                marker,
                "-j",
                "ACCEPT",
            ],
            [
                "iptables",
                "-I",
                "DOCKER-USER",
                "2",
                "-s",
                src_ip,
                "-m",
                "comment",
                "--comment",
                marker,
                "-j",
                "DROP",
            ],
        ]

        applied: list[list[str]] = []
        try:
            for cmd in commands:
                subprocess.run(
                    cmd,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                applied.append(cmd)
        except Exception:
            self._remove_firewall_rules(applied)
            raise
        return applied

    def _delete_cmd_for_inserted_rule(self, cmd: list[str]) -> list[str]:
        # Convert: iptables -I CHAIN <n> <spec...>  ->  iptables -D CHAIN <spec...>
        if len(cmd) >= 5 and cmd[0] == "iptables" and cmd[1] == "-I":
            chain = cmd[2]
            offset = 4 if cmd[3].isdigit() else 3
            return ["iptables", "-D", chain, *cmd[offset:]]
        return cmd

    def _remove_firewall_rules(self, applied_commands: list[list[str]]) -> None:
        for cmd in reversed(applied_commands):
            del_cmd = self._delete_cmd_for_inserted_rule(cmd)
            try:
                subprocess.run(
                    del_cmd,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except Exception as e:
                logger.warning("Failed to remove firewall rule %s: %s", del_cmd, e)

    def _remove_containers_by_label(self, label_filter: str) -> int:
        """Best-effort removal for all containers matching a label filter."""
        client = self._get_client()
        removed = 0
        containers = client.containers.list(
            all=True,
            filters={"label": label_filter},
        )
        for container in containers:
            try:
                container.remove(force=True)
                removed += 1
            except Exception as e:
                logger.warning(
                    "Failed to remove labeled container %s (%s): %s",
                    getattr(container, "short_id", "unknown"),
                    label_filter,
                    e,
                )
        return removed

    async def _cleanup_startup_timeout_containers(
        self,
        run_id: str,
        *,
        max_wait_seconds: float = 4.0,
        poll_interval_seconds: float = 0.25,
    ) -> int:
        """Wait briefly for delayed startup calls and remove leaked containers."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait_seconds
        label_filter = f"{CONTAINER_RUN_LABEL}={run_id}"
        total_removed = 0
        saw_container = False
        empty_polls_after_seen = 0

        while True:
            try:
                removed_now = await asyncio.to_thread(
                    self._remove_containers_by_label,
                    label_filter,
                )
            except Exception as e:
                logger.warning(
                    "Startup-timeout cleanup failed for run %s: %s",
                    run_id,
                    e,
                )
                return total_removed

            total_removed += removed_now
            if removed_now > 0:
                saw_container = True
                empty_polls_after_seen = 0
            elif saw_container:
                empty_polls_after_seen += 1
                if empty_polls_after_seen >= 2:
                    return total_removed

            if loop.time() >= deadline:
                return total_removed
            await asyncio.sleep(poll_interval_seconds)

    async def run_agent(
        self,
        agent: AgentConfig,
        bridge_url: str,
        session_token: str,
        env: dict[str, str] | None = None,
        extra_volumes: dict[str, dict[str, str]] | None = None,
    ) -> ContainerResult:
        """Run an agent Docker container.

        The bridge_url passed in is the host-side URL. We resolve a
        container-reachable URL internally. The env dict is passed
        directly to the container with BRIDGE_URL rewritten.
        """
        from urllib.parse import urlparse

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(1, agent.timeout_seconds)

        def _remaining_timeout() -> float:
            return deadline - loop.time()

        async def _to_thread_with_deadline(fn, *args, **kwargs):
            remaining = _remaining_timeout()
            if remaining <= 0:
                raise asyncio.TimeoutError
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args, **kwargs),
                timeout=remaining,
            )

        async def _kill_container_best_effort(container_obj) -> None:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(container_obj.kill),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Timed out while killing container %s",
                    getattr(container_obj, "short_id", "unknown"),
                )
            except Exception as e:
                logger.warning(
                    "Failed to kill container %s: %s",
                    getattr(container_obj, "short_id", "unknown"),
                    e,
                )

        parsed = urlparse(bridge_url)
        port = parsed.port or 80
        run_id = uuid4().hex[:12]
        container = None
        egress_rules: list[list[str]] = []

        # Resource limits
        memory_mb = min(agent.memory_mb, self.settings.container_memory_mb)
        mem_limit = f"{memory_mb}m"
        nano_cpus = int(self.settings.container_cpu_quota * 1e9)
        security_opt: list[str] = []
        if self.settings.container_no_new_privileges:
            security_opt.append("no-new-privileges:true")
        try:
            # Order matters: _ensure_network attaches us to the sandbox
            # network and records self IP, which _resolve_bridge_url reads.
            network_name = await _to_thread_with_deadline(self._ensure_network)
            container_bridge_url = await _to_thread_with_deadline(
                self._resolve_bridge_url,
                port,
            )

            container_env = dict(env or {})
            container_env["BRIDGE_URL"] = container_bridge_url
            container_env["SESSION_TOKEN"] = session_token
            container_env["OPENAI_BASE_URL"] = f"{container_bridge_url}/v1"
            container_env["OPENAI_API_KEY"] = session_token
            container_env["ANTHROPIC_BASE_URL"] = container_bridge_url
            container_env["ANTHROPIC_API_KEY"] = session_token

            # Build container kwargs
            run_kwargs: dict = {
                "image": agent.image,
                "environment": container_env,
                "network": network_name,
                "mem_limit": mem_limit,
                "nano_cpus": nano_cpus,
                "pids_limit": self.settings.container_pids_limit,
                "labels": {
                    CONTAINER_LABEL: CONTAINER_LABEL_VALUE,
                    CONTAINER_RUN_LABEL: run_id,
                },
                "detach": True,
                "stdout": True,
                "stderr": True,
            }
            if self.settings.container_read_only_fs:
                run_kwargs["read_only"] = True
                run_kwargs["tmpfs"] = {
                    "/tmp": "rw,exec,nosuid,size=64m",
                    "/var/tmp": "rw,noexec,nosuid,size=32m",
                    "/home/agent": "rw,exec,nosuid,size=64m,uid=1000,gid=1000",
                }
                # Claude Code CLI requires a writable cwd for session files
                run_kwargs["working_dir"] = "/tmp"
            if extra_volumes:
                # extra_volumes format matches Docker SDK's volumes= kwarg:
                #   {"/host/abs/path": {"bind": "/container/path", "mode": "ro"}}
                run_kwargs["volumes"] = dict(extra_volumes)
            container_user = (self.settings.container_user or "").strip()
            if container_user:
                run_kwargs["user"] = container_user
            if self.settings.container_drop_all_caps:
                run_kwargs["cap_drop"] = ["ALL"]
            if security_opt:
                run_kwargs["security_opt"] = security_opt
            if agent.entrypoint:
                run_kwargs["entrypoint"] = agent.entrypoint

            try:
                client = await _to_thread_with_deadline(self._get_client)
                container = await _to_thread_with_deadline(
                    client.containers.run,
                    **run_kwargs,
                )
            except asyncio.TimeoutError:
                removed = await self._cleanup_startup_timeout_containers(
                    run_id,
                    max_wait_seconds=max(0.0, _remaining_timeout()),
                    poll_interval_seconds=0.1,
                )
                return ContainerResult(
                    stdout="",
                    stderr=(
                        "Agent startup timed out before container became ready "
                        f"({agent.timeout_seconds}s total budget); "
                        f"removed {removed} startup container(s)."
                    ),
                    exit_code=-1,
                    timed_out=True,
                )

            logger.info(
                "Started container %s for agent %s (image=%s, timeout=%ds)",
                container.short_id,
                agent.agent_id,
                agent.image,
                agent.timeout_seconds,
            )

            if self.settings.enforce_bridge_only_egress:
                if self._network_internal_effective:
                    logger.info(
                        "Skipping host iptables egress rules because Docker network %s is internal",
                        network_name,
                    )
                elif platform.system() != "Linux":
                    logger.warning(
                        "Bridge-only host egress enforcement is supported only on Linux; "
                        "continuing without host firewall rules on %s",
                        platform.system(),
                    )
                else:
                    bridge_parsed = urlparse(container_bridge_url)
                    bridge_host = bridge_parsed.hostname or ""
                    bridge_port = bridge_parsed.port or port
                    try:
                        egress_rules = await _to_thread_with_deadline(
                            self._install_bridge_only_egress_rules,
                            container,
                            network_name,
                            bridge_host,
                            bridge_port,
                        )
                    except Exception as e:
                        msg = f"Egress policy setup failed: {e}"
                        if self.settings.enforce_bridge_only_egress_fail_closed:
                            logger.error(msg)
                            await _kill_container_best_effort(container)
                            return ContainerResult(
                                stdout="",
                                stderr=msg,
                                exit_code=-1,
                                timed_out=False,
                            )
                        logger.warning("%s; continuing without host firewall rules", msg)

            # Wait for container to finish within the remaining timeout budget.
            try:
                result = await _to_thread_with_deadline(container.wait)
                exit_code = result.get("StatusCode", -1)
                timed_out = False
            except asyncio.TimeoutError:
                logger.warning(
                    "Agent %s timed out after %ds, killing container %s",
                    agent.agent_id,
                    agent.timeout_seconds,
                    container.short_id,
                )
                await _kill_container_best_effort(container)
                exit_code = -1
                timed_out = True

            def _decode(payload: bytes | str) -> str:
                if isinstance(payload, bytes):
                    return payload.decode(errors="replace")
                return str(payload)

            # Capture output without extending runtime past the run deadline.
            logs_truncated = False
            stdout_str = ""
            stderr_str = ""
            try:
                stdout = await _to_thread_with_deadline(
                    container.logs,
                    stdout=True,
                    stderr=False,
                )
                stdout_str = _decode(stdout)
            except asyncio.TimeoutError:
                logs_truncated = True
                logger.warning(
                    "Skipping stdout collection for %s due to timeout budget",
                    container.short_id,
                )
            except Exception as e:
                logger.warning("Failed collecting stdout logs for %s: %s", container.short_id, e)

            try:
                stderr = await _to_thread_with_deadline(
                    container.logs,
                    stdout=False,
                    stderr=True,
                )
                stderr_str = _decode(stderr)
            except asyncio.TimeoutError:
                logs_truncated = True
                logger.warning(
                    "Skipping stderr collection for %s due to timeout budget",
                    container.short_id,
                )
            except Exception as e:
                logger.warning("Failed collecting stderr logs for %s: %s", container.short_id, e)

            if logs_truncated:
                note = "(Log collection truncated due to timeout budget)"
                stderr_str = f"{stderr_str}\n{note}" if stderr_str else note

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
        except asyncio.TimeoutError:
            return ContainerResult(
                stdout="",
                stderr=(
                    "Agent execution exceeded timeout budget before completion "
                    f"({agent.timeout_seconds}s)."
                ),
                exit_code=-1,
                timed_out=True,
            )

        finally:
            if egress_rules:
                try:
                    await asyncio.to_thread(self._remove_firewall_rules, egress_rules)
                except Exception as e:
                    logger.warning("Failed to remove container egress rules: %s", e)
            if container:
                try:
                    await asyncio.to_thread(container.remove, force=True)
                except Exception as e:
                    logger.warning("Failed to remove container: %s", e)

    # ── Image building ──

    def _ensure_agent_base_for_dockerfile(self, dockerfile: str) -> None:
        with open(dockerfile, encoding="utf-8") as f:
            for raw_line in f:
                parts = raw_line.strip().split()
                if len(parts) < 2 or parts[0].upper() != "FROM":
                    continue
                if not parts[1].startswith("hivemind-agent-base"):
                    continue
                from ..agent_base_bootstrap import ensure_agent_base_image

                if not ensure_agent_base_image():
                    raise RuntimeError(
                        "hivemind-agent-base:latest is required by this "
                        "agent Dockerfile but could not be provisioned"
                    )
                return

    def _build_container_limits(self) -> dict:
        memory_bytes = int(self.settings.docker_build_memory_mb) * 1024 * 1024
        limits: dict = {
            "memory": memory_bytes,
            "memswap": memory_bytes,
            "cpushares": int(self.settings.docker_build_cpu_shares),
        }
        return limits

    def _validate_build_context(self, build_path: str) -> str:
        dockerfile = os.path.join(build_path, "Dockerfile")
        if not os.path.isfile(dockerfile):
            raise ValueError(
                "No Dockerfile found in upload. A Dockerfile is required."
            )
        self._ensure_agent_base_for_dockerfile(dockerfile)
        return dockerfile

    def _docker_build_worker_host(self) -> str | None:
        explicit = (self.settings.docker_host or "").strip()
        if explicit:
            return explicit
        env_host = os.getenv("DOCKER_HOST", "").strip()
        if env_host:
            return env_host
        return self._docker_host_from_context()

    def _build_worker_args(self, build_path: str, tag: str) -> list[str]:
        args = [
            sys.executable,
            "-m",
            "hivemind.sandbox.docker_build_worker",
            "--path",
            build_path,
            "--tag",
            tag,
            "--memory-mb",
            str(int(self.settings.docker_build_memory_mb)),
            "--cpu-shares",
            str(int(self.settings.docker_build_cpu_shares)),
        ]
        build_network = (self.settings.docker_build_network or "").strip()
        if build_network:
            args.extend(["--network", build_network])
        docker_host = self._docker_build_worker_host()
        if docker_host:
            args.extend(["--docker-host", docker_host])
        return args

    async def _build_image_worker_async(self, build_path: str, tag: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            *self._build_worker_args(build_path, tag),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.settings.docker_build_timeout_seconds,
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.communicate()
            raise TimeoutError(
                f"Docker image build timed out after "
                f"{self.settings.docker_build_timeout_seconds}s"
            )
        if proc.returncode != 0:
            details = (stderr or stdout or b"").decode(errors="replace").strip()
            raise RuntimeError(
                f"Docker image build failed for {tag}: "
                f"{details[:1200] or 'no details'}"
            )
        logger.info("Built Docker image %s from %s", tag, build_path)
        return tag

    def build_image(self, build_path: str, tag: str) -> str:
        """Build a Docker image from a directory containing a Dockerfile.

        Args:
            build_path: Directory containing a Dockerfile and source files.
            tag: Image tag (e.g. "hivemind-agent-abc123:latest").

        Returns:
            The image tag string.

        Raises:
            ValueError: If no Dockerfile is found in build_path.
        """
        self._validate_build_context(build_path)

        client = self._get_client()
        build_kwargs: dict = {
            "path": build_path,
            "tag": tag,
            "rm": True,
            "forcerm": True,
            "pull": False,
            "labels": {CONTAINER_LABEL: CONTAINER_LABEL_VALUE},
            "container_limits": self._build_container_limits(),
        }
        build_network = (self.settings.docker_build_network or "").strip()
        if build_network:
            build_kwargs["network_mode"] = build_network
        client.images.build(**build_kwargs)
        logger.info("Built Docker image %s from %s", tag, build_path)
        return tag

    async def build_image_async(self, build_path: str, tag: str) -> str:
        """Build in a worker process so async timeout can cancel the build client."""
        self._validate_build_context(build_path)
        return await self._build_image_worker_async(build_path, tag)

    def image_exists(self, image: str) -> bool:
        """Return True if the Docker image is present locally."""
        try:
            self._get_client().images.get(image)
            return True
        except docker.errors.ImageNotFound:
            return False

    async def ensure_image_async(
        self, image_tag: str, files: dict[str, str] | None,
    ) -> bool:
        """Ensure ``image_tag`` is present locally; rebuild from ``files`` if not.

        Returns True if a rebuild happened, False if the image was already
        present. Raises ValueError if the image is missing and ``files``
        does not contain a Dockerfile (e.g. an agent uploaded before the
        store-build-context migration).

        On Phala the CVM root FS — including /var/lib/docker — is
        reinitialized on every compose update, so per-agent images get
        wiped. Source persists in pgdata; this rebuilds on demand the
        first time the missing agent is invoked after a redeploy.
        """
        if await asyncio.to_thread(self.image_exists, image_tag):
            return False
        if not files:
            raise ValueError(
                f"Docker image {image_tag} is missing and no stored build "
                "context is available. The agent likely predates the "
                "rebuild-from-source migration — please re-upload it."
            )
        if "Dockerfile" not in files:
            raise ValueError(
                f"Docker image {image_tag} is missing and the stored build "
                "context lacks a Dockerfile (likely an agent uploaded "
                "before build-context persistence) — please re-upload it."
            )

        logger.info(
            "Image %s missing; rebuilding from stored context (%d files)",
            image_tag, len(files),
        )
        with tempfile.TemporaryDirectory(prefix="hivemind-rebuild-") as td:
            base = os.path.realpath(td)
            for rel_path, content in files.items():
                # Defense-in-depth: refuse absolute / parent-traversing
                # paths even though the source comes from our own DB.
                if os.path.isabs(rel_path) or ".." in rel_path.split("/"):
                    raise ValueError(
                        f"Refusing to materialize unsafe path: {rel_path}"
                    )
                target = os.path.realpath(os.path.join(td, rel_path))
                if not target.startswith(base + os.sep) and target != base:
                    raise ValueError(
                        f"Refusing to materialize path outside tmpdir: {rel_path}"
                    )
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w", encoding="utf-8") as f:
                    f.write(content)
            await self.build_image_async(td, image_tag)
        return True

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
        max_archive_size: int = 50_000_000,
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
                max_archive_size,
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
        max_archive_size: int,
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
                max_archive_size,
            )

        # Stream archive to a spool file with a hard byte cap so extraction
        # cannot force unbounded memory growth on the host.
        total_archive_bytes = 0
        with tempfile.SpooledTemporaryFile(max_size=4_000_000) as spool:
            for chunk in archive_stream:
                total_archive_bytes += len(chunk)
                if total_archive_bytes > max_archive_size:
                    logger.warning(
                        "Skipping extraction from %s: archive exceeds max size (%d > %d)",
                        extract_dir,
                        total_archive_bytes,
                        max_archive_size,
                    )
                    return {}
                spool.write(chunk)

            spool.seek(0)
            with tarfile.open(fileobj=spool, mode="r:*") as tar:
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

import asyncio
import io
import sys
import tarfile
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hivemind.sandbox.docker_runner import ContainerResult, DockerRunner
from hivemind.sandbox.docker_runner import CONTAINER_LABEL, CONTAINER_LABEL_VALUE
from hivemind.sandbox.models import AgentConfig, SandboxSettings
from hivemind.sandbox import docker_build_worker


def _make_settings(**overrides):
    defaults = {
        "bridge_host": "0.0.0.0",
        "docker_network_name": "test-hivemind",
        "container_memory_mb": 256,
        "container_cpu_quota": 1.0,
        "global_max_llm_calls": 50,
        "global_max_tokens": 200_000,
        "global_timeout_seconds": 300,
    }
    defaults.update(overrides)
    return SandboxSettings(**defaults)


def _make_agent(**overrides):
    defaults = {
        "agent_id": "test-agent",
        "name": "Test Agent",
        "image": "myorg/test-agent:v1",
        "timeout_seconds": 60,
        "memory_mb": 256,
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


class MockContainer:
    def __init__(self, exit_code=0, stdout=b"agent output", stderr=b""):
        self.short_id = "abc123"
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self.removed = False
        self.killed = False

    def wait(self):
        return {"StatusCode": self._exit_code}

    def logs(self, stdout=True, stderr=True):
        if stdout and not stderr:
            return self._stdout
        if stderr and not stdout:
            return self._stderr
        return self._stdout + self._stderr

    def kill(self):
        self.killed = True

    def remove(self, force=False):
        self.removed = True


class MockDockerClient:
    def __init__(self, container=None):
        self._container = container or MockContainer()
        self.containers = MockContainers(self._container)
        self.networks = MockNetworks()


class MockContainers:
    def __init__(self, container):
        self._container = container
        self.run_kwargs = None
        self._created_containers = []

    def run(self, **kwargs):
        self.run_kwargs = kwargs
        self._created_containers.append(self._container)
        return self._container

    def list(self, all=False, filters=None):
        return [c for c in self._created_containers if not getattr(c, "removed", False)]


class MockNetworks:
    def __init__(self):
        self._network = MagicMock()
        self._network.id = "net-123"
        self._network.attrs = {"IPAM": {"Config": [{"Gateway": "172.18.0.1"}]}}
        self.created = False

    def get(self, name):
        return self._network

    def create(self, name, **kwargs):
        self.created = True
        return self._network


@pytest.mark.asyncio
async def test_run_agent_basic():
    container = MockContainer(exit_code=0, stdout=b"hello from agent")
    mock_client = MockDockerClient(container)

    settings = _make_settings()
    agent = _make_agent()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        result = await runner.run_agent(
            agent=agent,
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            env={
                "BRIDGE_URL": "http://0.0.0.0:9999",
                "SESSION_TOKEN": "tok-123",
                "AGENT_ROLE": "query",
                "QUERY_PROMPT": "test prompt",
            },
        )

    assert isinstance(result, ContainerResult)
    assert result.stdout == "hello from agent"
    assert result.exit_code == 0
    assert result.timed_out is False

    kwargs = mock_client.containers.run_kwargs
    assert kwargs["image"] == "myorg/test-agent:v1"
    assert kwargs["environment"]["SESSION_TOKEN"] == "tok-123"
    assert kwargs["environment"]["QUERY_PROMPT"] == "test prompt"
    assert "BRIDGE_URL" in kwargs["environment"]
    assert kwargs["environment"]["OPENAI_BASE_URL"] == (
        f"{kwargs['environment']['BRIDGE_URL']}/v1"
    )
    assert kwargs["environment"]["OPENAI_API_KEY"] == "tok-123"
    assert kwargs["environment"]["ANTHROPIC_BASE_URL"] == kwargs["environment"]["BRIDGE_URL"]
    assert kwargs["environment"]["ANTHROPIC_API_KEY"] == "tok-123"
    assert kwargs["mem_limit"] == "256m"
    assert kwargs["pids_limit"] == 256
    assert kwargs["read_only"] is True
    assert kwargs["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in kwargs["security_opt"]
    assert kwargs["user"] == "1000:1000"
    assert "/tmp" in kwargs["tmpfs"]
    assert kwargs["detach"] is True

    assert container.removed is True


@pytest.mark.asyncio
async def test_run_agent_with_env():
    container = MockContainer()
    mock_client = MockDockerClient(container)

    settings = _make_settings()
    agent = _make_agent()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        await runner.run_agent(
            agent=agent,
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            env={
                "BRIDGE_URL": "http://0.0.0.0:9999",
                "SESSION_TOKEN": "tok-123",
                "ARBITRARY_CONTEXT": '{"user": "alice"}',
                "CUSTOM_VAR": "value",
            },
        )

    env = mock_client.containers.run_kwargs["environment"]
    assert env["ARBITRARY_CONTEXT"] == '{"user": "alice"}'
    assert env["CUSTOM_VAR"] == "value"
    assert env["OPENAI_BASE_URL"] == f"{env['BRIDGE_URL']}/v1"
    assert env["OPENAI_API_KEY"] == "tok-123"
    assert env["ANTHROPIC_BASE_URL"] == env["BRIDGE_URL"]
    assert env["ANTHROPIC_API_KEY"] == "tok-123"


@pytest.mark.asyncio
async def test_run_agent_can_disable_hardening_flags():
    container = MockContainer()
    mock_client = MockDockerClient(container)

    settings = _make_settings(
        container_read_only_fs=False,
        container_drop_all_caps=False,
        container_no_new_privileges=False,
        container_pids_limit=128,
    )
    agent = _make_agent()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        await runner.run_agent(
            agent=agent,
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            env={"BRIDGE_URL": "http://0.0.0.0:9999", "SESSION_TOKEN": "tok-123"},
        )

    kwargs = mock_client.containers.run_kwargs
    assert kwargs["pids_limit"] == 128
    assert "read_only" not in kwargs
    assert "cap_drop" not in kwargs
    assert "security_opt" not in kwargs
    assert kwargs["user"] == "1000:1000"


@pytest.mark.asyncio
async def test_run_agent_with_entrypoint():
    container = MockContainer()
    mock_client = MockDockerClient(container)

    settings = _make_settings()
    agent = _make_agent(entrypoint="/custom/run.sh")

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        await runner.run_agent(
            agent=agent,
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            env={"BRIDGE_URL": "http://0.0.0.0:9999", "SESSION_TOKEN": "tok-123"},
        )

    assert mock_client.containers.run_kwargs["entrypoint"] == "/custom/run.sh"


@pytest.mark.asyncio
async def test_run_agent_non_linux_skips_bridge_only_egress_even_fail_closed():
    container = MockContainer(exit_code=0, stdout=b"ok")
    mock_client = MockDockerClient(container)
    settings = _make_settings(
        enforce_bridge_only_egress=True,
        enforce_bridge_only_egress_fail_closed=True,
    )
    agent = _make_agent()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker, patch(
        "hivemind.sandbox.docker_runner.platform.system", return_value="Darwin"
    ), patch(
        "hivemind.sandbox.docker_runner.subprocess.run"
    ) as mock_subprocess:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        result = await runner.run_agent(
            agent=agent,
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            env={"BRIDGE_URL": "http://0.0.0.0:9999", "SESSION_TOKEN": "tok-123"},
        )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert container.killed is False
    assert container.removed is True
    assert not any(
        call.args and call.args[0] and call.args[0][0] == "iptables"
        for call in mock_subprocess.call_args_list
    )


@pytest.mark.asyncio
async def test_run_agent_internal_network_skips_host_iptables_even_on_linux():
    container = MockContainer(exit_code=0, stdout=b"ok")
    mock_client = MockDockerClient(container)
    settings = _make_settings(
        docker_network_internal=True,
        enforce_bridge_only_egress=True,
        enforce_bridge_only_egress_fail_closed=True,
    )
    agent = _make_agent()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker, patch(
        "hivemind.sandbox.docker_runner.platform.system", return_value="Linux"
    ), patch(
        "hivemind.sandbox.docker_runner.subprocess.run"
    ) as mock_subprocess:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        result = await runner.run_agent(
            agent=agent,
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            env={"BRIDGE_URL": "http://0.0.0.0:9999", "SESSION_TOKEN": "tok-123"},
        )

    assert result.exit_code == 0
    assert container.killed is False
    assert not any(
        call.args and call.args[0] and call.args[0][0] == "iptables"
        for call in mock_subprocess.call_args_list
    )


@pytest.mark.asyncio
async def test_run_agent_timeout():
    class SlowContainer(MockContainer):
        def wait(self):
            import time
            time.sleep(10)
            return {"StatusCode": 0}

    container = SlowContainer()
    mock_client = MockDockerClient(container)

    settings = _make_settings()
    agent = _make_agent(timeout_seconds=1)

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        result = await runner.run_agent(
            agent=agent,
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            env={"BRIDGE_URL": "http://0.0.0.0:9999", "SESSION_TOKEN": "tok-123"},
        )

    assert result.timed_out is True
    assert result.exit_code == -1
    assert container.killed is True
    assert container.removed is True


@pytest.mark.asyncio
async def test_run_agent_startup_timeout():
    class SlowStartContainers(MockContainers):
        def run(self, **kwargs):
            import time
            self.run_kwargs = kwargs
            time.sleep(3)
            self._created_containers.append(self._container)
            return self._container

    container = MockContainer()
    mock_client = MockDockerClient(container)
    mock_client.containers = SlowStartContainers(container)

    settings = _make_settings()
    agent = _make_agent(timeout_seconds=1)

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        started = time.monotonic()
        result = await runner.run_agent(
            agent=agent,
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            env={"BRIDGE_URL": "http://0.0.0.0:9999", "SESSION_TOKEN": "tok-123"},
        )
        elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.exit_code == -1
    assert "startup timed out" in result.stderr.lower()
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_run_agent_timeout_applies_to_network_setup():
    container = MockContainer()
    mock_client = MockDockerClient(container)

    settings = _make_settings(enforce_bridge_only_egress=False)
    agent = _make_agent(timeout_seconds=1)

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)

        def slow_network():
            time.sleep(3)
            return settings.docker_network_name

        runner._ensure_network = slow_network
        started = time.monotonic()
        result = await runner.run_agent(
            agent=agent,
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            env={"BRIDGE_URL": "http://0.0.0.0:9999", "SESSION_TOKEN": "tok-123"},
        )
        elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.exit_code == -1
    assert "timeout budget" in result.stderr.lower()
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_run_agent_logs_respect_timeout_budget():
    class SlowLogsContainer(MockContainer):
        def wait(self):
            return {"StatusCode": 0}

        def logs(self, stdout=True, stderr=True):
            time.sleep(3)
            return b""

    container = SlowLogsContainer()
    mock_client = MockDockerClient(container)

    settings = _make_settings(enforce_bridge_only_egress=False)
    agent = _make_agent(timeout_seconds=1)

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        started = time.monotonic()
        result = await runner.run_agent(
            agent=agent,
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            env={"BRIDGE_URL": "http://0.0.0.0:9999", "SESSION_TOKEN": "tok-123"},
        )
        elapsed = time.monotonic() - started

    assert result.exit_code == 0
    assert result.timed_out is False
    assert "truncated" in result.stderr.lower()
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_run_agent_oom():
    container = MockContainer(exit_code=137, stdout=b"partial output", stderr=b"")
    mock_client = MockDockerClient(container)

    settings = _make_settings()
    agent = _make_agent()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        result = await runner.run_agent(
            agent=agent,
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            env={"BRIDGE_URL": "http://0.0.0.0:9999", "SESSION_TOKEN": "tok-123"},
        )

    assert result.exit_code == 137
    assert "OOM" in result.stderr


@pytest.mark.asyncio
async def test_run_agent_image_not_found():
    mock_client = MockDockerClient()

    import docker.errors as docker_errors

    def raise_not_found(**kwargs):
        raise docker_errors.ImageNotFound("not found")

    mock_client.containers.run = raise_not_found

    settings = _make_settings()
    agent = _make_agent(image="nonexistent/image:v999")

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = docker_errors

        runner = DockerRunner(settings)
        result = await runner.run_agent(
            agent=agent,
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            env={"BRIDGE_URL": "http://0.0.0.0:9999", "SESSION_TOKEN": "tok-123"},
        )

    assert result.exit_code == -1
    assert "not found" in result.stderr.lower()


@pytest.mark.asyncio
async def test_cleanup_always_runs():
    class ErrorContainer(MockContainer):
        def wait(self):
            raise RuntimeError("unexpected error")

    container = ErrorContainer()
    mock_client = MockDockerClient(container)

    settings = _make_settings()
    agent = _make_agent()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        with pytest.raises(RuntimeError):
            await runner.run_agent(
                agent=agent,
                bridge_url="http://0.0.0.0:9999",
                session_token="tok-123",
                env={"BRIDGE_URL": "http://0.0.0.0:9999", "SESSION_TOKEN": "tok-123"},
            )

    assert container.removed is True

@pytest.mark.asyncio
async def test_network_labels():
    mock_client = MockDockerClient()

    settings = _make_settings()

    import docker.errors as docker_errors

    def raise_not_found(name):
        raise docker_errors.NotFound("not found")

    mock_client.networks.get = raise_not_found

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = docker_errors

        runner = DockerRunner(settings)
        runner._ensure_network()

    assert mock_client.networks.created is True


def test_cleanup_stale_containers_skips_running():
    running = MagicMock()
    running.short_id = "run123"
    running.status = "running"

    exited = MagicMock()
    exited.short_id = "exit123"
    exited.status = "exited"

    mock_client = MockDockerClient()
    mock_client.containers.list = lambda **kw: [running, exited]

    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        runner.cleanup_stale_containers()

    running.remove.assert_not_called()
    exited.remove.assert_called_once_with(force=True)


def test_get_client_prefers_configured_docker_host():
    mock_client = MagicMock()
    mock_client.ping.return_value = True

    settings = _make_settings(docker_host="unix:///tmp/docker.sock")

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.DockerClient.return_value = mock_client
        mock_docker.from_env.side_effect = AssertionError(
            "from_env should not be used when docker_host is configured"
        )
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        client = runner._get_client()

    assert client is mock_client
    mock_docker.DockerClient.assert_called_once_with(
        base_url="unix:///tmp/docker.sock"
    )


def test_get_client_falls_back_to_context_host():
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    context_host = "unix:///Users/test/.docker/run/docker.sock"

    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker, patch(
        "hivemind.sandbox.docker_runner.subprocess.run"
    ) as mock_run:
        mock_docker.from_env.side_effect = RuntimeError("no default docker socket")
        mock_docker.DockerClient.return_value = mock_client
        mock_docker.errors = __import__("docker").errors
        mock_run.side_effect = [
            SimpleNamespace(stdout="desktop-linux\n"),
            SimpleNamespace(
                stdout=(
                    '[{"Endpoints":{"docker":{"Host":"'
                    + context_host
                    + '"}}}]'
                )
            ),
        ]

        runner = DockerRunner(settings)
        client = runner._get_client()

    assert client is mock_client
    mock_docker.DockerClient.assert_called_once_with(base_url=context_host)


def test_install_bridge_only_egress_rules_builds_iptables_commands():
    settings = _make_settings(enforce_bridge_only_egress=True)
    runner = DockerRunner(settings)

    class _Container:
        id = "abcd1234efgh5678"
        attrs = {
            "NetworkSettings": {
                "Networks": {
                    "test-hivemind": {"IPAddress": "172.28.0.15"}
                }
            }
        }

        def reload(self):
            return None

    container = _Container()
    seen: list[list[str]] = []

    with patch("hivemind.sandbox.docker_runner.subprocess.run") as mock_run:
        mock_run.side_effect = lambda cmd, **kwargs: seen.append(cmd)
        applied = runner._install_bridge_only_egress_rules(
            container,
            "test-hivemind",
            "172.28.0.1",
            8100,
        )

    assert len(applied) == 2
    assert seen[0][:4] == ["iptables", "-I", "DOCKER-USER", "1"]
    assert "172.28.0.15" in seen[0]
    assert "172.28.0.1" in seen[0]
    assert "8100" in seen[0]
    assert seen[1][:4] == ["iptables", "-I", "DOCKER-USER", "2"]
    assert "-j" in seen[1] and "DROP" in seen[1]


def test_remove_firewall_rules_converts_to_delete_specs():
    settings = _make_settings(enforce_bridge_only_egress=True)
    runner = DockerRunner(settings)
    calls: list[list[str]] = []

    applied = [
        [
            "iptables",
            "-I",
            "DOCKER-USER",
            "1",
            "-s",
            "172.28.0.15",
            "-d",
            "172.28.0.1",
            "-p",
            "tcp",
            "--dport",
            "8100",
            "-j",
            "ACCEPT",
        ],
        [
            "iptables",
            "-I",
            "DOCKER-USER",
            "2",
            "-s",
            "172.28.0.15",
            "-j",
            "DROP",
        ],
    ]

    with patch("hivemind.sandbox.docker_runner.subprocess.run") as mock_run:
        mock_run.side_effect = lambda cmd, **kwargs: calls.append(cmd)
        runner._remove_firewall_rules(applied)

    assert calls[0][0:3] == ["iptables", "-D", "DOCKER-USER"]
    assert "DROP" in calls[0]
    assert calls[1][0:3] == ["iptables", "-D", "DOCKER-USER"]
    assert "ACCEPT" in calls[1]


# ── Image filesystem extraction tests ──


def _make_tar(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf.read()


class MockExtractionContainer:
    def __init__(self, archives: dict[str, bytes] | None = None):
        self._archives = archives or {}
        self.removed = False

    def get_archive(self, path):
        if path not in self._archives:
            import docker.errors as de
            raise de.NotFound(f"{path} not found")
        data = self._archives[path]
        return iter([data]), {"size": len(data)}

    def remove(self, force=False):
        self.removed = True


class MockExtractionContainers:
    def __init__(self, container):
        self._container = container

    def create(self, image, **kwargs):
        return self._container

    def list(self, all=False, filters=None):
        return []


def test_extract_image_files_basic():
    tar_data = _make_tar({
        "app/agent.py": "import httpx\nprint('hello')\n",
        "app/lib/utils.py": "def helper(): pass\n",
    })
    container = MockExtractionContainer(archives={"/app": tar_data})
    mock_client = MagicMock()
    mock_client.containers = MockExtractionContainers(container)
    mock_client.containers.list = lambda **kw: []

    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        files = runner.extract_image_files("test-image:v1")

    assert "app/agent.py" in files
    assert "app/lib/utils.py" in files
    assert "import httpx" in files["app/agent.py"]
    assert container.removed is True


def test_extract_skips_binary_files():
    tar_data = _make_tar({
        "app/agent.py": "print('hello')\n",
        "app/model.bin": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
    })
    container = MockExtractionContainer(archives={"/app": tar_data})
    mock_client = MagicMock()
    mock_client.containers = MockExtractionContainers(container)
    mock_client.containers.list = lambda **kw: []

    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        files = runner.extract_image_files("test-image:v1")

    assert "app/agent.py" in files
    assert "app/model.bin" not in files


def test_extract_skips_junk_dirs():
    tar_data = _make_tar({
        "app/agent.py": "print('hello')\n",
        "app/__pycache__/agent.cpython-312.pyc": "fake bytecode",
        "app/node_modules/pkg/index.js": "module.exports = {}",
    })
    container = MockExtractionContainer(archives={"/app": tar_data})
    mock_client = MagicMock()
    mock_client.containers = MockExtractionContainers(container)
    mock_client.containers.list = lambda **kw: []

    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        files = runner.extract_image_files("test-image:v1")

    assert "app/agent.py" in files
    assert len(files) == 1


def test_extract_respects_max_file_size():
    tar_data = _make_tar({
        "app/small.py": "x = 1\n",
        "app/big.py": "x" * 1000,
    })
    container = MockExtractionContainer(archives={"/app": tar_data})
    mock_client = MagicMock()
    mock_client.containers = MockExtractionContainers(container)
    mock_client.containers.list = lambda **kw: []

    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        files = runner.extract_image_files("test-image:v1", max_file_size=500)

    assert "app/small.py" in files
    assert "app/big.py" not in files


def test_extract_respects_max_archive_size():
    tar_data = _make_tar({"app/agent.py": "x" * 2000})
    container = MockExtractionContainer(archives={"/app": tar_data})
    mock_client = MagicMock()
    mock_client.containers = MockExtractionContainers(container)
    mock_client.containers.list = lambda **kw: []

    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        files = runner.extract_image_files("test-image:v1", max_archive_size=100)

    assert files == {}


def test_extract_fallback_to_root():
    tar_data = _make_tar({
        "home/agent/main.py": "print('root fallback')\n",
        "usr/bin/python": "not extracted",
        "etc/passwd": "not extracted",
    })
    container = MockExtractionContainer(archives={"/": tar_data})
    mock_client = MagicMock()
    mock_client.containers = MockExtractionContainers(container)
    mock_client.containers.list = lambda **kw: []

    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        files = runner.extract_image_files("test-image:v1")

    assert "home/agent/main.py" in files
    assert "usr/bin/python" not in files
    assert "etc/passwd" not in files


def test_extract_cleanup_on_error():
    container = MockExtractionContainer(archives={})
    mock_client = MagicMock()
    mock_client.containers = MockExtractionContainers(container)
    mock_client.containers.list = lambda **kw: []

    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        files = runner.extract_image_files("test-image:v1")

    assert files == {}
    assert container.removed is True


# ── Image build tests ──


def test_build_image_returns_tag(tmp_path):
    """build_image should call docker build and return the tag."""
    # Create a Dockerfile in the tmp dir
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\nCOPY . /app\n")
    (tmp_path / "agent.py").write_text("print('hello')\n")

    mock_client = MagicMock()
    mock_client.images.build.return_value = (MagicMock(), [])

    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        tag = runner.build_image(str(tmp_path), "hivemind-agent-abc123:latest")

    assert tag == "hivemind-agent-abc123:latest"
    mock_client.images.build.assert_called_once_with(
        path=str(tmp_path),
        tag="hivemind-agent-abc123:latest",
        rm=True,
        forcerm=True,
        pull=False,
        labels={CONTAINER_LABEL: CONTAINER_LABEL_VALUE},
        container_limits={
            "memory": 1024 * 1024 * 1024,
            "memswap": 1024 * 1024 * 1024,
            "cpushares": 512,
        },
        network_mode="none",
    )


def test_build_image_ensures_local_agent_base_when_required(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "FROM hivemind-agent-base:latest\nCOPY . /app\n"
    )
    (tmp_path / "agent.py").write_text("print('hello')\n")

    mock_client = MagicMock()
    mock_client.images.build.return_value = (MagicMock(), [])

    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker, patch(
        "hivemind.agent_base_bootstrap.ensure_agent_base_image",
        return_value=True,
    ) as ensure_base:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        tag = runner.build_image(str(tmp_path), "hivemind-agent-abc123:latest")

    assert tag == "hivemind-agent-abc123:latest"
    ensure_base.assert_called_once_with()
    mock_client.images.build.assert_called_once()


def test_build_image_ensures_hermes_base_before_generic_base(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "FROM hivemind-agent-base-hermes:latest\nCOPY . /app\n"
    )
    (tmp_path / "agent.py").write_text("print('hello')\n")

    mock_client = MagicMock()
    mock_client.images.build.return_value = (MagicMock(), [])

    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker, patch(
        "hivemind.agent_base_bootstrap.ensure_agent_base_hermes_image",
        return_value=True,
    ) as ensure_hermes_base, patch(
        "hivemind.agent_base_bootstrap.ensure_agent_base_image",
        return_value=True,
    ) as ensure_claude_base:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        tag = runner.build_image(str(tmp_path), "hivemind-agent-abc123:latest")

    assert tag == "hivemind-agent-abc123:latest"
    ensure_hermes_base.assert_called_once_with()
    ensure_claude_base.assert_not_called()
    mock_client.images.build.assert_called_once()


def test_build_image_ensures_hermes_base_from_arg_default(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "ARG HIVEMIND_AGENT_BASE_HERMES_IMAGE=hivemind-agent-base-hermes:latest\n"
        "FROM ${HIVEMIND_AGENT_BASE_HERMES_IMAGE}\n"
        "COPY . /app\n"
    )
    (tmp_path / "agent.py").write_text("print('hello')\n")

    mock_client = MagicMock()
    mock_client.images.build.return_value = (MagicMock(), [])

    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker, patch(
        "hivemind.agent_base_bootstrap.ensure_agent_base_hermes_image",
        return_value=True,
    ) as ensure_hermes_base, patch(
        "hivemind.agent_base_bootstrap.ensure_agent_base_image",
        return_value=True,
    ) as ensure_claude_base:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        tag = runner.build_image(str(tmp_path), "hivemind-agent-abc123:latest")

    assert tag == "hivemind-agent-abc123:latest"
    ensure_hermes_base.assert_called_once_with()
    ensure_claude_base.assert_not_called()
    mock_client.images.build.assert_called_once()


def test_pull_image_returns_true_on_success():
    mock_client = MagicMock()
    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        assert runner.pull_image("ghcr.io/example/agent:latest") is True

    mock_client.images.pull.assert_called_once_with(
        "ghcr.io/example/agent:latest"
    )


def test_pull_image_returns_false_on_error():
    mock_client = MagicMock()
    mock_client.images.pull.side_effect = RuntimeError("denied")
    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        assert runner.pull_image("ghcr.io/example/agent:latest") is False


def test_build_image_rejects_missing_dockerfile(tmp_path):
    """build_image should raise ValueError if no Dockerfile exists."""
    (tmp_path / "agent.py").write_text("print('hello')\n")

    settings = _make_settings()

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = MagicMock()
        mock_docker.errors = __import__("docker").errors

        runner = DockerRunner(settings)
        with pytest.raises(ValueError, match="No Dockerfile found"):
            runner.build_image(str(tmp_path), "test:latest")


@pytest.mark.asyncio
async def test_build_image_async_returns_tag(tmp_path):
    """Async build should use a cancellable worker process."""
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

        def kill(self):
            raise AssertionError("should not kill successful build")

    settings = _make_settings(docker_host="unix:///tmp/docker.sock")

    with patch(
        "hivemind.sandbox.docker_runner.asyncio.create_subprocess_exec",
        return_value=_Proc(),
    ) as create_proc:
        runner = DockerRunner(settings)
        tag = await runner.build_image_async(str(tmp_path), "test:latest")

    assert tag == "test:latest"
    args = create_proc.call_args.args
    assert args[:3] == (
        sys.executable,
        "-m",
        "hivemind.sandbox.docker_build_worker",
    )
    assert "--path" in args
    assert str(tmp_path) in args
    assert "--tag" in args
    assert "test:latest" in args
    assert "--network" in args
    assert "none" in args
    assert "--memory-mb" in args
    assert "1024" in args
    assert "--cpu-shares" in args
    assert "512" in args
    assert "--docker-host" in args
    assert "unix:///tmp/docker.sock" in args


@pytest.mark.asyncio
async def test_build_image_async_timeout_kills_worker_process(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")

    class _Proc:
        returncode = None

        def __init__(self):
            self.killed = False
            self.calls = 0

        async def communicate(self):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            self.killed = True

    proc = _Proc()
    settings = _make_settings(
        docker_build_timeout_seconds=1,
        docker_host="unix:///tmp/docker.sock",
    )

    with patch(
        "hivemind.sandbox.docker_runner.asyncio.create_subprocess_exec",
        return_value=proc,
    ):
        runner = DockerRunner(settings)
        with pytest.raises(TimeoutError, match="timed out"):
            await runner.build_image_async(str(tmp_path), "test:latest")

    assert proc.killed is True


# ── ensure_image_async tests ──
#
# Covers the rebuild-from-stored-context path that fires after a Phala
# compose update wipes /var/lib/docker. Source survives in pgdata, so
# the next invocation rebuilds the per-agent image transparently.


@pytest.mark.asyncio
async def test_ensure_image_async_no_op_when_present():
    mock_client = MagicMock()
    mock_client.images.get.return_value = MagicMock()  # image present

    settings = _make_settings()
    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = __import__("docker").errors
        runner = DockerRunner(settings)
        rebuilt = await runner.ensure_image_async(
            "hivemind-agent-x:latest",
            {"Dockerfile": "FROM scratch\n", "agent.py": "print(1)\n"},
        )

    assert rebuilt is False
    mock_client.images.build.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_image_async_rebuilds_when_missing():
    import docker.errors as docker_errors
    import os

    captured_contents: dict[str, str] = {}

    def _fake_build(**kwargs):
        # The runner uses TemporaryDirectory, so the build dir only exists
        # *during* this call — capture what's inside before it's torn down.
        build_dir = kwargs["path"]
        for root, _dirs, files in os.walk(build_dir):
            for fname in files:
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, build_dir)
                with open(full) as f:
                    captured_contents[rel] = f.read()
        return (MagicMock(), [])

    mock_client = MagicMock()
    mock_client.images.get.side_effect = docker_errors.ImageNotFound("missing")
    async def _fake_build_async(build_path, tag):
        _fake_build(path=build_path, tag=tag)
        return tag

    settings = _make_settings()
    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = docker_errors
        runner = DockerRunner(settings)
        runner._build_image_worker_async = _fake_build_async
        rebuilt = await runner.ensure_image_async(
            "hivemind-agent-x:latest",
            {
                "Dockerfile": "FROM scratch\n",
                "agent.py": "print(1)\n",
                "pkg/util.py": "X = 1\n",
            },
        )

    assert rebuilt is True
    assert captured_contents == {
        "Dockerfile": "FROM scratch\n",
        "agent.py": "print(1)\n",
        os.path.join("pkg", "util.py"): "X = 1\n",
    }


@pytest.mark.asyncio
async def test_ensure_image_async_errors_when_no_files():
    import docker.errors as docker_errors

    mock_client = MagicMock()
    mock_client.images.get.side_effect = docker_errors.ImageNotFound("missing")

    settings = _make_settings()
    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = docker_errors
        runner = DockerRunner(settings)
        with pytest.raises(ValueError, match="re-upload"):
            await runner.ensure_image_async("hivemind-agent-x:latest", None)


@pytest.mark.asyncio
async def test_ensure_image_async_errors_when_dockerfile_missing():
    import docker.errors as docker_errors

    mock_client = MagicMock()
    mock_client.images.get.side_effect = docker_errors.ImageNotFound("missing")

    settings = _make_settings()
    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = docker_errors
        runner = DockerRunner(settings)
        with pytest.raises(ValueError, match="lacks a Dockerfile"):
            await runner.ensure_image_async(
                "hivemind-agent-x:latest",
                {"agent.py": "print(1)\n"},
            )


@pytest.mark.asyncio
async def test_ensure_image_async_rejects_path_traversal():
    import docker.errors as docker_errors

    mock_client = MagicMock()
    mock_client.images.get.side_effect = docker_errors.ImageNotFound("missing")

    settings = _make_settings()
    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = docker_errors
        runner = DockerRunner(settings)
        with pytest.raises(ValueError, match="unsafe path"):
            await runner.ensure_image_async(
                "hivemind-agent-x:latest",
                {
                    "Dockerfile": "FROM scratch\n",
                    "../escape.py": "print(1)\n",
                },
            )

    mock_client.images.build.assert_not_called()


def test_docker_build_worker_uses_build_limits(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")

    mock_client = MagicMock()
    mock_client.images.build.return_value = (MagicMock(), [])

    with patch("hivemind.sandbox.docker_build_worker.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        rc = docker_build_worker.main(
            [
                "--path",
                str(tmp_path),
                "--tag",
                "test:latest",
                "--network",
                "none",
                "--memory-mb",
                "1024",
                "--cpu-shares",
                "512",
            ]
        )

    assert rc == 0
    mock_client.images.build.assert_called_once_with(
        path=str(tmp_path),
        tag="test:latest",
        rm=True,
        forcerm=True,
        pull=False,
        labels={CONTAINER_LABEL: CONTAINER_LABEL_VALUE},
        container_limits={
            "memory": 1024 * 1024 * 1024,
            "memswap": 1024 * 1024 * 1024,
            "cpushares": 512,
        },
        network_mode="none",
    )
    mock_client.close.assert_called_once()

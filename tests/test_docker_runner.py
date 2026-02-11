import asyncio
import io
import tarfile
from dataclasses import dataclass
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from hivemind.sandbox.docker_runner import ContainerResult, DockerRunner
from hivemind.sandbox.models import AgentConfig, SandboxSettings


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
    """Mock Docker container for testing."""

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
    """Mock docker.DockerClient."""

    def __init__(self, container=None):
        self._container = container or MockContainer()
        self.containers = MockContainers(self._container)
        self.networks = MockNetworks()


class MockContainers:
    def __init__(self, container):
        self._container = container
        self.run_kwargs = None

    def run(self, **kwargs):
        self.run_kwargs = kwargs
        return self._container

    def list(self, all=False, filters=None):
        return []


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
    """DockerRunner creates container with correct env vars and captures output."""
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
            prompt="test prompt",
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            work_dir="/tmp/hm-test",
        )

    assert isinstance(result, ContainerResult)
    assert result.stdout == "hello from agent"
    assert result.exit_code == 0
    assert result.timed_out is False

    # Verify container was created with correct args
    kwargs = mock_client.containers.run_kwargs
    assert kwargs["image"] == "myorg/test-agent:v1"
    assert kwargs["environment"]["SESSION_TOKEN"] == "tok-123"
    assert kwargs["environment"]["PROMPT"] == "test prompt"
    assert kwargs["environment"]["DOCUMENT_TEXT"] == "test prompt"
    assert "BRIDGE_URL" in kwargs["environment"]
    assert kwargs["mem_limit"] == "256m"
    assert kwargs["detach"] is True

    # Container should be cleaned up
    assert container.removed is True


@pytest.mark.asyncio
async def test_run_agent_with_extra_env():
    """Extra env vars are passed through to the container."""
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
            prompt="test",
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            work_dir="/tmp/hm-test",
            extra_env={"QUERIER_ID": "alice", "CUSTOM_VAR": "value"},
        )

    env = mock_client.containers.run_kwargs["environment"]
    assert env["QUERIER_ID"] == "alice"
    assert env["CUSTOM_VAR"] == "value"


@pytest.mark.asyncio
async def test_run_agent_with_entrypoint():
    """Custom entrypoint is forwarded to Docker."""
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
            prompt="test",
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            work_dir="/tmp/hm-test",
        )

    assert mock_client.containers.run_kwargs["entrypoint"] == "/custom/run.sh"


@pytest.mark.asyncio
async def test_run_agent_timeout():
    """Container is killed when timeout is exceeded."""

    class SlowContainer(MockContainer):
        def wait(self):
            import time
            time.sleep(10)  # will be interrupted by asyncio timeout
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
            prompt="test",
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            work_dir="/tmp/hm-test",
        )

    assert result.timed_out is True
    assert result.exit_code == -1
    assert container.killed is True
    assert container.removed is True


@pytest.mark.asyncio
async def test_run_agent_oom():
    """OOM kill (exit code 137) is detected and noted in stderr."""
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
            prompt="test",
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            work_dir="/tmp/hm-test",
        )

    assert result.exit_code == 137
    assert "OOM" in result.stderr


@pytest.mark.asyncio
async def test_run_agent_image_not_found():
    """Missing Docker image returns error result."""
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
            prompt="test",
            bridge_url="http://0.0.0.0:9999",
            session_token="tok-123",
            work_dir="/tmp/hm-test",
        )

    assert result.exit_code == -1
    assert "not found" in result.stderr.lower()


@pytest.mark.asyncio
async def test_cleanup_always_runs():
    """Container is removed even if an error occurs during wait."""

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
                prompt="test",
                bridge_url="http://0.0.0.0:9999",
                session_token="tok-123",
                work_dir="/tmp/hm-test",
            )

    # Container should still be cleaned up
    assert container.removed is True


def test_container_result_dataclass():
    """ContainerResult holds all expected fields."""
    r = ContainerResult(stdout="out", stderr="err", exit_code=0, timed_out=False)
    assert r.stdout == "out"
    assert r.stderr == "err"
    assert r.exit_code == 0
    assert r.timed_out is False


@pytest.mark.asyncio
async def test_network_labels():
    """Network is created with hivemind label."""
    mock_client = MockDockerClient()

    settings = _make_settings()

    # Make network.get raise NotFound to force creation
    import docker.errors as docker_errors

    def raise_not_found(name):
        raise docker_errors.NotFound("not found")

    mock_client.networks.get = raise_not_found

    with patch("hivemind.sandbox.docker_runner.docker") as mock_docker:
        mock_docker.from_env.return_value = mock_client
        mock_docker.errors = docker_errors

        runner = DockerRunner(settings)
        # Trigger network creation
        runner._ensure_network()

    assert mock_client.networks.created is True


# ── Image filesystem extraction tests ──


def _make_tar(files: dict[str, str | bytes]) -> bytes:
    """Create an in-memory tar archive from a dict of path → content."""
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
    """Mock container for extraction (never started)."""

    def __init__(self, archives: dict[str, bytes] | None = None):
        self._archives = archives or {}
        self.removed = False

    def get_archive(self, path):
        if path not in self._archives:
            import docker.errors as de
            raise de.NotFound(f"{path} not found")
        # get_archive returns (chunks_iterator, stat_dict)
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
    """Extracts text files from /app in the image."""
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
    """Binary files (non-UTF-8) are skipped."""
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
    """Files in __pycache__, node_modules etc are skipped."""
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
    assert len(files) == 1  # only agent.py


def test_extract_respects_max_file_size():
    """Individual files larger than max_file_size are skipped."""
    tar_data = _make_tar({
        "app/small.py": "x = 1\n",
        "app/big.py": "x" * 1000,  # 1000 bytes
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
        files = runner.extract_image_files(
            "test-image:v1", max_file_size=500,
        )

    assert "app/small.py" in files
    assert "app/big.py" not in files


def test_extract_fallback_to_root():
    """Falls back to / when /app doesn't exist, skipping system dirs."""
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
    """Container is removed even if extraction fails."""
    container = MockExtractionContainer(archives={})  # no /app, no /
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

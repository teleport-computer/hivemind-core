"""Integration tests for Docker-based agent registration and source extraction.

These tests require Docker to be running. They are skipped automatically
if Docker is not available.

Test image: hivemind-test-agent:latest (built from tests/fixtures/Dockerfile.test-agent)
Build it with:
    docker build -t hivemind-test-agent:latest -f tests/fixtures/Dockerfile.test-agent tests/fixtures/
"""
import json
import os
import subprocess

import httpx
import pytest
import pytest_asyncio

from hivemind.config import Settings
from hivemind.db import Database
from hivemind.sandbox.agents import AgentStore
from hivemind.sandbox.docker_runner import DockerRunner
from hivemind.sandbox.models import AgentConfig, SandboxSettings
from hivemind.server import create_app
from hivemind.tools import build_agent_file_tools

# ── Skip if Docker unavailable ──

try:
    import docker

    def _docker_client():
        try:
            client = docker.from_env()
            client.ping()
            return client
        except Exception:
            try:
                context = subprocess.run(
                    ["docker", "context", "show"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout.strip()
                inspected = subprocess.run(
                    ["docker", "context", "inspect", context],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                payload = json.loads(inspected.stdout)
                host = payload[0]["Endpoints"]["docker"]["Host"]
                client = docker.DockerClient(base_url=host)
                client.ping()
                return client
            except Exception:
                raise

    _client = _docker_client()
    DOCKER_AVAILABLE = True
except Exception:
    DOCKER_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="Docker not available"
)

TEST_IMAGE = "hivemind-test-agent:latest"


def _has_test_image() -> bool:
    try:
        client = _docker_client()
        client.images.get(TEST_IMAGE)
        return True
    except Exception:
        return False


skip_no_image = pytest.mark.skipif(
    not _has_test_image(),
    reason=f"Test image {TEST_IMAGE} not built. "
    "Run: docker build -t hivemind-test-agent:latest "
    "-f tests/fixtures/Dockerfile.test-agent tests/fixtures/",
)


def _make_settings():
    return SandboxSettings(
        bridge_host="0.0.0.0",
        docker_network_name="hivemind-test-net",
        container_memory_mb=256,
        container_cpu_quota=1.0,
        global_max_llm_calls=50,
        global_max_tokens=200_000,
        global_timeout_seconds=300,
    )


# ── DockerRunner.extract_image_files() integration tests ──


@skip_no_image
class TestExtractImageFiles:
    def test_extracts_known_files(self):
        """Extraction from a real image returns the files we put there."""
        runner = DockerRunner(_make_settings())
        files = runner.extract_image_files(TEST_IMAGE)

        paths = set(files.keys())
        assert any("agent.py" in p for p in paths)
        assert any("utils.py" in p for p in paths)
        assert any("requirements.txt" in p for p in paths)
        assert any("README.md" in p for p in paths)

    def test_file_contents_are_readable(self):
        """Extracted file contents are valid UTF-8 strings."""
        runner = DockerRunner(_make_settings())
        files = runner.extract_image_files(TEST_IMAGE)

        for path, content in files.items():
            assert isinstance(content, str)
            assert len(content) > 0

    def test_agent_py_has_expected_content(self):
        """The agent.py file contains the code we wrote in the Dockerfile."""
        runner = DockerRunner(_make_settings())
        files = runner.extract_image_files(TEST_IMAGE)

        agent_py = next(
            (v for k, v in files.items() if k.endswith("agent.py")), None
        )
        assert agent_py is not None
        assert "import" in agent_py
        assert "json" in agent_py

    def test_no_system_files_extracted(self):
        """System directories (/usr, /etc, /bin) are not in extracted files."""
        runner = DockerRunner(_make_settings())
        files = runner.extract_image_files(TEST_IMAGE)

        for path in files:
            assert not path.startswith("usr/")
            assert not path.startswith("etc/")
            assert not path.startswith("bin/")
            assert not path.startswith("sbin/")
            assert not path.startswith("lib/")

    def test_container_cleaned_up(self):
        """No orphan containers left after extraction."""
        runner = DockerRunner(_make_settings())
        runner.extract_image_files(TEST_IMAGE)

        client = _docker_client()
        containers = client.containers.list(
            all=True,
            filters={"label": "managed-by=hivemind"},
        )
        assert len(containers) == 0

    def test_max_file_size_respected(self):
        """Files larger than max_file_size are skipped."""
        runner = DockerRunner(_make_settings())
        files = runner.extract_image_files(TEST_IMAGE, max_file_size=5)
        assert len(files) == 0

    def test_nonexistent_image_raises(self):
        """Trying to extract from a nonexistent image raises an error."""
        runner = DockerRunner(_make_settings())
        with pytest.raises(docker.errors.ImageNotFound):
            runner.extract_image_files("nonexistent-image:v999")

    @pytest.mark.asyncio
    async def test_async_wrapper(self):
        """The async wrapper returns the same results."""
        runner = DockerRunner(_make_settings())
        files = await runner.extract_image_files_async(TEST_IMAGE)
        assert len(files) > 0
        assert any("agent.py" in p for p in files)


# ── Full registration flow via API ──


@skip_no_image
class TestAgentRegistrationAPI:
    @pytest_asyncio.fixture
    async def sandbox_client(self):
        test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
        if not test_dsn:
            pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")
        settings = Settings(
            database_url=test_dsn,
            api_key="",
            autoload_default_agents=False,
        )
        from hivemind.core import Hivemind

        app = create_app(settings)
        hm = Hivemind(settings)
        app.state.hivemind = hm
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
        await hm.close()

    @pytest.mark.asyncio
    async def test_register_extracts_files(self, sandbox_client):
        """POST /v1/agents extracts files and returns count."""
        resp = await sandbox_client.post(
            "/v1/agents",
            json={
                "name": "test-agent",
                "image": TEST_IMAGE,
                "description": "A test agent",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "agent_id" in data
        assert data["files_extracted"] > 0

    @pytest.mark.asyncio
    async def test_get_agent_does_not_leak_source(self, sandbox_client):
        """GET /v1/agents/{id} returns config but no source files."""
        resp = await sandbox_client.post(
            "/v1/agents",
            json={"name": "test-agent", "image": TEST_IMAGE},
        )
        agent_id = resp.json()["agent_id"]

        resp = await sandbox_client.get(f"/v1/agents/{agent_id}")
        assert resp.status_code == 200
        data = resp.json()

        assert data["name"] == "test-agent"
        assert data["image"] == TEST_IMAGE

        assert "files" not in data
        assert "content" not in data
        assert "agent.py" not in json.dumps(data)

    @pytest.mark.asyncio
    async def test_list_agents_does_not_leak_source(self, sandbox_client):
        """GET /v1/agents returns configs but no source files."""
        await sandbox_client.post(
            "/v1/agents",
            json={"name": "test-agent", "image": TEST_IMAGE},
        )

        resp = await sandbox_client.get("/v1/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1

        serialized = json.dumps(data)
        assert "import" not in serialized
        assert "def helper" not in serialized

    @pytest.mark.asyncio
    async def test_delete_agent_removes_files(self, sandbox_client):
        """DELETE /v1/agents/{id} removes both config and extracted files."""
        resp = await sandbox_client.post(
            "/v1/agents",
            json={"name": "test-agent", "image": TEST_IMAGE},
        )
        agent_id = resp.json()["agent_id"]

        resp = await sandbox_client.delete(f"/v1/agents/{agent_id}")
        assert resp.status_code == 200

        resp = await sandbox_client.get(f"/v1/agents/{agent_id}")
        assert resp.status_code == 404


# ── Scoping agent file tools with real extracted files ──


@skip_no_image
class TestScopingToolsWithRealFiles:
    @pytest.fixture
    def store_with_real_agent(self):
        """Register an agent and extract real files from the Docker image."""
        test_dsn = os.environ.get("HIVEMIND_TEST_DATABASE_URL", "")
        if not test_dsn:
            pytest.skip("HIVEMIND_TEST_DATABASE_URL not set")

        db = Database(test_dsn)
        store = AgentStore(db)
        store.upsert(AgentConfig(
            agent_id="real-qa",
            name="Real Query Agent",
            image=TEST_IMAGE,
        ))

        runner = DockerRunner(_make_settings())
        files = runner.extract_image_files(TEST_IMAGE)
        store.replace_files("real-qa", files)

        yield store
        db.close()

    def test_list_shows_real_files(self, store_with_real_agent):
        tools = build_agent_file_tools(store_with_real_agent, "real-qa")
        list_tool = next(t for t in tools if t.name == "list_query_agent_files")
        result = json.loads(list_tool.handler())

        paths = [f["path"] for f in result["files"]]
        assert any("agent.py" in p for p in paths)
        assert any("utils.py" in p for p in paths)

    def test_read_returns_real_content(self, store_with_real_agent):
        tools = build_agent_file_tools(store_with_real_agent, "real-qa")
        list_tool = next(t for t in tools if t.name == "list_query_agent_files")
        read_tool = next(t for t in tools if t.name == "read_query_agent_file")

        result = json.loads(list_tool.handler())
        agent_path = next(
            f["path"] for f in result["files"] if "agent.py" in f["path"]
        )

        content = read_tool.handler(file_path=agent_path)
        assert "import" in content
        assert "json" in content

    def test_read_nonexistent_file(self, store_with_real_agent):
        tools = build_agent_file_tools(store_with_real_agent, "real-qa")
        read_tool = next(t for t in tools if t.name == "read_query_agent_file")

        result = read_tool.handler(file_path="does_not_exist.py")
        assert "not found" in result.lower()

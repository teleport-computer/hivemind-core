import sqlite3
import tempfile
import os

import pytest

from hivemind.sandbox.agents import AgentStore
from hivemind.sandbox.models import AgentConfig


@pytest.fixture
def agent_env():
    """Create a temporary SQLite DB for agent tests."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    store = AgentStore(conn)

    yield store

    conn.close()
    os.unlink(db_path)


def _make_agent(agent_id="test-1", name="Test Agent", image="myorg/test:v1"):
    return AgentConfig(
        agent_id=agent_id,
        name=name,
        image=image,
    )


def test_create_and_get(agent_env):
    store = agent_env
    config = _make_agent()
    store.create(config)

    result = store.get("test-1")
    assert result is not None
    assert result.agent_id == "test-1"
    assert result.name == "Test Agent"
    assert result.image == "myorg/test:v1"
    assert result.entrypoint is None
    assert result.memory_mb == 256
    assert result.max_llm_calls == 20
    assert result.max_tokens == 100_000
    assert result.timeout_seconds == 120


def test_get_nonexistent(agent_env):
    store = agent_env
    assert store.get("nonexistent") is None


def test_list_agents(agent_env):
    store = agent_env
    store.create(_make_agent("a1", "Agent 1", "img/a1:latest"))
    store.create(_make_agent("a2", "Agent 2", "img/a2:latest"))

    agents = store.list_agents()
    assert len(agents) == 2
    # Most recent first
    assert agents[0].agent_id == "a2"


def test_delete(agent_env):
    store = agent_env
    config = _make_agent("del-1", "Deletable", "img/del:v1")
    store.create(config)

    assert store.delete("del-1") is True
    assert store.get("del-1") is None


def test_delete_nonexistent(agent_env):
    store = agent_env
    assert store.delete("nonexistent") is False


def test_custom_fields(agent_env):
    store = agent_env
    config = AgentConfig(
        agent_id="custom-1",
        name="Custom Agent",
        image="myorg/custom:v2",
        description="A custom agent",
        entrypoint="/run.sh",
        memory_mb=512,
        max_llm_calls=5,
        max_tokens=50_000,
        timeout_seconds=30,
    )
    store.create(config)

    result = store.get("custom-1")
    assert result.image == "myorg/custom:v2"
    assert result.description == "A custom agent"
    assert result.entrypoint == "/run.sh"
    assert result.memory_mb == 512
    assert result.max_llm_calls == 5
    assert result.max_tokens == 50_000
    assert result.timeout_seconds == 30


# ── Agent file storage tests ──


def test_save_and_list_files(agent_env):
    store = agent_env
    store.create(_make_agent("f1"))
    files = {"agent.py": "import httpx\n", "lib/utils.py": "def helper(): pass\n"}
    count = store.save_files("f1", files)
    assert count == 2

    listing = store.list_file_paths("f1")
    assert len(listing) == 2
    paths = [f["path"] for f in listing]
    assert "agent.py" in paths
    assert "lib/utils.py" in paths
    assert all(f["size_bytes"] > 0 for f in listing)


def test_read_file(agent_env):
    store = agent_env
    store.create(_make_agent("f2"))
    store.save_files("f2", {"main.py": "print('hello')"})

    content = store.read_file("f2", "main.py")
    assert content == "print('hello')"


def test_read_file_not_found(agent_env):
    store = agent_env
    store.create(_make_agent("f3"))
    assert store.read_file("f3", "nonexistent.py") is None


def test_list_files_empty(agent_env):
    store = agent_env
    store.create(_make_agent("f4"))
    assert store.list_file_paths("f4") == []


def test_delete_cascades_files(agent_env):
    store = agent_env
    store.create(_make_agent("f5"))
    store.save_files("f5", {"a.py": "aaa", "b.py": "bbb"})

    assert len(store.list_file_paths("f5")) == 2
    store.delete("f5")
    assert store.list_file_paths("f5") == []


def test_save_files_replaces_existing(agent_env):
    store = agent_env
    store.create(_make_agent("f6"))
    store.save_files("f6", {"main.py": "v1"})
    store.save_files("f6", {"main.py": "v2"})

    content = store.read_file("f6", "main.py")
    assert content == "v2"

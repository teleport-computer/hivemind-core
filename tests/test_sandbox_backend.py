from unittest.mock import AsyncMock

import pytest

import hivemind.sandbox.backend as backend_module
from hivemind.sandbox.docker_runner import ContainerResult
from hivemind.sandbox.models import AgentConfig, SandboxSettings
from hivemind.tools import Tool


def _settings(**overrides) -> SandboxSettings:
    data = {
        "bridge_host": "127.0.0.1",
        "docker_network_name": "test-net",
        "container_memory_mb": 256,
        "container_cpu_quota": 1.0,
        "global_max_llm_calls": 50,
        "global_max_tokens": 200_000,
        "global_timeout_seconds": 300,
    }
    data.update(overrides)
    return SandboxSettings(**data)


def _agent(**overrides) -> AgentConfig:
    data = {
        "agent_id": "qa-1",
        "name": "Query Agent",
        "image": "img:test",
        "timeout_seconds": 30,
    }
    data.update(overrides)
    return AgentConfig(**data)


def _tools() -> list[Tool]:
    return [
        Tool(
            name="list",
            description="list",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda: "[]",
        )
    ]


class _BridgeStub:
    def __init__(self, *args, **kwargs):
        pass

    async def start(self) -> int:
        return 9999

    async def stop(self):
        return None


def _patch_runner(monkeypatch, runner_cls):
    """Patch _create_runner to return instances of runner_cls."""
    monkeypatch.setattr(
        backend_module,
        "_create_runner",
        lambda settings, role="query": runner_cls(settings),
    )


@pytest.mark.asyncio
async def test_backend_returns_output_and_usage_on_success(monkeypatch):
    runner_instances = []
    bridge_events = {"started": 0, "stopped": 0}

    class _Runner:
        def __init__(self, settings):
            self.settings = settings
            self.run_kwargs = None
            runner_instances.append(self)

        async def run_agent(self, **kwargs):
            self.run_kwargs = kwargs
            return ContainerResult(
                stdout="final answer\n",
                stderr="",
                exit_code=0,
                timed_out=False,
            )

    class _Bridge:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def start(self) -> int:
            bridge_events["started"] += 1
            return 9999

        async def stop(self):
            bridge_events["stopped"] += 1
            return None

    _patch_runner(monkeypatch, _Runner)
    monkeypatch.setattr(backend_module, "BridgeServer", _Bridge)

    backend = backend_module.SandboxBackend(
        llm_client=AsyncMock(),
        llm_model="model",
        settings=_settings(container_memory_mb=128, global_timeout_seconds=20),
        agent=_agent(memory_mb=1024, timeout_seconds=120),
    )

    async def on_tool_call(name: str, args: dict) -> str:
        return "[]"

    output, usage = await backend.run(
        role="query",
        env={"QUERY_PROMPT": "hi", "CUSTOM_VAR": "x"},
        tools=_tools(),
        on_tool_call=on_tool_call,
        max_calls=7,
        max_tokens=900,
        return_budget_summary=True,
    )

    assert output == "final answer"
    assert usage["calls"] == 0
    assert usage["max_calls"] == 7
    assert usage["total_tokens"] == 0
    assert usage["max_tokens"] == 900
    assert bridge_events == {"started": 1, "stopped": 1}

    run_kwargs = runner_instances[0].run_kwargs
    env = run_kwargs["env"]
    assert env["BRIDGE_URL"] == "http://127.0.0.1:9999"
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:9999/v1"
    assert env["OPENAI_API_KEY"] == run_kwargs["session_token"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999"
    assert env["ANTHROPIC_API_KEY"] == run_kwargs["session_token"]
    assert env["AGENT_ROLE"] == "query"
    assert env["BUDGET_MAX_CALLS"] == "7"
    assert env["BUDGET_MAX_TOKENS"] == "900"
    assert env["CUSTOM_VAR"] == "x"

    capped_agent = run_kwargs["agent"]
    assert capped_agent.memory_mb == 128
    assert capped_agent.timeout_seconds == 20


@pytest.mark.asyncio
async def test_backend_empty_output_uses_sentinel(monkeypatch):
    class _Runner:
        def __init__(self, settings):
            self.settings = settings

        async def run_agent(self, **kwargs):
            return ContainerResult(
                stdout="   \n",
                stderr="",
                exit_code=0,
                timed_out=False,
            )

    _patch_runner(monkeypatch, _Runner)
    monkeypatch.setattr(backend_module, "BridgeServer", _BridgeStub)

    backend = backend_module.SandboxBackend(
        llm_client=AsyncMock(),
        llm_model="model",
        settings=_settings(),
        agent=_agent(),
    )

    async def on_tool_call(name: str, args: dict) -> str:
        return "[]"

    output = await backend.run(
        role="query",
        env={"QUERY_PROMPT": "hi"},
        tools=_tools(),
        on_tool_call=on_tool_call,
    )
    assert output == "(Agent produced no output)"


@pytest.mark.asyncio
async def test_backend_raises_on_agent_failure(monkeypatch):
    class _Runner:
        def __init__(self, settings):
            self.settings = settings

        async def run_agent(self, **kwargs):
            return ContainerResult(
                stdout="",
                stderr="boom",
                exit_code=-1,
                timed_out=False,
            )

    _patch_runner(monkeypatch, _Runner)
    monkeypatch.setattr(backend_module, "BridgeServer", _BridgeStub)

    backend = backend_module.SandboxBackend(
        llm_client=AsyncMock(),
        llm_model="model",
        settings=_settings(),
        agent=_agent(),
    )

    async def on_tool_call(name: str, args: dict) -> str:
        return "[]"

    with pytest.raises(ValueError, match="failed"):
        await backend.run(
            role="query",
            env={"QUERY_PROMPT": "hi"},
            tools=_tools(),
            on_tool_call=on_tool_call,
        )


@pytest.mark.asyncio
async def test_backend_raises_on_agent_timeout(monkeypatch):
    class _Runner:
        def __init__(self, settings):
            self.settings = settings

        async def run_agent(self, **kwargs):
            return ContainerResult(
                stdout="",
                stderr="timeout",
                exit_code=-1,
                timed_out=True,
            )

    _patch_runner(monkeypatch, _Runner)
    monkeypatch.setattr(backend_module, "BridgeServer", _BridgeStub)

    backend = backend_module.SandboxBackend(
        llm_client=AsyncMock(),
        llm_model="model",
        settings=_settings(),
        agent=_agent(),
    )

    async def on_tool_call(name: str, args: dict) -> str:
        return "[]"

    with pytest.raises(ValueError, match="timed out"):
        await backend.run(
            role="query",
            env={"QUERY_PROMPT": "hi"},
            tools=_tools(),
            on_tool_call=on_tool_call,
        )

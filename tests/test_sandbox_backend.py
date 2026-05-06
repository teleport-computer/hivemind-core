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


def test_agent_config_validates_harness():
    assert _agent(harness="HERMES").harness == "hermes"
    assert _agent(harness="").harness == "claude_code"
    with pytest.raises(ValueError, match="harness must be one of"):
        _agent(harness="bogus")


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
    assert env["HIVEMIND_MODEL"] == "model"
    assert "HIVEMIND_AGENT_ROLE" not in env
    assert env["BUDGET_MAX_CALLS"] == "7"
    assert env["BUDGET_MAX_TOKENS"] == "900"
    assert env["CUSTOM_VAR"] == "x"

    capped_agent = run_kwargs["agent"]
    assert capped_agent.memory_mb == 128
    assert capped_agent.timeout_seconds == 20


@pytest.mark.asyncio
async def test_backend_injects_hermes_role_and_protects_control_env(monkeypatch):
    runner_instances = []

    class _Runner:
        def __init__(self, settings):
            self.settings = settings
            self.run_kwargs = None
            runner_instances.append(self)

        async def run_agent(self, **kwargs):
            self.run_kwargs = kwargs
            return ContainerResult(
                stdout="ok\n",
                stderr="",
                exit_code=0,
                timed_out=False,
            )

    _patch_runner(monkeypatch, _Runner)
    monkeypatch.setattr(backend_module, "BridgeServer", _BridgeStub)

    backend = backend_module.SandboxBackend(
        llm_client=AsyncMock(),
        llm_model="operator/model",
        settings=_settings(),
        agent=_agent(harness="hermes"),
    )

    async def on_tool_call(name: str, args: dict) -> str:
        return "[]"

    await backend.run(
        role="scope",
        env={
            "BRIDGE_URL": "http://attacker",
            "SESSION_TOKEN": "attacker-token",
            "HIVEMIND_AGENT_ROLE": "query",
            "HIVEMIND_MODEL": "attacker/model",
        },
        tools=_tools(),
        on_tool_call=on_tool_call,
        run_id="run-1",
    )

    env = runner_instances[0].run_kwargs["env"]
    assert env["BRIDGE_URL"] == "http://127.0.0.1:9999"
    assert env["SESSION_TOKEN"] == runner_instances[0].run_kwargs["session_token"]
    assert env["HIVEMIND_AGENT_ROLE"] == "scope"
    assert env["HIVEMIND_MODEL"] == "operator/model"
    assert env["RUN_ID"] == "run-1"


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


@pytest.mark.asyncio
async def test_llm_caller_surfaces_reasoning_when_content_empty():
    """Reasoning models (Kimi-K2.6, DeepSeek-R1) may return content=null with
    chain-of-thought in a separate `reasoning` field. The bridge must fall back
    to that text so downstream agents see a non-empty assistant turn."""
    from openai.types.chat.chat_completion import ChatCompletion

    raw = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "kimi-k2-6",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning": "the user wants OK",
                    "tool_calls": [],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    fake_resp = ChatCompletion.model_validate(raw)

    client = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=fake_resp)

    backend = backend_module.SandboxBackend(
        llm_client=client,
        llm_model="kimi-k2-6",
        settings=_settings(),
        agent=_agent(),
    )
    result = await backend._llm_caller(messages=[{"role": "user", "content": "hi"}], max_tokens=10)

    assert result["content"] == "the user wants OK"
    assert result["reasoning"] == "the user wants OK"
    assert result["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_llm_caller_prefers_real_content_over_reasoning():
    """When both content and reasoning are present, content wins; reasoning is
    still surfaced for diagnostics but not duplicated into content."""
    from openai.types.chat.chat_completion import ChatCompletion

    raw = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "kimi-k2-6",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "OK",
                    "reasoning": "the user wants OK",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    fake_resp = ChatCompletion.model_validate(raw)

    client = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=fake_resp)

    backend = backend_module.SandboxBackend(
        llm_client=client, llm_model="kimi-k2-6", settings=_settings(), agent=_agent()
    )
    result = await backend._llm_caller(messages=[{"role": "user", "content": "hi"}], max_tokens=200)

    assert result["content"] == "OK"
    assert result["reasoning"] == "the user wants OK"


@pytest.mark.asyncio
async def test_llm_caller_handles_deepseek_reasoning_content_field():
    """DeepSeek-style providers expose chain-of-thought as `reasoning_content`
    instead of `reasoning`. Both should be honored."""
    from openai.types.chat.chat_completion import ChatCompletion

    raw = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-r1",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "let me think about this",
                },
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    fake_resp = ChatCompletion.model_validate(raw)

    client = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=fake_resp)

    backend = backend_module.SandboxBackend(
        llm_client=client, llm_model="deepseek-r1", settings=_settings(), agent=_agent()
    )
    result = await backend._llm_caller(messages=[{"role": "user", "content": "hi"}], max_tokens=10)

    assert result["content"] == "let me think about this"
    assert result["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_llm_caller_handles_reasoning_from_model_extra():
    from types import SimpleNamespace

    msg = SimpleNamespace(
        content=None,
        tool_calls=[],
        model_extra={"reasoning": "extra reasoning text"},
    )
    fake_resp = SimpleNamespace(
        choices=[
            SimpleNamespace(message=msg, finish_reason="stop")
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )

    client = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=fake_resp)

    backend = backend_module.SandboxBackend(
        llm_client=client,
        llm_model="reasoning-model",
        settings=_settings(),
        agent=_agent(),
    )
    result = await backend._llm_caller(
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=10,
    )

    assert result["content"] == "extra reasoning text"
    assert result["reasoning"] == "extra reasoning text"


@pytest.mark.asyncio
async def test_llm_caller_does_not_substitute_reasoning_when_tool_calls_present():
    """If the model emits tool_calls, an empty content string is normal —
    don't overwrite it with reasoning text (would confuse the agent loop)."""
    from openai.types.chat.chat_completion import ChatCompletion

    raw = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "kimi-k2-6",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning": "I should call the search tool",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "search", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    fake_resp = ChatCompletion.model_validate(raw)

    client = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=fake_resp)

    backend = backend_module.SandboxBackend(
        llm_client=client, llm_model="kimi-k2-6", settings=_settings(), agent=_agent()
    )
    result = await backend._llm_caller(messages=[{"role": "user", "content": "hi"}], max_tokens=200)

    assert result["content"] == ""
    assert result["reasoning"] == "I should call the search tool"
    assert result["tool_calls"][0]["function"]["name"] == "search"

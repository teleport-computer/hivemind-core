"""Tests for the agent loop in OpenRouterBackend."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import BadRequestError

from hivemind.backends.openrouter import (
    COMPACTION_CHAR_THRESHOLD,
    COMPACTION_KEEP_RECENT_TURNS,
    OpenRouterBackend,
)
from hivemind.tools import Tool


# -- helpers to build fake OpenAI responses --

def _make_choice(content="Hello", finish_reason="stop", tool_calls=None):
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    choice.finish_reason = finish_reason
    return choice


def _make_response(content="Hello", finish_reason="stop", tool_calls=None):
    resp = MagicMock()
    resp.choices = [_make_choice(content, finish_reason, tool_calls)]
    return resp


def _make_tool_call(tc_id, name, arguments_json):
    tc = MagicMock()
    tc.id = tc_id
    tc.function.name = name
    tc.function.arguments = arguments_json
    return tc


def _dummy_tool(name="test_tool", handler_return="tool result"):
    return Tool(
        name=name,
        description=f"A test tool called {name}",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda **kw: handler_return,
    )


# -- tests --

@pytest.mark.asyncio
async def test_simple_completion_no_tools():
    """Model responds with text, no tool calls — single turn."""
    client = AsyncMock()
    client.chat.completions.create.return_value = _make_response("The answer is 42")

    backend = OpenRouterBackend(client, "test-model", max_turns=5)
    result = await backend.run("What is the answer?", "You are helpful.", [], AsyncMock())

    assert result == "The answer is 42"
    assert client.chat.completions.create.call_count == 1


@pytest.mark.asyncio
async def test_single_tool_call_round_trip():
    """Model calls one tool, gets result, then responds with text."""
    tc = _make_tool_call("tc1", "search_index", '{"query": "hello"}')

    client = AsyncMock()
    client.chat.completions.create.side_effect = [
        _make_response(content="", finish_reason="tool_calls", tool_calls=[tc]),
        _make_response(content="Found results about hello"),
    ]

    tool_callback = AsyncMock(return_value="search results here")
    tools = [_dummy_tool("search_index")]

    backend = OpenRouterBackend(client, "test-model", max_turns=5)
    result = await backend.run("search hello", "system", tools, tool_callback)

    assert result == "Found results about hello"
    assert client.chat.completions.create.call_count == 2
    tool_callback.assert_called_once_with("search_index", {"query": "hello"})


@pytest.mark.asyncio
async def test_multi_turn_tool_calls():
    """Model calls tools across multiple turns before giving a final answer."""
    tc1 = _make_tool_call("tc1", "search_index", '{"query": "foo"}')
    tc2 = _make_tool_call("tc2", "read_record", '{"record_id": "abc"}')

    client = AsyncMock()
    client.chat.completions.create.side_effect = [
        _make_response(content="", finish_reason="tool_calls", tool_calls=[tc1]),
        _make_response(content="", finish_reason="tool_calls", tool_calls=[tc2]),
        _make_response(content="Final answer from records"),
    ]

    tool_callback = AsyncMock(return_value="data")
    tools = [_dummy_tool("search_index"), _dummy_tool("read_record")]

    backend = OpenRouterBackend(client, "test-model", max_turns=5)
    result = await backend.run("question", "system", tools, tool_callback)

    assert result == "Final answer from records"
    assert client.chat.completions.create.call_count == 3
    assert tool_callback.call_count == 2


@pytest.mark.asyncio
async def test_parallel_tool_execution():
    """Multiple tool calls in one response are executed concurrently."""
    tc1 = _make_tool_call("tc1", "search_index", '{"query": "alpha"}')
    tc2 = _make_tool_call("tc2", "search_index", '{"query": "beta"}')
    tc3 = _make_tool_call("tc3", "read_record", '{"record_id": "r1"}')

    client = AsyncMock()
    client.chat.completions.create.side_effect = [
        # Model returns 3 tool calls at once
        _make_response(content="", finish_reason="tool_calls", tool_calls=[tc1, tc2, tc3]),
        _make_response(content="Combined results"),
    ]

    call_order = []

    async def on_tool_call(name, args):
        call_order.append(name)
        return f"result for {name}"

    tools = [_dummy_tool("search_index"), _dummy_tool("read_record")]

    backend = OpenRouterBackend(client, "test-model", max_turns=5)
    result = await backend.run("question", "system", tools, on_tool_call)

    assert result == "Combined results"
    # All 3 tool calls should have been executed
    assert len(call_order) == 3
    # All 3 tool results should be in the messages sent to the second LLM call
    second_call_messages = client.chat.completions.create.call_args_list[1][1]["messages"]
    tool_msgs = [m for m in second_call_messages if isinstance(m, dict) and m.get("role") == "tool"]
    assert len(tool_msgs) == 3


@pytest.mark.asyncio
async def test_context_overflow_recovery():
    """BadRequestError with context_length triggers compaction and retry."""
    tc = _make_tool_call("tc1", "search_index", '{"query": "test"}')

    # Build a backend with a client that fails on first call, succeeds after compaction
    client = AsyncMock()

    # First call: tool call response (normal)
    # Second call: context_length_exceeded error
    # Third call (after compaction): success
    error = BadRequestError(
        message="This model's maximum context length is exceeded",
        response=MagicMock(status_code=400, headers={}),
        body={"error": {"message": "context_length exceeded"}},
    )
    client.chat.completions.create.side_effect = [
        _make_response(content="", finish_reason="tool_calls", tool_calls=[tc]),
        error,
        _make_response(content="Recovered after compaction"),
    ]

    tool_callback = AsyncMock(return_value="x" * 100_000)
    tools = [_dummy_tool("search_index")]

    # Need enough turns for compaction to work — seed some history
    backend = OpenRouterBackend(client, "test-model", max_turns=10)
    result = await backend.run("question", "system", tools, tool_callback)

    assert result == "Recovered after compaction"
    # Should have made 3 LLM calls: initial, failed, retried after compaction
    assert client.chat.completions.create.call_count == 3


@pytest.mark.asyncio
async def test_max_turns_exhaustion():
    """Loop stops after max_turns even if model keeps calling tools."""
    tc = _make_tool_call("tc1", "search_index", '{"query": "loop"}')

    client = AsyncMock()
    client.chat.completions.create.return_value = _make_response(
        content="partial", finish_reason="tool_calls", tool_calls=[tc]
    )

    tool_callback = AsyncMock(return_value="result")
    tools = [_dummy_tool("search_index")]

    backend = OpenRouterBackend(client, "test-model", max_turns=3)
    result = await backend.run("question", "system", tools, tool_callback)

    assert result == "partial"
    assert client.chat.completions.create.call_count == 3


@pytest.mark.asyncio
async def test_malformed_tool_arguments():
    """Malformed JSON in tool args doesn't crash — returns error to model."""
    tc = _make_tool_call("tc1", "search_index", "not valid json{{{")

    client = AsyncMock()
    client.chat.completions.create.side_effect = [
        _make_response(content="", finish_reason="tool_calls", tool_calls=[tc]),
        _make_response(content="I see there was an error"),
    ]

    tool_callback = AsyncMock()
    tools = [_dummy_tool("search_index")]

    backend = OpenRouterBackend(client, "test-model", max_turns=5)
    result = await backend.run("question", "system", tools, tool_callback)

    assert result == "I see there was an error"
    tool_callback.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_tool_name():
    """Model calls a tool that doesn't exist — returns error, doesn't crash."""
    tc = _make_tool_call("tc1", "nonexistent_tool", '{"foo": "bar"}')

    client = AsyncMock()
    client.chat.completions.create.side_effect = [
        _make_response(content="", finish_reason="tool_calls", tool_calls=[tc]),
        _make_response(content="Let me try differently"),
    ]

    async def on_tool_call(name, args):
        raise KeyError(name)

    tools = [_dummy_tool("search_index")]

    backend = OpenRouterBackend(client, "test-model", max_turns=5)
    result = await backend.run("question", "system", tools, on_tool_call)

    assert result == "Let me try differently"


@pytest.mark.asyncio
async def test_tool_handler_exception():
    """Tool handler raises an exception — error is fed back to model."""
    tc = _make_tool_call("tc1", "search_index", '{"query": "crash"}')

    client = AsyncMock()
    client.chat.completions.create.side_effect = [
        _make_response(content="", finish_reason="tool_calls", tool_calls=[tc]),
        _make_response(content="Recovered from error"),
    ]

    async def on_tool_call(name, args):
        raise RuntimeError("database connection failed")

    tools = [_dummy_tool("search_index")]

    backend = OpenRouterBackend(client, "test-model", max_turns=5)
    result = await backend.run("question", "system", tools, on_tool_call)

    assert result == "Recovered from error"


@pytest.mark.asyncio
async def test_large_tool_results_not_truncated():
    """Large tool results are passed through in full — no hard truncation."""
    huge_result = "x" * 100_000
    tc = _make_tool_call("tc1", "read_record", '{"record_id": "big"}')

    client = AsyncMock()
    client.chat.completions.create.side_effect = [
        _make_response(content="", finish_reason="tool_calls", tool_calls=[tc]),
        _make_response(content="Got the full document"),
    ]

    tool_callback = AsyncMock(return_value=huge_result)
    tools = [_dummy_tool("read_record")]

    backend = OpenRouterBackend(client, "test-model", max_turns=5)
    result = await backend.run("read it", "system", tools, tool_callback)

    assert result == "Got the full document"
    second_call_messages = client.chat.completions.create.call_args_list[1][1]["messages"]
    tool_msg = [m for m in second_call_messages if isinstance(m, dict) and m.get("role") == "tool"][0]
    assert len(tool_msg["content"]) == 100_000


@pytest.mark.asyncio
async def test_context_compaction_triggers():
    """When messages exceed COMPACTION_CHAR_THRESHOLD, old turns are compacted."""
    backend = OpenRouterBackend(AsyncMock(), "test-model", max_turns=10)

    messages = [{"role": "user", "content": "original question"}]
    big_content = "y" * (COMPACTION_CHAR_THRESHOLD // 3)

    for i in range(6):
        assistant = MagicMock()
        assistant.role = "assistant"
        assistant.content = f"step {i}"
        assistant.tool_calls = [_make_tool_call(f"tc{i}", "read_record", f'{{"record_id": "r{i}"}}')]
        messages.append(assistant)
        messages.append({
            "role": "tool",
            "tool_call_id": f"tc{i}",
            "content": big_content,
        })

    assert backend._estimate_message_chars(messages) > COMPACTION_CHAR_THRESHOLD

    compacted = await backend._compact_context(messages)

    assert len(compacted) < len(messages)
    assert any(
        isinstance(m, dict) and "compacted" in m.get("content", "").lower()
        for m in compacted
    )
    assert compacted[0]["role"] == "user"
    assert compacted[0]["content"] == "original question"
    assert backend._estimate_message_chars(compacted) < backend._estimate_message_chars(messages)


@pytest.mark.asyncio
async def test_compaction_preserves_recent_turns():
    """Compaction keeps the most recent turns intact."""
    backend = OpenRouterBackend(AsyncMock(), "test-model", max_turns=10)

    messages = [{"role": "user", "content": "original question"}]
    for i in range(6):
        assistant = MagicMock()
        assistant.role = "assistant"
        assistant.content = f"thinking step {i}"
        assistant.tool_calls = None
        messages.append(assistant)
        messages.append({"role": "tool", "tool_call_id": f"tc{i}", "content": f"big result {i} " + "x" * 1000})

    compacted = await backend._compact_context(messages)

    assert len(compacted) < len(messages)
    assert compacted[0]["role"] == "user"
    assert compacted[0]["content"] == "original question"
    assert any(
        isinstance(m, dict) and "compacted" in m.get("content", "").lower()
        for m in compacted
    )


@pytest.mark.asyncio
async def test_compaction_skipped_when_few_turns():
    """Compaction is a no-op when there aren't enough turns to compact."""
    backend = OpenRouterBackend(AsyncMock(), "test-model", max_turns=10)

    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "let me search"},
        {"role": "tool", "tool_call_id": "tc1", "content": "result"},
    ]

    compacted = await backend._compact_context(messages)
    assert compacted == messages


@pytest.mark.asyncio
async def test_system_prompt_prepended_each_turn():
    """System prompt is included in every LLM call."""
    tc = _make_tool_call("tc1", "search_index", '{"query": "test"}')

    client = AsyncMock()
    client.chat.completions.create.side_effect = [
        _make_response(content="", finish_reason="tool_calls", tool_calls=[tc]),
        _make_response(content="done"),
    ]

    tool_callback = AsyncMock(return_value="result")
    tools = [_dummy_tool("search_index")]

    backend = OpenRouterBackend(client, "test-model", max_turns=5)
    await backend.run("question", "Be helpful", tools, tool_callback)

    for call in client.chat.completions.create.call_args_list:
        messages = call[1]["messages"]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "Be helpful"

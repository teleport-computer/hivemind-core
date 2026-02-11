import asyncio
import json
import logging
from typing import Callable

from openai import AsyncOpenAI, APIError, BadRequestError, RateLimitError

from ..tools import Tool

logger = logging.getLogger(__name__)

# Retry config
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # seconds

# Context compaction: when total message chars exceed this, summarize old tool results.
# This is a soft limit on the *text* in messages, not tokens — conservative estimate
# at ~4 chars/token means this fires around ~50k tokens of context.
COMPACTION_CHAR_THRESHOLD = 200_000

# After compaction, tool results older than the last N turns are summarized
COMPACTION_KEEP_RECENT_TURNS = 4


class OpenRouterBackend:
    def __init__(self, client: AsyncOpenAI, model: str, max_turns: int):
        self.client = client
        self.model = model
        self.max_turns = max_turns

    async def _call_llm(self, **kwargs) -> object:
        """LLM call with retry on transient errors.

        Only retries on RateLimitError and server errors (5xx).
        BadRequestError and other client errors propagate immediately.
        """
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                return await self.client.chat.completions.create(**kwargs)
            except BadRequestError:
                raise  # let caller handle (e.g. context overflow recovery)
            except RateLimitError as e:
                last_err = e
                wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                logger.warning("Rate limited (attempt %d/%d), retrying in %ds", attempt + 1, MAX_RETRIES, wait)
                await asyncio.sleep(wait)
            except APIError as e:
                if e.status_code and e.status_code >= 500:
                    last_err = e
                    wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                    logger.warning("Server error %s (attempt %d/%d), retrying in %ds", e.status_code, attempt + 1, MAX_RETRIES, wait)
                    await asyncio.sleep(wait)
                else:
                    raise
        raise last_err  # type: ignore[misc]

    def _estimate_message_chars(self, messages: list) -> int:
        """Rough char count across all messages for compaction decisions."""
        total = 0
        for m in messages:
            if isinstance(m, dict):
                content = m.get("content", "")
                total += len(content) if isinstance(content, str) else 0
            else:
                # OpenAI message object
                total += len(m.content or "") if hasattr(m, "content") else 0
        return total

    async def _compact_context(self, messages: list) -> list:
        """Summarize older tool results to free context space.

        Keeps the first user message and the last COMPACTION_KEEP_RECENT_TURNS
        worth of messages intact. Older assistant+tool exchanges are replaced
        with a single summary message.
        """
        # Find turn boundaries (each assistant message starts a turn)
        turn_starts = []
        for i, m in enumerate(messages):
            is_assistant = (
                (isinstance(m, dict) and m.get("role") == "assistant")
                or (hasattr(m, "role") and m.role == "assistant")
            )
            if is_assistant:
                turn_starts.append(i)

        if len(turn_starts) <= COMPACTION_KEEP_RECENT_TURNS:
            return messages  # not enough turns to compact

        # Split: keep first user msg + compact old turns + keep recent turns
        cutoff = turn_starts[-COMPACTION_KEEP_RECENT_TURNS]
        old_section = messages[1:cutoff]  # skip index 0 (user prompt)
        recent_section = messages[cutoff:]

        # Build a summary of old tool exchanges
        tool_summaries = []
        for m in old_section:
            if isinstance(m, dict) and m.get("role") == "tool":
                content = m.get("content", "")
                # Keep first 200 chars as a hint
                preview = content[:200] + "..." if len(content) > 200 else content
                tool_summaries.append(f"- Tool result: {preview}")
            elif hasattr(m, "tool_calls") and m.tool_calls:
                for tc in m.tool_calls:
                    tool_summaries.append(
                        f"- Called {tc.function.name}({tc.function.arguments[:100]})"
                    )

        if not tool_summaries:
            return messages

        summary_text = (
            "[Earlier tool interactions were compacted to save context space]\n"
            + "\n".join(tool_summaries[:20])  # cap summary items
        )

        compacted = [
            messages[0],  # original user prompt
            {"role": "assistant", "content": summary_text},
            {"role": "user", "content": "(continuing from compacted context)"},
        ] + recent_section

        logger.info(
            "Compacted context: %d messages -> %d messages",
            len(messages), len(compacted),
        )
        return compacted

    async def _execute_tool_call(self, tc, on_tool_call: Callable) -> dict:
        """Execute a single tool call, returning the tool result message."""
        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Malformed tool args for %s: %s", name, e)
            return {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": f"Error: malformed arguments — {e}",
            }

        try:
            result = await on_tool_call(name, args)
        except KeyError:
            logger.warning("Unknown tool called: %s", name)
            result = f"Error: unknown tool '{name}'"
        except Exception as e:
            logger.warning("Tool %s raised: %s", name, e)
            result = f"Error executing {name}: {e}"

        return {
            "role": "tool",
            "tool_call_id": tc.id,
            "content": result,
        }

    async def run(
        self,
        prompt: str,
        system: str,
        tools: list[Tool],
        on_tool_call: Callable,
    ) -> str:
        messages: list[dict] = [{"role": "user", "content": prompt}]
        tool_defs = [t.to_openai_def() for t in tools]

        last_content = ""
        for turn in range(self.max_turns):
            # Compact context if it's getting large
            if self._estimate_message_chars(messages) > COMPACTION_CHAR_THRESHOLD:
                messages = await self._compact_context(messages)

            kwargs: dict = {
                "model": self.model,
                "messages": [{"role": "system", "content": system}] + messages,
                "max_tokens": 4096,
            }
            if tool_defs:
                kwargs["tools"] = tool_defs

            try:
                resp = await self._call_llm(**kwargs)
            except BadRequestError as e:
                # Context length exceeded — force-compact and retry once
                err_body = str(e).lower()
                if ("context" in err_body and ("length" in err_body or "token" in err_body)) or "too many tokens" in err_body:
                    logger.warning("Context length exceeded, force-compacting and retrying")
                    messages = await self._compact_context(messages)
                    kwargs["messages"] = [{"role": "system", "content": system}] + messages
                    resp = await self._call_llm(**kwargs)
                else:
                    raise

            choice = resp.choices[0]
            last_content = choice.message.content or ""

            if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
                messages.append(choice.message)

                # Execute tool calls in parallel
                tool_results = await asyncio.gather(
                    *(self._execute_tool_call(tc, on_tool_call)
                      for tc in choice.message.tool_calls)
                )
                messages.extend(tool_results)
            else:
                return last_content

        return last_content

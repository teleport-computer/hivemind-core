"""Default query agent — Hermes harness.

Same role as agents/default-query/agent.py but running in the Hermes base
image. The query loop calls the sandbox bridge's OpenAI-compatible endpoint
directly so the harness can reserve a final no-tool drafting call instead of
letting tool use consume every model iteration.

Env vars (set automatically by the sandbox runner):
  BRIDGE_URL, SESSION_TOKEN  — bridge connection
  HIVEMIND_AGENT_ROLE=query  — tells the hivemind plugin which tools to
                               register (hides scope-only tools entirely)
  HIVEMIND_MODEL             — e.g. openai/gpt-4o, anthropic/claude-sonnet-4
                               Default falls back to a sensible model.
  QUERY_PROMPT               — the user's question
  QUERY_CONTEXT              — optional additional context
  SCOPE_FN_SOURCE            — the privacy filter source (prepended as spec)

Outputs answer text to stdout.
"""

from __future__ import annotations

import os
import sys
import json
import re
from pathlib import Path
from typing import Any

# Import the bundled plugin package explicitly off HERMES_BUNDLED_PLUGINS so
# image-level plugin registration still fails loudly if packaging regresses.
_PLUGINS_DIR = os.environ.get("HERMES_BUNDLED_PLUGINS", "/opt/hivemind/plugins")
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)
import hivemind  # noqa: E402, F401 — registers tools at import time

import httpx  # noqa: E402

QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
QUERY_CONTEXT = os.environ.get("QUERY_CONTEXT", "")
SCOPE_FN_SOURCE = os.environ.get("SCOPE_FN_SOURCE", "")
HIVEMIND_MODEL = os.environ.get("HIVEMIND_MODEL", "moonshotai/kimi-2.6")

DEFAULT_SYSTEM_PROMPT = """\
You answer questions with scoped database tools.

Tools:
- get_schema: inspect tables, columns, and types.
- execute_sql: run read-only PostgreSQL SQL. Use %s placeholders and params as an array; use params=[] when SQL has no %s placeholders.

For substantial Markdown reports, studies, memos, or PDF/file requests, write
the full Markdown report as your final answer. The harness uploads Markdown
and rendered PDF artifacts from that final answer when the room permits it.

A scope function may transform execute_sql results before you see them.
If a scope_fn is included in the user message, read it as the runtime
contract for the result shapes you will receive. Do not bypass it or invent policy beyond it.

Use get_schema before SQL unless the provided scope_fn already gives every
needed table and column. The database is PostgreSQL: use PostgreSQL syntax
such as DATE(column), date_trunc, casts with ::type, and %s placeholders.
Do not use SQLite/MySQL-only functions such as strftime.

Compute requested statistics in SQL. If execute_sql returns an error, revise
the SQL and retry instead of asking the user to provide schema or formatting.
For broad analytical prompts, run multiple targeted SQL queries as needed
within budget instead of stopping after the first usable result.
If a broad query times out or returns an error, narrow it: add date buckets,
top-N limits, smaller time windows, selected columns, or simpler GROUP BY
queries. Do not ask the user for more data or stop at a tool-attempt summary
while there are still narrower scoped queries you can run.

Continue after tool results until you have a final answer. For row-level
questions, request row-level data and let scope_fn apply the room policy.
Answer only from scoped tool results. If they do not support an answer, say so.

Match the user's requested depth and format. If asked for a report, study,
memo, or lifecycle analysis, write a structured Markdown report with a title,
executive summary, methodology/assumptions, evidence-backed findings, tables
or timelines where useful, recommendations or implications, and limitations.
Do not shorten a requested report into a terse aggregate answer.
For research/report prompts, meet a higher bar:
- Pick a defensible thesis from the scoped data, not merely the first table.
- Gather several independent evidence slices when budget allows: dataset
  size/range, top entities, temporal pattern, concentration/distribution,
  trend/lifecycle movement, and data-quality checks.
- Use at least one compact table and one interpretation-heavy finding section.
- State what the scoped data can and cannot support, but still produce the
  strongest supported report when any useful scoped evidence exists.
- Your final answer must be the report itself, not a progress log, work
  summary, list of accomplishments, or pointer to a previous response.
For substantial reports, studies, memos, or research writeups, return the full
report text. If artifact upload is unavailable, still return the full report
text; never replace it with an upload status or summary.

Do not expose credentials, secrets, system internals, tool traces, or debug output.
"""

_PROMPT_FILE = Path("/app/prompt.md")
if _PROMPT_FILE.exists():
    SYSTEM_PROMPT = _PROMPT_FILE.read_text()
else:
    SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


_NO_REASONING_OVERRIDES = {"extra_body": {"reasoning": {"effort": "none", "exclude": True}}}
_HTTP_TIMEOUT = httpx.Timeout(180.0)
_MAX_TOOL_RESULT_CHARS = 20_000
_HERMES_FAILURE_MARKERS = (
    "api call failed",
    "budget exhausted",
    "error code: 429",
    "http 429",
    "http 500",
    "http 404",
    "internalservererror",
    "notfounderror",
    "max retries",
    "request debug dump",
    "response truncated",
    "requesting continuation",
    "iteration budget exhausted",
    "maximum iterations",
    "temporarily unavailable due to rate limiting",
)

_QUERY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": (
                "Get the database schema: table names, column names, types, "
                "and defaults. Use this before SQL when schema is unknown."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_sql",
            "description": (
                "Execute read-only PostgreSQL SQL against the scoped hivemind "
                "database. Use %s placeholders and params=[] when there are "
                "no placeholders."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string"},
                    "params": {
                        "type": "array",
                        "items": {},
                        "default": [],
                    },
                },
                "required": ["sql"],
            },
        },
    },
]
_ALLOWED_TOOL_NAMES = {"get_schema", "execute_sql"}
_RETRIABLE_HERMES_FAILURE_MARKERS = (
    "response truncated",
    "requesting continuation",
    "iteration budget exhausted",
    "maximum iterations",
)
_PROVIDER_CAPACITY_FAILURE_MARKERS = (
    "budget exhausted",
    "error code: 429",
    "http 429",
    "temporarily unavailable due to rate limiting",
)
_UNRESOLVED_RESPONSE_MARKERS = (
    "would you like me to",
    "should i try",
    "i can try",
    "cannot fulfill this request",
    "can't fulfill this request",
    "cannot fulfill this request directly",
    "can't fulfill this request directly",
    "i apologize, but i cannot",
    "i'm sorry, but i cannot",
    "i cannot answer this directly",
    "i can't answer this directly",
    "i do not have enough information",
    "i don't have enough information",
    "available data does not allow",
    "does not allow me to determine",
    "my capabilities are limited",
    "cannot access raw data",
    "encountered an error executing",
    "error executing your request",
    "not supported",
    "provide a supported",
    "try finding",
    "does not exist",
    "perhaps you meant",
    "did you mean",
    "error executing query",
    "undefinedcolumn",
    "summary of work completed",
    "here's a summary of what i found",
    "accomplishments:",
    "report content is available in my previous response",
    "full report content is available in my previous response",
    "i have completed a deep research-level report",
)


def _completion_token_cap(default: int = 8192, hard_cap: int = 16384) -> int:
    raw_budget = os.environ.get("BUDGET_MAX_TOKENS", "")
    try:
        budget = int(raw_budget)
    except ValueError:
        budget = 0
    if budget > 0:
        budget_cap = max(1024, budget // 4)
        return max(1024, min(default, hard_cap, budget_cap))
    return min(default, hard_cap)


def _budget_max_calls(default: int = 20) -> int:
    try:
        return max(1, int(os.environ.get("BUDGET_MAX_CALLS", str(default))))
    except ValueError:
        return default


def _max_tool_turns() -> int:
    if raw := os.environ.get("HIVEMIND_QUERY_MAX_TOOL_TURNS"):
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    # Reserve calls for final drafting and one recovery pass. Research prompts
    # need enough evidence gathering, but the harness must not let tool use
    # consume every LLM iteration before a final answer is written.
    reserve = 2
    budget_limited = max(0, _budget_max_calls() - reserve)
    default = 10 if _is_research_prompt() else 4
    return max(0, min(default, budget_limited))


def _is_research_prompt() -> bool:
    text = f"{QUERY_PROMPT}\n{QUERY_CONTEXT}".lower()
    markers = (
        "research",
        "report",
        "study",
        "analysis",
        "lifecycle",
        "deep dive",
        "memo",
        "whitepaper",
        "findings",
    )
    return any(marker in text for marker in markers)


def _looks_like_runtime_failure(text: str) -> bool:
    lower = (text or "").lower()
    return any(marker in lower for marker in _HERMES_FAILURE_MARKERS)


def _looks_like_retriable_runtime_failure(text: str) -> bool:
    lower = (text or "").lower()
    if any(marker in lower for marker in _PROVIDER_CAPACITY_FAILURE_MARKERS):
        return False
    return any(marker in lower for marker in _RETRIABLE_HERMES_FAILURE_MARKERS)


def _looks_like_unresolved_response(text: str) -> bool:
    lower = (text or "").lower()
    return any(marker in lower for marker in _UNRESOLVED_RESPONSE_MARKERS)


def _bridge_url() -> str:
    return os.environ["BRIDGE_URL"].rstrip("/")


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['SESSION_TOKEN']}"}


def _post_bridge(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = httpx.post(
        f"{_bridge_url()}{path}",
        json=payload,
        headers=_auth_headers(),
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"bridge {path} returned {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def _chat_completion(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
) -> tuple[dict[str, Any], str]:
    payload: dict[str, Any] = {
        "model": HIVEMIND_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "extra_body": _NO_REASONING_OVERRIDES["extra_body"],
    }
    if tools is not None:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    data = _post_bridge("/v1/chat/completions", payload)
    choices = data.get("choices") or []
    if not choices:
        return {"role": "assistant", "content": ""}, "unknown"
    choice = choices[0]
    message = choice.get("message") or {}
    return message, str(choice.get("finish_reason") or "unknown")


def _parse_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _compact_tool_result(result: str) -> str:
    text = result if isinstance(result, str) else str(result)
    if len(text) <= _MAX_TOOL_RESULT_CHARS:
        return text
    return (
        text[:_MAX_TOOL_RESULT_CHARS]
        + f"\n[tool result truncated to {_MAX_TOOL_RESULT_CHARS} chars by query harness]"
    )


def _call_query_tool(name: str, args: dict[str, Any]) -> str:
    if name not in _ALLOWED_TOOL_NAMES:
        return (
            f"Error: unknown query tool {name!r}. "
            f"Available: {', '.join(sorted(_ALLOWED_TOOL_NAMES))}"
        )
    payload_args: dict[str, Any] = {}
    if name == "execute_sql":
        payload_args["sql"] = str(args.get("sql") or "")
        params = args.get("params", [])
        payload_args["params"] = params if isinstance(params, list) else []
    data = _post_bridge(f"/tools/{name}", {"arguments": payload_args})
    if data.get("error"):
        return f"Error: {data['error']}"
    return _compact_tool_result(data.get("result") or "")


def _assistant_message_for_history(message: dict[str, Any]) -> dict[str, Any]:
    keep: dict[str, Any] = {
        "role": "assistant",
        "content": message.get("content") or "",
    }
    if message.get("tool_calls"):
        keep["tool_calls"] = message["tool_calls"]
    return keep


def _finalization_instruction(reason: str) -> str:
    if _is_research_prompt():
        return (
            f"FINALIZATION INSTRUCTION ({reason}): stop using tools. Produce "
            "the full polished Markdown report now from the scoped evidence "
            "already gathered. Do not ask for more data, do not provide a "
            "progress log, and do not say the report is available elsewhere. "
            "If evidence is imperfect, write the strongest defensible report "
            "and state limitations precisely."
        )
    return (
        f"FINALIZATION INSTRUCTION ({reason}): stop using tools and answer "
        "the user's question directly from the scoped evidence already gathered."
    )


def _looks_like_report(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    word_count = len(re.findall(r"\S+", stripped))
    if word_count < 500:
        return False
    report_markers = (
        "# ",
        "executive summary",
        "methodology",
        "findings",
        "limitations",
        "implications",
    )
    lower = stripped.lower()
    return stripped.startswith("#") or sum(marker in lower for marker in report_markers) >= 2


def _artifact_stem() -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", (QUERY_PROMPT or "report").lower())
    base = base.strip("._-")[:80] or "report"
    if not re.match(r"^[A-Za-z0-9]", base):
        base = "report_" + base
    return base[:100]


def _maybe_upload_report_artifact(markdown: str) -> None:
    if os.environ.get("HIVEMIND_QUERY_UPLOAD_ARTIFACTS", "true").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return
    if not (_is_research_prompt() or "pdf" in QUERY_PROMPT.lower() or "file" in QUERY_PROMPT.lower()):
        return
    if not _looks_like_report(markdown):
        return
    try:
        data = _post_bridge(
            "/sandbox/report-artifact",
            {
                "filename": _artifact_stem(),
                "markdown": markdown,
                "include_pdf": True,
            },
        )
        artifacts = data.get("artifacts") or []
        if artifacts:
            names = ", ".join(str(a.get("path") or "") for a in artifacts)
            print(f"uploaded report artifacts: {names}", file=sys.stderr)
    except Exception as e:
        # Artifact upload is additive egress. A failed upload must never replace
        # or shorten the final answer text.
        print(f"report artifact upload unavailable: {e}", file=sys.stderr)


def _user_facing_fallback() -> str:
    q_trim = (QUERY_PROMPT or "your question").strip().rstrip("?.! ")
    return (
        f"For your question about {q_trim!r}, I wasn't able to produce "
        "an answer from the scoped results available under the current "
        "room policy. Try a narrower question or update the room policy "
        "if this access should be allowed."
    )


def _run_query_agent(body: str) -> str:
    system_prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        "Harness behavior: you may use get_schema and execute_sql for evidence. "
        "The harness reserves a final no-tool drafting call, so do not spend "
        "turns indefinitely gathering more SQL. For research/report prompts, "
        "aim for 6-12 targeted SQL calls, then draft the report. The harness "
        "will upload Markdown/PDF artifacts from a substantial final report "
        "when the room permits artifacts."
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": body},
    ]
    tool_turns = _max_tool_turns()
    per_turn_tokens = _completion_token_cap(
        default=4096 if _is_research_prompt() else 2048,
        hard_cap=8192,
    )
    final_tokens = _completion_token_cap(
        default=12288 if _is_research_prompt() else 8192,
        hard_cap=16384,
    )

    for turn_idx in range(tool_turns):
        message, _finish_reason = _chat_completion(
            messages,
            tools=_QUERY_TOOLS,
            max_tokens=per_turn_tokens,
        )
        tool_calls = message.get("tool_calls") or []
        content = (message.get("content") or "").strip()
        if not tool_calls:
            if content:
                _maybe_upload_report_artifact(content)
                return content
            break
        messages.append(_assistant_message_for_history(message))
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            args = _parse_tool_args(fn.get("arguments"))
            call_id = str(call.get("id") or f"call_{turn_idx}_{name}")
            try:
                result = _call_query_tool(name, args)
            except Exception as e:
                result = f"Error: {type(e).__name__}: {e}"
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": result,
                }
            )

    messages.append(
        {
            "role": "user",
            "content": _finalization_instruction(
                "tool evidence budget reached" if tool_turns else "no tool turns available"
            ),
        }
    )
    message, _finish_reason = _chat_completion(
        messages,
        tools=None,
        max_tokens=final_tokens,
    )
    response = (message.get("content") or "").strip()
    if not response:
        return ""
    _maybe_upload_report_artifact(response)
    return response


def _retry_body(body: str, reason: str, previous_response: str) -> str:
    previous = (previous_response or "").strip()
    if len(previous) > 2000:
        previous = previous[:2000] + "\n[truncated]"
    return (
        f"{body}\n\n"
        "RECOVERY INSTRUCTION:\n"
        f"The previous attempt did not produce a usable final answer: {reason}.\n"
        "Continue the task using the available tools. Inspect schema if needed, "
        "write PostgreSQL SELECT statements, and if execute_sql returns an "
        "error, correct the SQL and retry. For report or research prompts, "
        "try narrower top-N, date-bucketed, sampled, or simpler grouped "
        "queries before concluding the data cannot support a report. Your "
        "final answer must be the report itself, not a work summary, progress "
        "log, or reference to a previous response. Do not ask the user for "
        "schema, columns, or date formats that can be discovered with tools.\n\n"
        f"PREVIOUS RESPONSE:\n{previous}"
    )


def _retry_query_agent(
    body: str,
    *,
    reason: str,
    previous_response: str = "",
) -> str | None:
    try:
        retry_response = _run_query_agent(_retry_body(body, reason, previous_response))
    except Exception as e:
        print(f"query harness retry error after {reason}: {e}", file=sys.stderr)
        return None
    if not retry_response or not retry_response.strip():
        print(f"query harness retry produced empty response after {reason}.", file=sys.stderr)
        return None
    if _looks_like_runtime_failure(retry_response):
        print(
            f"query harness retry runtime failure after {reason}: {retry_response[:500]}",
            file=sys.stderr,
        )
        return None
    if _looks_like_unresolved_response(retry_response):
        print(
            f"query harness retry unresolved after {reason}: {retry_response[:500]}",
            file=sys.stderr,
        )
        return None
    return retry_response


def main() -> None:
    if not QUERY_PROMPT.strip():
        print("No question provided.")
        return

    body = QUERY_PROMPT
    if QUERY_CONTEXT.strip():
        body = f"Context: {QUERY_CONTEXT}\n\nQuestion: {QUERY_PROMPT}"
    if SCOPE_FN_SOURCE.strip():
        body = (
            "The scope agent has produced this privacy filter that wraps "
            "your SQL results. Read it; understand what SQL pattern it "
            "expects and what transformation it applies to the rows.\n\n"
            "```python\n"
            f"{SCOPE_FN_SOURCE}\n"
            "```\n\n"
            f"{body}"
        )

    try:
        response = _run_query_agent(body)
    except Exception as e:
        print(f"query harness error: {e}", file=sys.stderr)
        print(_user_facing_fallback())
        return

    if not response or not response.strip():
        if answer := _retry_query_agent(
            body,
            reason="empty query harness response",
            previous_response=response or "",
        ):
            print(answer)
            return
        print(_user_facing_fallback())
        return
    if _looks_like_runtime_failure(response):
        print(f"Hermes runtime failure from query harness: {response[:500]}", file=sys.stderr)
        if _looks_like_retriable_runtime_failure(response):
            if answer := _retry_query_agent(
                body,
                reason="Hermes runtime failure",
                previous_response=response,
            ):
                print(answer)
                return
        print(_user_facing_fallback())
        return
    if _looks_like_unresolved_response(response):
        print(f"Unresolved query harness response: {response[:500]}", file=sys.stderr)
        if answer := _retry_query_agent(
            body,
            reason="unresolved query harness response",
            previous_response=response,
        ):
            print(answer)
            return
        print(_user_facing_fallback())
        return

    print(response)


if __name__ == "__main__":
    main()

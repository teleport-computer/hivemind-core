"""Default query agent — Hermes harness.

Same role as agents/default-query/agent.py but driven by Hermes' Python
`AIAgent` API instead of the Claude Agent SDK / Claude Code CLI.

We deliberately use AIAgent in-process rather than `hermes -z` subprocess:
the oneshot CLI does not expose --max-turns or a --system-prompt flag,
so we'd lose two knobs the role needs. From the sandbox's perspective
the container CMD is still a single Python process.

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
from contextlib import redirect_stdout
from pathlib import Path

# Trigger plugin registration BEFORE importing AIAgent. Hermes' bundled
# plugin discovery runs lazily and doesn't always fire in library mode,
# so we import the plugin package explicitly off HERMES_BUNDLED_PLUGINS.
_PLUGINS_DIR = os.environ.get("HERMES_BUNDLED_PLUGINS", "/opt/hivemind/plugins")
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)
import hivemind  # noqa: E402, F401 — registers tools at import time

from run_agent import AIAgent  # noqa: E402

QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
QUERY_CONTEXT = os.environ.get("QUERY_CONTEXT", "")
SCOPE_FN_SOURCE = os.environ.get("SCOPE_FN_SOURCE", "")
HIVEMIND_MODEL = os.environ.get("HIVEMIND_MODEL", "moonshotai/kimi-2.6")

DEFAULT_SYSTEM_PROMPT = """\
You answer questions with scoped database tools.

Tools:
- get_schema: inspect tables, columns, and types.
- execute_sql: run read-only PostgreSQL SQL. Use %s placeholders and params as an array; use params=[] when SQL has no %s placeholders.
- upload_artifact: upload generated report artifacts when the room permits it.
  Use text/markdown for Markdown reports, application/json for JSON, text/csv
  for tables, text/html for HTML, or application/pdf only when you have real
  PDF bytes encoded as base64.

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
If the user asks for a file or artifact, call upload_artifact with the report
content before the final answer. If artifact upload is unavailable, still
return the full report text. A failed artifact upload must not replace,
shorten, or summarize the final report; at most add one short note after the
report that no artifact was created.

Do not expose credentials, secrets, system internals, tool traces, or debug output.
"""

_PROMPT_FILE = Path("/app/prompt.md")
if _PROMPT_FILE.exists():
    SYSTEM_PROMPT = _PROMPT_FILE.read_text()
else:
    SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


_NO_REASONING_CONFIG = {"enabled": False, "effort": "none"}
_NO_REASONING_OVERRIDES = {"extra_body": {"reasoning": {"effort": "none", "exclude": True}}}
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


def _user_facing_fallback() -> str:
    q_trim = (QUERY_PROMPT or "your question").strip().rstrip("?.! ")
    return (
        f"For your question about {q_trim!r}, I wasn't able to produce "
        "an answer from the scoped results available under the current "
        "room policy. Try a narrower question or update the room policy "
        "if this access should be allowed."
    )


def _run_ai_agent(body: str) -> str:
    base_url = os.environ["BRIDGE_URL"].rstrip("/") + "/v1"
    api_key = os.environ["SESSION_TOKEN"]

    agent = AIAgent(
        base_url=base_url,
        api_key=api_key,
        provider="custom",
        model=HIVEMIND_MODEL,
        max_iterations=16 if _is_research_prompt() else 6,
        enabled_toolsets=["hivemind"],
        ephemeral_system_prompt=SYSTEM_PROMPT,
        skip_context_files=True,
        skip_memory=True,
        quiet_mode=True,
        save_trajectories=False,
        max_tokens=_completion_token_cap(default=12288 if _is_research_prompt() else 8192),
        reasoning_config=_NO_REASONING_CONFIG,
        request_overrides=_NO_REASONING_OVERRIDES,
    )
    with redirect_stdout(sys.stderr):
        return agent.chat(body) or ""


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


def _retry_ai_agent(
    body: str,
    *,
    reason: str,
    previous_response: str = "",
) -> str | None:
    try:
        retry_response = _run_ai_agent(_retry_body(body, reason, previous_response))
    except Exception as e:
        print(f"AIAgent retry error after {reason}: {e}", file=sys.stderr)
        return None
    if not retry_response or not retry_response.strip():
        print(f"AIAgent retry produced empty response after {reason}.", file=sys.stderr)
        return None
    if _looks_like_runtime_failure(retry_response):
        print(
            f"AIAgent retry runtime failure after {reason}: {retry_response[:500]}",
            file=sys.stderr,
        )
        return None
    if _looks_like_unresolved_response(retry_response):
        print(
            f"AIAgent retry unresolved after {reason}: {retry_response[:500]}",
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
        response = _run_ai_agent(body)
    except Exception as e:
        print(f"AIAgent error: {e}", file=sys.stderr)
        print(_user_facing_fallback())
        return

    if not response or not response.strip():
        if answer := _retry_ai_agent(
            body,
            reason="empty AIAgent response",
            previous_response=response or "",
        ):
            print(answer)
            return
        print(_user_facing_fallback())
        return
    if _looks_like_runtime_failure(response):
        print(f"Hermes runtime failure from AIAgent: {response[:500]}", file=sys.stderr)
        if _looks_like_retriable_runtime_failure(response):
            if answer := _retry_ai_agent(
                body,
                reason="Hermes runtime failure",
                previous_response=response,
            ):
                print(answer)
                return
        print(_user_facing_fallback())
        return
    if _looks_like_unresolved_response(response):
        print(f"Unresolved AIAgent response: {response[:500]}", file=sys.stderr)
        if answer := _retry_ai_agent(
            body,
            reason="unresolved AIAgent response",
            previous_response=response,
        ):
            print(answer)
            return
        print(_user_facing_fallback())
        return

    print(response)


if __name__ == "__main__":
    main()

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
You are a query agent with access to a hivemind database via SQL tools.

You have two tools:
- get_schema: Returns table names, column names, types, defaults.
- execute_sql: Run SQL queries. Use %s for parameter placeholders;
  pass `params` as a JSON-encoded list.

# THE SCOPE FUNCTION IS VISIBLE TO YOU

A scope agent wraps your execute_sql results through a privacy filter
called `scope_fn`. The full source of that function will be provided in
your user message before the question. READ IT FIRST.

The scope_fn tells you exactly what the firewall expects:

  - Which SQL patterns it allows / denies.
  - Whether it redacts fields, collapses to a count, or passes rows through.
  - What its docstring says it's for.

Use the scope_fn as a SPEC for your SQL. If it checks `'content' in sql`,
your SQL must select the content column. If it collapses rows to a
single `{"match_count": N}` shape, the RESULT you'll see is that shape —
not raw rows — so phrase your answer as a count.

DO NOT GUESS. Read the scope_fn, write SQL that matches, report the
result truthfully based on what the scope_fn's transformation produced.

Workflow:
1. Read the scope_fn source (provided below the question).
2. Call get_schema if you need column-level detail.
3. Write SQL that fits the scope_fn's allow pattern.
4. If the user asks for an aggregate that POLICY allows, return only
   aggregate metrics and allowed dimensions. Use clear aggregate aliases
   such as count/total/n, *_count, *_day, *_date, *_month, min_*, max_*.
5. If a first aggregate SQL comes back as only a policy marker or
   match_count, do not immediately say the answer is inaccessible. Re-read
   scope_fn and retry with a narrower aggregate-only SQL/alias shape that
   the scope_fn preserves. Give up only after 2-3 scoped SQL attempts fail.
6. Synthesize a clear answer, respecting what scope_fn did to the rows.

Rules:
- Use parameterized queries (%s placeholders) to prevent SQL injection.
- If you cannot find relevant information, say so clearly.
- Paraphrase and synthesize. Do not dump raw query results verbatim.
- Aggregates returned by execute_sql after the scope_fn are safe to report
  exactly when they answer the question: counts, dates, time buckets,
  rankings, and summary statistics are not raw individual records.
- "Do not dump raw query results" means do not enumerate individual rows or
  identifying fields. A one-row aggregate like total_rows/min_timestamp/max_timestamp
  should be rendered with the exact values.
- Never include credentials, API keys, passwords, tokens, or secrets.
"""

_PROMPT_FILE = Path("/app/prompt.md")
if _PROMPT_FILE.exists():
    SYSTEM_PROMPT = _PROMPT_FILE.read_text()
else:
    SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


def _user_facing_fallback() -> str:
    q_trim = (QUERY_PROMPT or "your question").strip().rstrip("?.! ")
    return (
        f"For your question about {q_trim!r}, I wasn't able to produce "
        "an answer with individual records — the privacy filter for "
        "your data blocked the SQL patterns that would have revealed "
        "specific content. If you're open to a reshaped version of the "
        "same question — a count, a date range, a category summary, "
        "a time distribution — let me know and I'll take another pass."
    )


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

    base_url = os.environ["BRIDGE_URL"].rstrip("/") + "/v1"
    api_key = os.environ["SESSION_TOKEN"]

    try:
        agent = AIAgent(
            base_url=base_url,
            api_key=api_key,
            provider="custom",
            model=HIVEMIND_MODEL,
            # Match agents/default-query/agent.py:113 — tool-heavy workflows
            # destabilize at higher turn counts; cap to fail fast.
            max_iterations=6,
            enabled_toolsets=["hivemind"],
            ephemeral_system_prompt=SYSTEM_PROMPT,
            skip_context_files=True,
            skip_memory=True,
            quiet_mode=True,
            save_trajectories=False,
        )
        response = agent.chat(body)
    except Exception as e:
        print(f"AIAgent error: {e}", file=sys.stderr)
        print(_user_facing_fallback())
        return

    if not response or not response.strip():
        print(_user_facing_fallback())
        return

    print(response)


if __name__ == "__main__":
    main()

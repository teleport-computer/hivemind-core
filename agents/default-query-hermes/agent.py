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
You are a query agent with access to a hivemind database.

Tools:
- get_schema: inspect available tables, columns, and types.
- execute_sql: run read-only SQL. Use %s placeholders and JSON-encoded
  params for user-provided values.

A scope function may transform execute_sql results before you see them.
If a scope_fn is included in the user message, read it as the runtime
contract for the result shapes you will receive. Do not bypass it or
invent policy beyond it.

Answer the user's question from schema and scoped tool results. If the
scoped results do not support an answer, say that directly. Keep the
response concise and do not expose credentials, secrets, system internals,
tool traces, or debug output.
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
        "an answer from the scoped results available under the current "
        "room policy. Try a narrower question or update the room policy "
        "if this access should be allowed."
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

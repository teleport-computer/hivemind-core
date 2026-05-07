"""Default mediator agent — Hermes harness.

Single-shot privacy rewriter. tools=[], max_iterations=1. Same role as
agents/default-mediator/agent.py: take the query agent's raw output,
either pass it through unchanged or rewrite it to comply with policy.

No tool calls, no loop — the entire playbook is the system prompt.

Env (set automatically by the sandbox runner):
  BRIDGE_URL, SESSION_TOKEN     — bridge connection (LLM routing only)
  HIVEMIND_AGENT_ROLE=mediator  — plugin registers no tools for this role
  HIVEMIND_MODEL                — model id passed to AIAgent
  RAW_OUTPUT                    — the query agent's raw output to filter
  QUERY_PROMPT                  — the original question
  MEDIATION_POLICY              — optional custom policy instructions
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_PLUGINS_DIR = os.environ.get("HERMES_BUNDLED_PLUGINS", "/opt/hivemind/plugins")
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)
import hivemind  # noqa: E402, F401  (registers nothing for role=mediator)

from run_agent import AIAgent  # noqa: E402

RAW_OUTPUT = os.environ.get("RAW_OUTPUT", "")
QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
MEDIATION_POLICY = os.environ.get("MEDIATION_POLICY", "")
HIVEMIND_MODEL = os.environ.get("HIVEMIND_MODEL", "moonshotai/kimi-2.6")

DEFAULT_SYSTEM_PROMPT = """\
You are the final response mediator.

Inputs:
- POLICY, if present.
- QUERY_PROMPT.
- RESPONSE TO FILTER.

If POLICY is present, enforce exactly that policy. Do not add extra
privacy rules, output-shape rules, aggregation rules, token rules, or
PII categories that the policy did not state. If the response is
compliant, return it unchanged.

If POLICY is absent, pass the response through except for obvious
credentials, secrets, system internals, tool traces, or debug output.

If rewriting is needed, make the smallest edit that satisfies the policy
while preserving useful content. Return only the final user-facing text:
no audit report, no preamble, no explanation of mediation.
"""

_PROMPT_FILE = Path("/app/prompt.md")
if _PROMPT_FILE.exists():
    SYSTEM_PROMPT = _PROMPT_FILE.read_text()
else:
    SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


def main() -> None:
    if not RAW_OUTPUT.strip():
        print("")
        return

    parts: list[str] = []
    if MEDIATION_POLICY:
        parts.append(f"POLICY:\n{MEDIATION_POLICY}")
    parts.append(f"QUERY_PROMPT:\n{QUERY_PROMPT}")
    parts.append(f"RESPONSE TO FILTER:\n{RAW_OUTPUT}")
    parts.append(
        "\nINSTRUCTION: Output ONLY the filtered response text. "
        "If compliant, output it exactly as-is. If it has violations, "
        "output a rewritten version. NO audit reports, assessments, "
        "recommendations, or commentary. Your output goes directly to "
        "the end user."
    )
    body = "\n\n".join(parts)

    base_url = os.environ["BRIDGE_URL"].rstrip("/") + "/v1"
    api_key = os.environ["SESSION_TOKEN"]

    try:
        agent = AIAgent(
            base_url=base_url,
            api_key=api_key,
            provider="custom",
            model=HIVEMIND_MODEL,
            max_iterations=1,
            enabled_toolsets=[],  # tool-less single-shot rewriter
            ephemeral_system_prompt=SYSTEM_PROMPT,
            skip_context_files=True,
            skip_memory=True,
            quiet_mode=True,
            save_trajectories=False,
        )
        response = agent.chat(body)
    except Exception as e:
        print(f"AIAgent error: {e}", file=sys.stderr)
        # Fail closed on the user-facing channel.
        print("Unable to process response due to an internal error.")
        return

    print(response or "Unable to process response due to an internal error.")


if __name__ == "__main__":
    main()

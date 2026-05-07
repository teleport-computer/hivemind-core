"""Default mediator agent — fully autonomous Claude Code for output auditing.

Audits query agent output for policy compliance before returning to the user.

Env vars (set automatically by the sandbox):
  BRIDGE_URL, SESSION_TOKEN — bridge connection
  ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY — SDK routes LLM calls through bridge
  RAW_OUTPUT — the query agent's raw output to audit
  QUERY_PROMPT — the original question
  MEDIATION_POLICY — optional custom policy instructions

Outputs filtered/audited text to stdout.
"""

import asyncio
import os
import sys
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

RAW_OUTPUT = os.environ.get("RAW_OUTPUT", "")
QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
MEDIATION_POLICY = os.environ.get("MEDIATION_POLICY", "")

MEDIATOR_SYSTEM = """\
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

# Override with external prompt file if present (same pattern as scope/query agents)
_PROMPT_FILE = Path("/app/mediator-prompt.md")
if _PROMPT_FILE.exists():
    MEDIATOR_SYSTEM = _PROMPT_FILE.read_text()


async def main() -> None:
    if not RAW_OUTPUT.strip():
        print("")
        return

    parts = []
    if MEDIATION_POLICY:
        parts.append(f"POLICY:\n{MEDIATION_POLICY}")
    parts.append(f"QUERY_PROMPT:\n{QUERY_PROMPT}")
    parts.append(f"RESPONSE TO FILTER:\n{RAW_OUTPUT}")
    parts.append(
        "\nINSTRUCTION: Output ONLY the filtered response text. "
        "If the response is policy-compliant, output it exactly as-is. "
        "If it has violations, output a rewritten version. "
        "Do NOT output any audit reports, assessments, recommendations, "
        "or commentary. Your output goes directly to the end user."
    )
    user_msg = "\n\n".join(parts)

    final_result = ""
    result_is_error = False
    captured_stderr: list[str] = []
    try:
        async for message in query(
            prompt=user_msg,
            options=ClaudeAgentOptions(
                system_prompt=MEDIATOR_SYSTEM,
                tools=[],
                max_turns=1,
                permission_mode="bypassPermissions",
                cwd="/tmp",
                stderr=captured_stderr.append,
                extra_args={"debug": "api,mcp,hooks"},
            ),
        ):
            if hasattr(message, "result"):
                final_result = message.result
                # ResultMessage carries is_error; preserve it so we only
                # pass through genuinely successful outputs.
                result_is_error = bool(getattr(message, "is_error", False))
    except Exception as e:
        # Claude Code CLI sometimes exits non-zero AFTER producing a valid
        # ResultMessage (e.g. on shutdown errors). If we already have a
        # non-error result in hand, trust it — otherwise fail closed.
        print(
            f"Agent SDK error: {e}\nCLI stderr:\n" + "".join(captured_stderr),
            file=sys.stderr,
        )
        if final_result and not result_is_error:
            print(final_result)
            return
        print("Unable to process response due to an internal error.")
        return

    if result_is_error:
        # Surface underlying CLI error text to stderr but still fail closed
        # on the stdout channel that the mediator returns to users.
        print(
            f"Mediator ResultMessage is_error=True, result={final_result!r}\n"
            f"CLI stderr:\n" + "".join(captured_stderr),
            file=sys.stderr,
        )
        print("Unable to process response due to an internal error.")
        return

    print(final_result)


if __name__ == "__main__":
    asyncio.run(main())

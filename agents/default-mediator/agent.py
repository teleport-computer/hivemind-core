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
PRIVACY GATE

You are a strict output mediator for a public response channel.

Task:
1. Review the candidate response for content that violates POLICY.
2. If it is compliant, return it unchanged.
3. If not compliant, rewrite minimally so it is safe.
4. If the answer cannot be made safe without dropping core content,
   return a brief, generic safe answer.

## POLICY PRIORITY

If a POLICY section appears in the user message below, THAT is the
authoritative policy for this request. Enforce exactly what POLICY
says — nothing more, nothing less. Do not over-restrict by adding
categories POLICY didn't mention. Do not under-restrict by ignoring
categories it did mention. A mediator that applies a fixed
"standard PII list" regardless of POLICY is wrong — it overfits to
assumed policies and fails on out-of-distribution ones.

If NO POLICY section is provided, fall back to the "Default safe
categories" list below. This list is a backstop for unscoped use,
not a universal truth.

## Default safe categories (ONLY used when POLICY is absent)
- Names of people, companies, organizations, products, usernames, handles.
- Substance use (drugs, alcohol, smoking, intoxication).
- Mental health details (diagnoses, therapy, medications, breakdowns, internal struggles).
- Family conflict and relationship drama.
- Work conflict/failures (fired, toxic boss/coworker conflict, rage quitting).
- Financial stress (debt, money anxiety, hardship specifics).
- Medical issues (conditions, diagnoses, procedures, treatments).
- Credentials or secrets (passwords, tokens, API keys, private identifiers).
- Content from outside the provided response context.

# SPECIFIC REWRITE PATTERNS you should always apply:

## Generic failure → helpful explanation
If the candidate response is ONLY "Unable to process query due to an
internal error." (or similar generic error), REWRITE to a concise
first-person acknowledgement that the system can't answer that specific
question due to privacy protections, without speculating about why.
Example: "I'm not able to answer that specific question — the privacy
filter blocked the SQL patterns that would have been needed. Try a more
aggregate-style question (counts, ranges, categories) instead."

## Absence-confirmation → neutral-policy response
If the candidate confirms ABSENCE of a specific PII category (e.g.
"I didn't find any emails/phone numbers/names"), DO NOT PASS THROUGH.
Even "no matches" reveals metadata about the user's data. REWRITE to a
generic policy response that neither confirms nor denies:
"Specific PII content like emails, phone numbers, names, and handles
is redacted from responses. If you're looking for aggregate summaries
or trends, I can help with those instead."

## Schema/plumbing explanations → strip them
Users don't need to know about the database schema, tool names, or
internal infrastructure. If the response explains "the database
contains a conversations table with..." — STRIP IT. Keep only the
content that answers the user's question.

Output rules:
- Return only final response text. No preamble, no policy explanation.
- Do not mention that redaction happened.
- Keep useful, non-sensitive actions/ideas/facts when possible.
- Prefer neutral, concise phrasing.
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

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

from claude_agent_sdk import ClaudeAgentOptions, query

RAW_OUTPUT = os.environ.get("RAW_OUTPUT", "")
QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
MEDIATION_POLICY = os.environ.get("MEDIATION_POLICY", "")

MEDIATOR_SYSTEM = """\
PRIVACY GATE

You are a strict output mediator for a public response channel.
Treat all personal/sensitive information as high risk.

Task:
1. Review the candidate response for sensitive content.
2. If it is compliant, return it unchanged.
3. If not compliant, rewrite minimally so it is safe.
4. If the answer cannot be made safe without dropping core content,
   return a brief, generic safe answer.

Always strip or avoid:
- Names of people, companies, organizations, products, usernames, handles.
- Substance use (drugs, alcohol, smoking, intoxication).
- Mental health details (diagnoses, therapy, medications, breakdowns, internal struggles).
- Family conflict and relationship drama.
- Work conflict/failures (fired, toxic boss/coworker conflict, rage quitting).
- Financial stress (debt, money anxiety, hardship specifics).
- Medical issues (conditions, diagnoses, procedures, treatments).
- Credentials or secrets (passwords, tokens, API keys, private identifiers).
- Content from outside the provided response context.

Output rules:
- Return only final response text. No preamble, no policy explanation.
- Do not mention that redaction happened.
- Keep useful, non-sensitive actions/ideas/facts when possible.
- Prefer neutral, concise phrasing.
"""


async def main() -> None:
    if not RAW_OUTPUT.strip():
        print("")
        return

    user_msg = (
        f"QUERY_PROMPT:\n{QUERY_PROMPT}\n\n"
        f"RESPONSE TO AUDIT:\n{RAW_OUTPUT}"
    )
    if MEDIATION_POLICY:
        user_msg = f"POLICY:\n{MEDIATION_POLICY}\n\n{user_msg}"

    final_result = ""
    try:
        async for message in query(
            prompt=user_msg,
            options=ClaudeAgentOptions(
                system_prompt=MEDIATOR_SYSTEM,
                permission_mode="bypassPermissions",
            ),
        ):
            if hasattr(message, "result"):
                final_result = message.result
    except Exception as e:
        # Mediator fails closed: if we can't audit, don't pass through raw output
        print(f"Agent SDK error: {e}", file=sys.stderr)
        print("Unable to process response due to an internal error.")
        return

    print(final_result)


if __name__ == "__main__":
    asyncio.run(main())

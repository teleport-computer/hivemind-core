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
PRIVACY GATE

You are a strict output mediator for a public response channel.

Task:
1. Review the candidate response for content that violates POLICY.
2. If it is compliant, return it unchanged.
3. If not compliant, rewrite minimally so it is safe.
4. If the answer cannot be made safe without dropping core content,
   return a brief, generic safe answer.

## POLICY PRIORITY

If a POLICY section appears in the user message, THAT is the
authoritative policy. Enforce exactly what POLICY says — nothing more,
nothing less. If NO POLICY section is provided, fall back to the
"Default safe categories" list below.

## Default safe categories (ONLY when POLICY is absent)
- Names of people, companies, organizations, products, usernames, handles.
- Substance use, mental health details, medical issues.
- Family conflict, relationship drama, work conflict, financial stress.
- Credentials or secrets (passwords, tokens, API keys).
- Content from outside the provided response context.

## Common rewrite patterns

Generic plumbing failures ("Unable to process query", "scope agent
failed", schema dumps) → rewrite as a first-person acknowledgment that
the privacy filter blocked the question; suggest aggregate alternatives.

Absence-confirmation ("I didn't find any emails", "no @ handles") →
rewrite to a neutral-policy response that neither confirms nor denies.

Schema / infrastructure plumbing → strip; users don't need to know
about tables, MCP, scope_fn internals.

## Output rules

Return ONLY the final response text. The user reads this verbatim.
NO audit reports, NO "Issues Identified" sections, NO preamble like
"Here is the filtered response", NO mention that mediation happened.
Preserve tone and format of the original output where possible.
When POLICY explicitly allows aggregate statistics, exact aggregate counts,
dates, buckets, rankings, and summaries are compliant. Do not rewrite a safe
aggregate answer into a generic refusal just because it contains a specific
date or count.
"""

_PROMPT_FILE = Path("/app/prompt.md")
if _PROMPT_FILE.exists():
    SYSTEM_PROMPT = _PROMPT_FILE.read_text()
else:
    SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


def _policy_allows_aggregates(policy: str) -> bool:
    p = policy.lower()
    return any(
        term in p
        for term in (
            "aggregate",
            "statistics",
            "summaries",
            "summary",
            "counts",
            "trends",
            "rankings",
            "histogram",
        )
    )


def _safe_fast_path_response(raw_output: str, policy: str) -> str | None:
    """Pass obviously safe aggregate/refusal outputs without an LLM call."""
    text = (raw_output or "").strip()
    if not text:
        return ""
    if len(text) > 2000:
        return None

    lower = text.lower()
    sensitive_markers = (
        "http://",
        "https://",
        "sec_user_id",
        "user_id",
        "viewer_id",
        "video_id",
        "author_id",
        "system prompt",
        "tool call",
        "execute_sql",
        "scope_fn",
        "select ",
        "password",
        "api_key",
        " token",
    )
    if any(marker in lower for marker in sensitive_markers):
        return None

    safe_refusal_markers = (
        "cannot provide raw rows",
        "cannot reveal hidden prompts",
        "cannot reveal",
        "can provide aggregate statistics",
        "raw row content redacted",
    )
    if any(marker in lower for marker in safe_refusal_markers):
        return text

    if not _policy_allows_aggregates(policy):
        return None

    aggregate_markers = (
        "total_rows:",
        "first_watch:",
        "last_watch:",
        "watch_day:",
        "videos:",
        "count:",
        "aggregate",
        "summary",
        "trend",
        "ranking",
    )
    if any(marker in lower for marker in aggregate_markers):
        return text

    return None


def main() -> None:
    if not RAW_OUTPUT.strip():
        print("")
        return

    fast = _safe_fast_path_response(RAW_OUTPUT, MEDIATION_POLICY)
    if fast is not None:
        print(fast)
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

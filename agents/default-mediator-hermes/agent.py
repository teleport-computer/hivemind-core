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
from contextlib import redirect_stdout
from pathlib import Path

_PLUGINS_DIR = os.environ.get("HERMES_BUNDLED_PLUGINS", "/opt/hivemind/plugins")
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)


def _isolate_hivemind_toolset() -> None:
    """Keep Hermes startup from importing unrelated built-in tool modules."""
    if os.environ.get("HIVEMIND_HERMES_ENABLE_BUILTIN_TOOLS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    try:
        from tools import registry as hermes_registry  # type: ignore
    except Exception:
        return
    hermes_registry.discover_builtin_tools = lambda *args, **kwargs: []


_isolate_hivemind_toolset()
import hivemind  # noqa: E402, F401  (registers nothing for role=mediator)

from run_agent import AIAgent  # noqa: E402

RAW_OUTPUT = os.environ.get("RAW_OUTPUT", "")
QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
MEDIATION_POLICY = os.environ.get("MEDIATION_POLICY", "")
HIVEMIND_MODEL = os.environ.get("HIVEMIND_MODEL", "moonshotai/kimi-k2.6")

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

If POLICY is absent, pass the response through except for obvious credentials,
secrets, system internals, tool traces, or debug output.

If rewriting is needed, make the smallest policy-compliant edit. Preserve the
response's depth, structure, tables, and report length whenever they comply
with policy. Return only the final user-facing text: no audit report, preamble,
or commentary.
"""

_PROMPT_FILE = Path("/app/prompt.md")
if _PROMPT_FILE.exists():
    SYSTEM_PROMPT = _PROMPT_FILE.read_text()
else:
    SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


_NO_REASONING_CONFIG = {"enabled": False, "effort": "none"}
_NO_REASONING_OVERRIDES = {
    "extra_body": {"reasoning": {"effort": "none", "exclude": True}}
}
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
_SECRET_MARKERS = (
    "api_key",
    "api key",
    "password",
    "secret",
    "bearer ",
    "-----begin ",
    "sk-",
    "ghp_",
    "gho_",
    "hmk_",
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


def _looks_like_runtime_failure(text: str) -> bool:
    lower = (text or "").lower()
    return any(marker in lower for marker in _HERMES_FAILURE_MARKERS)


def _needs_safety_filter(text: str) -> bool:
    lower = (text or "").lower()
    return any(marker in lower for marker in _SECRET_MARKERS)


def _always_llm_filter_enabled() -> bool:
    return os.environ.get("HIVEMIND_MEDIATOR_ALWAYS_LLM", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _mediator_internal_error() -> str:
    return "Unable to process response due to an internal error."


def main() -> None:
    if not RAW_OUTPUT.strip():
        print("")
        return
    if _looks_like_runtime_failure(RAW_OUTPUT):
        print("query agent emitted Hermes runtime diagnostics; failing closed", file=sys.stderr)
        print(
            "Unable to complete the request because the model provider or "
            "agent runtime failed before producing a usable answer."
        )
        return
    # The scope function is the privacy boundary for database access. Default
    # mediator behavior preserves utility by passing clean outputs through;
    # operators can opt into LLM rewriting for stricter policy experiments.
    if not _always_llm_filter_enabled() and not _needs_safety_filter(RAW_OUTPUT):
        print(RAW_OUTPUT)
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
            max_tokens=_completion_token_cap(),
            reasoning_config=_NO_REASONING_CONFIG,
            request_overrides=_NO_REASONING_OVERRIDES,
        )
        with redirect_stdout(sys.stderr):
            response = agent.chat(body)
    except Exception as e:
        print(f"AIAgent error: {e}", file=sys.stderr)
        # Fail closed on the user-facing channel.
        print(_mediator_internal_error())
        return

    if not response or _looks_like_runtime_failure(response):
        print(f"Hermes runtime failure from mediator: {(response or '')[:500]}", file=sys.stderr)
        print(_mediator_internal_error())
        return

    print(response)


if __name__ == "__main__":
    main()

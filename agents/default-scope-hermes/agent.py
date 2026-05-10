"""Default scope agent — Hermes harness.

Same role as agents/default-scope/agent.py: emit a `scope_fn` that
transforms the query agent's rows before they reach the user, given a
question + an optional room policy. Emits a single JSON object
`{"scope_fn": "..."}` on the final line of stdout.

Uses Hermes' Python AIAgent API. The system prompt below is a focused
distillation of the Claude-Code scope prompt — it omits workspace-mount
and Write-tool workflows that Hermes doesn't expose. If empirical eval
shows the agent needs deeper playbook material mid-loop, promote those
sections to a Hermes skill (markdown under HERMES_BUNDLED_PLUGINS or
HERMES_HOME/skills) and preload via toolsets/skills config.

Env (set automatically by the sandbox runner):
  BRIDGE_URL, SESSION_TOKEN  — bridge connection
  HIVEMIND_AGENT_ROLE=scope  — plugin registers verify/simulate tools
  HIVEMIND_MODEL             — model id passed to AIAgent
  QUERY_PROMPT               — the user's question
  QUERY_AGENT_ID             — the query agent simulate_* will run
  POLICY_CONTEXT             — optional room policy to enforce
"""

from __future__ import annotations

import json
import os
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

import httpx

_PLUGINS_DIR = os.environ.get("HERMES_BUNDLED_PLUGINS", "/opt/hivemind/plugins")
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)
import hivemind  # noqa: E402, F401

from run_agent import AIAgent  # noqa: E402

QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
QUERY_AGENT_ID = os.environ.get("QUERY_AGENT_ID", "")
POLICY_CONTEXT = os.environ.get("POLICY_CONTEXT", "").strip()
HIVEMIND_MODEL = os.environ.get("HIVEMIND_MODEL", "moonshotai/kimi-2.6")

DEFAULT_SYSTEM_PROMPT = """\
You emit one Python row transformer for a hivemind room.

Goal: find the best privacy/utility frontier for this question and room. If
POLICY is present, it is authoritative: enforce exactly that policy,
with no extra categories and no missing categories. Do not apply canned policies.
If no policy is present, use first-principles data minimization.
Preserve useful information whenever it is allowed; remove or transform only
what is necessary for compliance.

Treat policy as both permissions and restrictions. When policy allows a class
of information, preserve that class whenever the row shape already fits it or
can be transformed into it. Do not suppress allowed summary metrics,
allowed row-level records, allowed identifiers, or allowed derived fields just
because another policy might forbid them.

For analytical/report prompts, useful disclosure is often aggregate or
statistical SQL output. If policy allows that shape, preserve grouping/bucket
fields plus metric fields, even when aliases are domain-specific instead of
generic names like count, total, sum, min, max, or avg. Return an empty list
only after you have no policy-compliant useful disclosure to preserve.

Tools:
- get_schema(): inspect tables, columns, and types.
- execute_sql(sql, params): sample or compute facts needed for the policy.
- verify_scope_fn(source, tests): fast compile/test of your candidate.
- simulate_query(scope_fn_source, prompt) and simulate_multi(candidates, prompt):
  expensive downstream checks; use only when the tradeoff is unclear.
- list_query_agent_files() and read_query_agent_file(path): inspect the query
  agent if its behavior matters.

Process:
1. Read the policy and question.
2. Use get_schema and small execute_sql checks only when they help understand
   the data shape or policy boundary.
3. Draft the least destructive compliant transform: pass through, filter rows,
   drop or replace fields, derive safer fields, summarize, or return no rows.
4. Use verify_scope_fn on the exact function you will emit. Use simulate_query
   or simulate_multi only when comparing candidates would materially clarify
   the privacy/utility frontier.
For analytical/report prompts, your verified function should preserve useful
summary rows in tests; a function that compiles but drops every useful metric
row is not a good frontier.

Function contract:
- Signature exactly `def scope(sql, params, rows):`.
- Return exactly `{"allow": True, "rows": <list of dicts>}`.
- Never return `{"allow": False, ...}`.
- No imports, exec, eval, open, __import__, dunder access, or classes.
- Use only simple Python builtins and str/list/dict methods.

Final output must be one single-line JSON object and nothing else:
{"scope_fn": "def scope(sql, params, rows):\\n    ..."}
"""

_PROMPT_FILE = Path("/app/prompt.md")
if _PROMPT_FILE.exists():
    SYSTEM_PROMPT = _PROMPT_FILE.read_text()
else:
    SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


_NO_REASONING_CONFIG = {"enabled": False, "effort": "none"}
_NO_REASONING_OVERRIDES = {"extra_body": {"reasoning": {"effort": "none", "exclude": True}}}


_EMPTY_FALLBACK_SCOPE_FN = 'def scope(sql, params, rows):\n    return {"allow": True, "rows": []}\n'
_MAX_RETRY_CONTEXT_CHARS = 3000
_VERIFY_TESTS = [
    {
        "sql": "SELECT bucket, COUNT(*)::int AS total FROM events GROUP BY bucket",
        "params": [],
        "rows": [{"bucket": "2026-04-15", "total": 482237}],
        "expect_allow": True,
        "label": "summary metric row is preserved",
    },
    {
        "sql": "SELECT hashtag, watches FROM events ORDER BY watches DESC LIMIT 10",
        "params": [],
        "rows": [{"hashtag": "fyp", "watches": 2442}],
        "expect_allow": True,
        "label": "top-N summary row is preserved",
    },
]


def _completion_token_cap(default: int = 4096, hard_cap: int = 8192) -> int:
    raw_budget = os.environ.get("BUDGET_MAX_TOKENS", "")
    try:
        budget = int(raw_budget)
    except ValueError:
        budget = 0
    if budget > 0:
        budget_cap = max(1024, budget // 8)
        return max(1024, min(default, hard_cap, budget_cap))
    return min(default, hard_cap)


def _is_analytical_prompt() -> bool:
    text = QUERY_PROMPT.lower()
    markers = (
        "research",
        "report",
        "study",
        "analysis",
        "lifecycle",
        "deep dive",
        "memo",
        "findings",
    )
    return any(marker in text for marker in markers)


def _run_ai_agent(body: str) -> str:
    base_url = os.environ["BRIDGE_URL"].rstrip("/") + "/v1"
    api_key = os.environ["SESSION_TOKEN"]

    agent = AIAgent(
        base_url=base_url,
        api_key=api_key,
        provider="custom",
        model=HIVEMIND_MODEL,
        # Match agents/default-scope: hard cap 20, target emit by 10.
        max_iterations=20,
        enabled_toolsets=["hivemind"],
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
        return agent.chat(body) or ""


def _retry_body(body: str, reason: str, previous_response: str) -> str:
    previous = (previous_response or "").strip()
    if len(previous) > _MAX_RETRY_CONTEXT_CHARS:
        previous = previous[:_MAX_RETRY_CONTEXT_CHARS] + "\n[truncated]"
    return (
        f"{body}\n\n"
        "RECOVERY INSTRUCTION:\n"
        f"The previous scope attempt was not usable: {reason}.\n"
        "Return only one compact JSON object with a short scope_fn string. "
        "No markdown, no explanation, no audit report. Enforce the policy "
        "exactly and preserve allowed information when possible.\n\n"
        f"PREVIOUS RESPONSE:\n{previous}"
    )


def _retry_scope_emit(body: str, *, reason: str, previous_response: str) -> dict | None:
    try:
        retry_response = _run_ai_agent(_retry_body(body, reason, previous_response))
    except Exception as e:
        print(f"scope agent retry error after {reason}: {e}", file=sys.stderr)
        return None
    parsed = _extract_json_emit(retry_response)
    if parsed and isinstance(parsed.get("scope_fn"), str) and parsed["scope_fn"].strip():
        return parsed
    print(
        f"scope agent retry produced no parseable JSON after {reason}. "
        f"raw={retry_response[:500]!r}",
        file=sys.stderr,
    )
    return None


def _verify_scope_source(source: str) -> tuple[bool, str]:
    bridge_url = os.environ.get("BRIDGE_URL", "").rstrip("/")
    session_token = os.environ.get("SESSION_TOKEN", "")
    if not bridge_url or not session_token:
        return True, "bridge verification unavailable"
    try:
        resp = httpx.post(
            f"{bridge_url}/sandbox/verify_scope_fn",
            json={"source": source, "tests": _VERIFY_TESTS},
            headers={"Authorization": f"Bearer {session_token}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"scope self-verify unavailable: {e}", file=sys.stderr)
        return True, "bridge verification unavailable"

    if not data.get("compiles"):
        return False, str(data.get("compile_error") or "compile failed")
    if not data.get("all_tests_passed"):
        return False, json.dumps(data.get("results", [])[:3])
    if _is_analytical_prompt():
        empty_results = [
            item for item in data.get("results", [])
            if int(item.get("rows_returned") or 0) == 0
        ]
        if empty_results:
            return False, (
                "scope_fn dropped useful synthetic summary rows: "
                + json.dumps(empty_results[:3])
            )
    return True, "ok"


def _extract_json_emit(text: str) -> dict | None:
    """Pull the last JSON object containing `scope_fn` from the agent's output."""
    if not isinstance(text, str) or not text:
        return None
    text = text.strip()

    def _scope_source(src: object) -> str | None:
        if not isinstance(src, str):
            return None
        for line in src.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("@"):
                continue
            if s.startswith("def scope(") or s.startswith("def scope ("):
                return src
            return None
        return None

    def _scrape_def_scope(candidate: str) -> str | None:
        match = re.search(
            r"(?m)^[ \t]*(def\s+scope\s*\([^)]*\)\s*(?:->\s*[^:]+)?\s*:)",
            candidate,
        )
        if not match:
            return None
        lines = candidate[match.start() :].splitlines()
        out = [lines[0]]
        for line in lines[1:]:
            stripped = line.lstrip()
            if not stripped:
                out.append(line)
                continue
            if line[:1] not in (" ", "\t"):
                if stripped.startswith("```"):
                    break
                break
            out.append(line)
        while out and not out[-1].strip():
            out.pop()
        return "\n".join(out) if out else None

    def _validate_or_rescue(obj: object) -> dict | None:
        if not (isinstance(obj, dict) and "scope_fn" in obj):
            return None
        src = _scope_source(obj.get("scope_fn"))
        if src:
            return {"scope_fn": src}
        for candidate in (obj.get("scope_fn"), text):
            if isinstance(candidate, str):
                rescued = _scrape_def_scope(candidate)
                if _scope_source(rescued):
                    return {"scope_fn": rescued}
        return None

    candidates = [text]
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            inner = "\n".join(lines[1:])
            if inner.rstrip().endswith("```"):
                inner = inner.rstrip()[:-3]
            candidates.append(inner.strip())

    for candidate in candidates:
        try:
            result = _validate_or_rescue(json.loads(candidate))
            if result:
                return result
        except json.JSONDecodeError:
            continue

    decoder = json.JSONDecoder()
    found: dict | None = None
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        result = _validate_or_rescue(obj)
        if result:
            found = result
    if found:
        return found

    rescued = _scrape_def_scope(text)
    if _scope_source(rescued):
        return {"scope_fn": rescued}

    return None


def main() -> None:
    if not QUERY_PROMPT.strip():
        # Fail closed with no disclosure when there is no question to scope.
        print(json.dumps({"scope_fn": _EMPTY_FALLBACK_SCOPE_FN}))
        return

    parts: list[str] = []
    if POLICY_CONTEXT:
        parts.append(f"POLICY:\n{POLICY_CONTEXT}")
    parts.append(f"QUESTION:\n{QUERY_PROMPT}")
    body = "\n\n".join(parts)

    response = ""
    try:
        response = _run_ai_agent(body)
    except Exception as e:
        print(f"AIAgent error: {e}", file=sys.stderr)

    parsed = _extract_json_emit(response)
    if parsed and isinstance(parsed.get("scope_fn"), str) and parsed["scope_fn"].strip():
        verified, reason = _verify_scope_source(parsed["scope_fn"])
        if not verified:
            print(f"scope self-verify failed: {reason}", file=sys.stderr)
            retry = _retry_scope_emit(
                body,
                reason=f"scope_fn failed self verification: {reason}",
                previous_response=response,
            )
            if retry:
                retry_verified, retry_reason = _verify_scope_source(retry["scope_fn"])
                if retry_verified:
                    print(json.dumps({"scope_fn": retry["scope_fn"]}))
                    return
                print(
                    f"scope retry self-verify failed: {retry_reason}",
                    file=sys.stderr,
                )
            print(json.dumps({"scope_fn": _EMPTY_FALLBACK_SCOPE_FN}))
            return
        else:
            # Re-emit canonically so the pipeline parses cleanly.
            print(json.dumps({"scope_fn": parsed["scope_fn"]}))
            return

    # Fail closed with no disclosure so the pipeline can complete rather
    # than HARD FAIL on bad JSON.
    retry = _retry_scope_emit(
        body,
        reason="unparseable or truncated scope JSON",
        previous_response=response,
    )
    if retry:
        print(json.dumps({"scope_fn": retry["scope_fn"]}))
        return
    print(
        f"scope agent produced no parseable JSON; using fallback. raw={response[:500]!r}",
        file=sys.stderr,
    )
    print(json.dumps({"scope_fn": _EMPTY_FALLBACK_SCOPE_FN}))


if __name__ == "__main__":
    main()

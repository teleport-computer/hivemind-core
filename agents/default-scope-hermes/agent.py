"""Default scope agent — Hermes harness.

Same role as agents/default-scope/agent.py: emit a `scope_fn` that
transforms the query agent's rows before they reach the user, given a
question + an optional MEDIATION_POLICY. Emits a single JSON object
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
  POLICY_CONTEXT             — optional MEDIATION_POLICY to enforce
"""

from __future__ import annotations

import json
import os
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

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

Goal: preserve the most useful information the room policy permits while
preventing what it forbids. Do not apply canned policies. If
MEDIATION_POLICY is present, it is authoritative: enforce exactly that policy,
with no extra categories and no missing categories. If no policy is present,
use first-principles data minimization.

Utility matters. Do not add unstated granularity rules. If the policy permits
aggregate statistics, preserve compliant grouped or bucketed result rows such
as counts, sums, averages, rankings, or trends unless the policy explicitly
forbids that grouping key or field. Empty rows are appropriate only when the
requested result shape violates the policy or when every returned row violates
the policy.

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
2. Inspect schema, and sample/compute only what is needed to understand the
   privacy/utility tradeoff.
3. Write the least destructive compliant transform on the privacy/utility
   frontier: pass through, filter rows, drop or replace fields, derive safer
   fields, summarize, or return no rows. Preserve allowed information when the
   row shape already fits the policy.
4. Call verify_scope_fn on the exact function you will emit. Include tests for
   both an allowed result shape and a forbidden result shape when the policy
   distinguishes them.

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
_ENABLE_AGGREGATE_FALLBACK = os.environ.get(
    "HIVEMIND_SCOPE_AGGREGATE_FALLBACK", ""
).strip().lower() in {"1", "true", "yes", "on"}


_EMPTY_FALLBACK_SCOPE_FN = 'def scope(sql, params, rows):\n    return {"allow": True, "rows": []}\n'

_AGGREGATE_FALLBACK_SCOPE_FN = (
    "def scope(sql, params, rows):\n"
    "    sql_l = str(sql).lower()\n"
    "    aggregate_sql = False\n"
    '    for marker in ("group by", "count(", "sum(", "avg(", "min(", "max("):\n'
    "        if marker in sql_l:\n"
    "            aggregate_sql = True\n"
    "    safe = []\n"
    "    if aggregate_sql:\n"
    "        for row in rows:\n"
    "            if isinstance(row, dict):\n"
    "                has_metric = False\n"
    "                for value in row.values():\n"
    "                    if isinstance(value, (int, float)) and not isinstance(value, bool):\n"
    "                        has_metric = True\n"
    "                if has_metric:\n"
    "                    safe.append(dict(row))\n"
    '    return {"allow": True, "rows": safe}\n'
)


def _aggregate_fallback_is_policy_appropriate() -> bool:
    text = f"{POLICY_CONTEXT}\n{QUERY_PROMPT}".lower()
    policy = POLICY_CONTEXT.lower()
    query = QUERY_PROMPT.lower()
    aggregate_terms = (
        "aggregate",
        "statistic",
        "statistics",
        "count",
        "counts",
        "ranking",
        "rankings",
        "trend",
        "trends",
        "summary",
        "summaries",
        "highest",
        "lowest",
        "most",
        "least",
        "average",
        "total",
        "number of",
    )
    negative_aggregate_phrases = (
        "not allowed: aggregate",
        "not allowed aggregate",
        "forbid aggregate",
        "forbidden aggregate",
        "disallow aggregate",
        "disallowed aggregate",
    )
    policy_allows_aggregate = "allowed" in policy and any(
        term in policy for term in aggregate_terms
    )
    query_asks_aggregate = any(term in query for term in aggregate_terms)
    policy_denies_aggregate = any(phrase in text for phrase in negative_aggregate_phrases)
    return policy_allows_aggregate and query_asks_aggregate and not policy_denies_aggregate


def _statically_erases_rows(source: str) -> bool:
    compact = re.sub(r"\s+", "", source)
    return '"rows":[]' in compact or "'rows':[]" in compact


def _fallback_scope_fn_source() -> str:
    if _ENABLE_AGGREGATE_FALLBACK and _aggregate_fallback_is_policy_appropriate():
        return _AGGREGATE_FALLBACK_SCOPE_FN
    return _EMPTY_FALLBACK_SCOPE_FN


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
        parts.append(f"MEDIATION_POLICY:\n{POLICY_CONTEXT}")
    parts.append(f"QUESTION:\n{QUERY_PROMPT}")
    body = "\n\n".join(parts)

    base_url = os.environ["BRIDGE_URL"].rstrip("/") + "/v1"
    api_key = os.environ["SESSION_TOKEN"]

    response = ""
    try:
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
            max_tokens=2048,
            reasoning_config=_NO_REASONING_CONFIG,
            request_overrides=_NO_REASONING_OVERRIDES,
        )
        with redirect_stdout(sys.stderr):
            response = agent.chat(body) or ""
    except Exception as e:
        print(f"AIAgent error: {e}", file=sys.stderr)

    parsed = _extract_json_emit(response)
    if parsed and isinstance(parsed.get("scope_fn"), str) and parsed["scope_fn"].strip():
        if (
            _ENABLE_AGGREGATE_FALLBACK
            and _aggregate_fallback_is_policy_appropriate()
            and _statically_erases_rows(parsed["scope_fn"])
        ):
            print(
                "scope agent emitted static empty rows for allowed aggregate; "
                "using aggregate fallback.",
                file=sys.stderr,
            )
            print(json.dumps({"scope_fn": _AGGREGATE_FALLBACK_SCOPE_FN}))
            return
        # Re-emit canonically so the pipeline parses cleanly.
        print(json.dumps({"scope_fn": parsed["scope_fn"]}))
        return

    # Fail closed with no disclosure so the pipeline can complete rather
    # than HARD FAIL on bad JSON.
    print(
        f"scope agent produced no parseable JSON; using fallback. raw={response[:500]!r}",
        file=sys.stderr,
    )
    print(json.dumps({"scope_fn": _fallback_scope_fn_source()}))


if __name__ == "__main__":
    main()

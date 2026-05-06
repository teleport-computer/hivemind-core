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
You are the ROW TRANSFORMER.

Output a single Python function `scope(sql, params, rows)` that
transforms the query agent's result rows into a privacy-safe shape
before they reach the downstream answer.

# THE CONTRACT

The function MUST:
  1. Have signature EXACTLY `def scope(sql, params, rows):`. Three params.
  2. Return `{"allow": True, "rows": <transformed_list_of_dicts>}`.
  3. NEVER return `{"allow": False, ...}`. The host's AST validator
     REJECTS deny-paths and the query HARD FAILS. Transform rows to a
     safe shape; do not block.
  4. Use ONLY these builtins: len, str, int, float, bool, list, dict,
     set, tuple, min, max, sum, sorted, any, all, abs, round, enumerate,
     zip, range, isinstance. Plus standard str/list/dict methods.
  5. NO `import` statements.
  6. NO exec, eval, open, __import__, no dunder attribute access.
  7. NO class definitions.

# YOUR TOOLS

  get_schema() — returns user tables + columns. Use FIRST.
  execute_sql(sql, params) — sample data to learn row shapes.
  verify_scope_fn(source, tests) — FAST (ms). Compile + test the
    candidate transform against synthetic test cases. NO LLM call.
  simulate_query(scope_fn_source, prompt) — SLOW (~60s, nested LLM
    run). Plays the query agent as an NPC with your candidate scope_fn
    and returns the output the USER would actually see. Use as a
    save / load test before emitting your final JSON.
  simulate_multi(candidates, prompt) — same budget as ONE simulate_query
    but runs up to 3 candidates in parallel. Use when the right strategy
    is ambiguous (row-exclusion vs value-redaction vs aggregation).
  list_query_agent_files() — list the NPC's source files.
  read_query_agent_file(path) — read one file (e.g. 'agent.py',
    'query-prompt.md') to understand exactly what the query agent will
    do under your scope_fn.

No external network. No file-write or shell tools.

# THE NPC-SIMULATOR VIEW — save / load / revert

You are playing a security-review game. Your character:
  - CAN READ the NPC (query agent) source via list_query_agent_files +
    read_query_agent_file. The source API is read-only — you cannot modify
    the query agent's code or prompt; you can only change YOUR scope_fn.
  - CAN RUN the NPC with a candidate scope_fn via simulate_query. Each
    call is a fresh query-agent run with a clean slate — no state
    persists between simulations. Treat each as a save / load / retry.
  - CAN REVERT at zero cost. If the simulated output leaks or is
    useless, revise your scope_fn and call simulate_query again.

Typical loop:
  1. read_query_agent_file('agent.py') and the query prompt to
     understand the NPC's workflow.
  2. get_schema + execute_sql to see actual row shapes.
  3. Draft a candidate scope_fn.
  4. verify_scope_fn(source, tests) — compile-check.
  5. simulate_query — see what final output the user would actually get.
  6. If the output leaks or is useless, revise and step 4 again.
  7. When output is SAFE + USEFUL, emit the final JSON.

# HARD PROTOCOL — YOU MUST CALL verify_scope_fn BEFORE EMITTING

Before your final JSON emit, call verify_scope_fn at least once on the
scope_fn you plan to emit. A final emit with zero prior verify calls is
a PROTOCOL VIOLATION — the host substitutes a safe fallback and your
utility score craters.

Common failure modes verify_scope_fn catches:
  - Wrong signature (`scope_fn` instead of `scope`).
  - Returns `{"allow": False, ...}` — rejected.
  - Uses SQL-text gating instead of row transformation.
  - Imports modules / uses forbidden builtins.

# BUDGET

simulate_query is expensive — use AT MOST TWICE per invocation.
verify_scope_fn is cheap — use freely. Hard turn cap is 20.
Target: emit valid JSON by turn 6. By turn 10 emit whatever you have —
an imperfect transformer that ships beats one that doesn't.

# OUTPUT

Your FINAL message MUST be exactly one JSON object on a single line:

    {"scope_fn": "def scope(sql, params, rows):\\n    ..."}

Nothing else. No prose, no markdown fences, no commentary. The host
parses your final message as JSON.
"""

_PROMPT_FILE = Path("/app/prompt.md")
if _PROMPT_FILE.exists():
    SYSTEM_PROMPT = _PROMPT_FILE.read_text()
else:
    SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


_FALLBACK_SCOPE_FN = (
    "def scope(sql, params, rows):\n"
    "    return {\"allow\": True, \"rows\": [{\"match_count\": len(rows)}]}\n"
)

_AGGREGATE_POLICY_SCOPE_FN = """\
def scope(sql, params, rows):
    # aggregate_policy_fast_path: preserve aggregate outputs, collapse raw rows.
    if not rows:
        return {"allow": True, "rows": []}
    sensitive = {
        "sec_user_id", "user_id", "viewer_id", "video_id", "url",
        "title", "description", "author_id", "raw_title",
        "raw_description", "email", "phone", "token", "secret",
        "password", "api_key",
    }
    metric_names = {
        "count", "n", "total", "total_rows", "row_count",
        "match_count", "videos", "watches", "views", "likes",
        "comments", "shares", "average", "avg", "sum", "min", "max",
    }
    dimension_names = {
        "watch_day", "day", "date", "week", "month", "hashtag",
        "hashtags", "music", "bucket", "period", "trend",
        "first_watch", "last_watch",
    }
    out = []
    for row in rows:
        clean = {}
        raw_like = False
        has_metric = False
        for key, value in row.items():
            lk = str(key).lower()
            if lk in sensitive:
                raw_like = True
            if (
                lk in metric_names
                or "count" in lk
                or lk.endswith("_total")
                or lk.startswith("total_")
                or lk.startswith("avg_")
                or lk.startswith("min_")
                or lk.startswith("max_")
            ):
                has_metric = True
        if raw_like:
            continue
        for key, value in row.items():
            lk = str(key).lower()
            if (
                lk in metric_names
                or lk in dimension_names
                or "count" in lk
                or lk.endswith("_total")
                or lk.startswith("total_")
                or lk.startswith("avg_")
                or lk.startswith("min_")
                or lk.startswith("max_")
            ):
                clean[key] = value
        if clean and has_metric:
            out.append(clean)
        if len(out) >= 50:
            break
    if out:
        return {"allow": True, "rows": out}
    return {
        "allow": True,
        "rows": [{
            "policy_note": "raw row content redacted by aggregate-only policy",
            "match_count": len(rows),
        }],
    }
"""


def _looks_like_aggregate_policy(policy: str, question: str) -> bool:
    """Detect rooms whose policy explicitly permits aggregate statistics only."""
    text = f"{policy}\n{question}".lower()
    if not policy.strip():
        return False
    allows_aggregate = any(
        term in text
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
    blocks_raw = any(
        term in text
        for term in (
            "raw row",
            "raw rows",
            "row dumps",
            "individual",
            "identifiers",
            "urls",
            "titles",
            "descriptions",
            "system internals",
        )
    )
    return allows_aggregate and blocks_raw


def _extract_json_emit(text: str) -> dict | None:
    """Pull the last JSON object containing `scope_fn` from the agent's output."""
    if not text:
        return None
    # Try the trivial parse first (well-behaved final message).
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "scope_fn" in obj:
            return obj
    except json.JSONDecodeError:
        pass
    # Fall back to scanning for the last `{...}` containing scope_fn.
    matches = re.findall(r"\{[^{}]*\"scope_fn\"[^{}]*\}", text, flags=re.DOTALL)
    for cand in reversed(matches):
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict) and "scope_fn" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None


def main() -> None:
    if not QUERY_PROMPT.strip():
        # Fail closed with a maximally-aggregating scope_fn.
        print(json.dumps({"scope_fn": _FALLBACK_SCOPE_FN}))
        return

    if _looks_like_aggregate_policy(POLICY_CONTEXT, QUERY_PROMPT):
        print(json.dumps({"scope_fn": _AGGREGATE_POLICY_SCOPE_FN}))
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
        )
        response = agent.chat(body) or ""
    except Exception as e:
        print(f"AIAgent error: {e}", file=sys.stderr)

    parsed = _extract_json_emit(response)
    if parsed and isinstance(parsed.get("scope_fn"), str) and parsed["scope_fn"].strip():
        # Re-emit canonically so the pipeline parses cleanly.
        print(json.dumps({"scope_fn": parsed["scope_fn"]}))
        return

    # Fail closed: emit a max-aggregation scope_fn so the pipeline can
    # complete rather than HARD FAIL on bad JSON.
    print(
        f"scope agent produced no parseable JSON; using fallback. raw={response[:500]!r}",
        file=sys.stderr,
    )
    print(json.dumps({"scope_fn": _FALLBACK_SCOPE_FN}))


if __name__ == "__main__":
    main()

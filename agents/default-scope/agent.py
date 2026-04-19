"""Default scope agent — DIAGNOSTIC BUILD (2026-04-17).

SDK-based scope agent with explicit stderr capture to root-cause the
"Fatal error in message reader: Command failed with exit code 1" crash.

Key differences from the no-SDK rewrite:
  - Uses claude_agent_sdk.query() with MCP tools (Claude Code agent loop)
  - max_turns=6 (matches query agent; bounds the Node CLI exposure window)
  - stderr=captured_stderr.append on ClaudeAgentOptions (captures Node CLI output)
  - ALWAYS dumps captured stderr at end (success OR failure) — the base SDK
    hardcodes the error string "Check stderr output for details" without
    actually surfacing the Node CLI's stderr, so the crash looks opaque. This
    build forces it into our server log for diagnosis.

Revert to the no-SDK build via: cp agent_no_sdk.py.bak agent.py (and rebuild).

Env vars (set automatically by the sandbox):
  BRIDGE_URL, SESSION_TOKEN — bridge connection
  ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY — SDK routes LLM calls through bridge
  QUERY_PROMPT — the query to scope for
  QUERY_AGENT_ID — the query agent that will run

Output JSON to stdout:
  {"scope_fn": "def scope(sql, params, rows): ..."}
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, query, tool
from _bridge import (
    bridge_simulate,
    bridge_verify_scope_fn,
    create_hivemind_server,
)

QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
QUERY_AGENT_ID = os.environ.get("QUERY_AGENT_ID", "")
POLICY_CONTEXT = os.environ.get("POLICY_CONTEXT", "").strip()


# ── Scope-specific MCP tools ──
#
# The standard tools (get_schema, execute_sql) come from _bridge.py's
# create_hivemind_server(). We add verify_scope_fn only — a tight tool set
# so tool-count is not a confound in the diagnostic.


@tool(
    "verify_scope_fn",
    (
        "Compile + test a candidate scope_fn. Pass 'source' as the Python "
        "function text and 'tests' as a JSON string array of test cases. "
        "Each test case: {sql: str, params: list, rows: list[dict], "
        "expected_allow?: bool}. Returns {compiles, compile_error, "
        "all_tests_passed, results}. Fast (ms), no LLM call."
    ),
    {"source": str, "tests": str},
)
async def verify_tool(args: dict[str, Any]) -> dict[str, Any]:
    source = args.get("source", "")
    tests_raw = args.get("tests", "[]")
    if isinstance(tests_raw, str):
        try:
            tests = json.loads(tests_raw)
        except json.JSONDecodeError:
            tests = []
    elif isinstance(tests_raw, list):
        tests = tests_raw
    else:
        tests = []
    result = await bridge_verify_scope_fn(source, tests=tests)
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


@tool(
    "simulate_query",
    (
        "Play the query agent as an NPC: run it in a sandboxed simulation "
        "with a candidate scope_fn_source and see what final output it "
        "produces. Returns {'output': str, 'usage': dict, 'tape': [...]}. "
        "Uses the SAME question you were given unless 'prompt' is passed. "
        "Use this as a save/load test: try a scope_fn, see what the query "
        "agent actually says to the user, revise the scope_fn if the output "
        "leaks or is useless. Expensive (nested LLM run) — do it ONCE per "
        "candidate scope_fn, not per iteration."
    ),
    {"scope_fn_source": str, "prompt": str},
)
async def simulate_tool(args: dict[str, Any]) -> dict[str, Any]:
    scope_fn_source = args.get("scope_fn_source", "")
    if not scope_fn_source:
        return {"content": [{"type": "text",
                             "text": "Error: scope_fn_source is required"}]}
    prompt = args.get("prompt") or QUERY_PROMPT
    result = await bridge_simulate(QUERY_AGENT_ID, prompt, scope_fn_source)
    if result is None:
        return {"content": [{"type": "text",
                             "text": "Simulation failed or unavailable."}]}
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


SCOPE_TOOLS = [verify_tool, simulate_tool]
server = create_hivemind_server(extra_tools=SCOPE_TOOLS)


SYSTEM_PROMPT = """\
You are the ROW TRANSFORMER.

You receive three inputs describing a SQL query that the host has already
executed. You output a single Python function that transforms that query's
result rows into a privacy-safe shape before they reach the downstream
query agent.

The function signature is:
    def scope(sql, params, rows):

`rows` is the PROTAGONIST of your function. It is a list of dicts —
the actual data your transform will operate on. Read the dict keys from
get_schema. Decide which keys carry identifying content. Choose a
transformation: pass through, redact fields, collapse to an aggregate
(count / histogram / top-N bucket), or emit a single marker row.

`sql` and `params` are context only. The host has already run the query;
they cannot be changed. Do not gate on sql.lower() patterns — the privacy
boundary is at the ROWS, not at the SQL text.

# THE CONTRACT

The function you write MUST:
  1. Have signature EXACTLY `def scope(sql, params, rows):`. Three params.
  2. Return `{"allow": True, "rows": <transformed_list_of_dicts>}`.
  3. NEVER return `{"allow": False, ...}` — the host's AST validator will
     REJECT such functions and the query will HARD FAIL. There is no deny
     path. If a query's raw rows would leak, you MUST transform them into
     a safe shape, not block the query.
  4. Use ONLY these builtins: len, str, int, float, bool, list, dict, set,
     tuple, min, max, sum, sorted, any, all, abs, round, enumerate, zip,
     range, isinstance. Plus standard str/list/dict methods.
  5. Contain NO `import` statements.
  6. Contain NO exec, eval, open, __import__, no dunder attribute access.
  7. Contain NO class definitions.

# YOUR TOOLS

  mcp__hivemind__get_schema() — returns all user tables + columns. Use FIRST.
  mcp__hivemind__execute_sql(sql, params) — FULL_READ. Sample data to see
    what row shapes actually look like.
  mcp__hivemind__verify_scope_fn(source, tests) — FAST (ms). Compile + test
    candidate transform.
  mcp__hivemind__simulate_query(scope_fn_source, prompt) — SLOW (~60s),
    nested LLM run. Plays the query agent as an NPC with your candidate
    scope_fn and returns the output the USER would actually see. Use this
    as your save/load test before emitting your final JSON.

Claude Code built-in tools (Bash, Read, Write, Grep, Glob) are available.
No external network.

# THE NPC-SIMULATOR VIEW — save / load / revert

You are playing a security-review game. Your character:
  - CAN READ the NPC (query agent) source at `/workspace/query-agent/` —
    that directory is read-only mounted with the exact Python code and
    prompt the query agent will run under your scope_fn. Use Read/Grep/Glob
    to inspect it. You cannot modify these files; the filesystem is read-only.
  - CAN RUN the NPC with a candidate scope_fn via the MCP tool:
      `mcp__hivemind__simulate_query(scope_fn_source=..., prompt=...)`
    This hits a sandboxed /sandbox/simulate endpoint server-side, with
    tape recording, so you can replay / diff runs.
  - CAN REVERT at zero cost. Each simulate call is a fresh query-agent
    run with a clean slate — no state persists between simulations.
    Treat each as a save / load / retry cycle.
  - CANNOT WRITE to the NPC. The mount is RO; you cannot change the
    query agent's code or prompt. You can only change YOUR scope_fn.

Typical loop — use this when unsure what the query agent will do:

  1. Read /workspace/query-agent/agent.py to understand its workflow.
  2. get_schema + execute_sql to see the data shapes.
  3. Draft a candidate scope_fn, write it to /tmp/cand.py via Write.
  4. verify_scope_fn(source, tests) — compile-check.
  5. simulate_query — see what final output the user actually gets.
  6. If the output leaks or is useless, revise scope_fn, step 4 again.
  7. When output is SAFE + USEFUL, emit the final JSON.

Budget rule: simulate_query is expensive. Use it at MOST TWICE per
invocation. verify_scope_fn is cheap — use it freely.

# WORKFLOW — YOU HAVE 20 TURNS MAX, BUT EMIT BY TURN 10

Target: emit valid JSON by turn 6. Turns 7-20 are for debugging / fixing
a failing verify. If you reach turn 10 WITHOUT a final JSON emitted, you
MUST emit whatever scope_fn you have — an imperfect transformer that
ships is better than one that never does, because the system HARD FAILS
if you don't emit valid JSON.

Each turn is 20-40s wall clock.

  Turn 0 (free): READ THE POLICY from your user message if one is
          provided. The policy is authoritative — any defaults below must
          yield to an explicit policy constraint. If no policy is given,
          fall back to the general first-principles reasoning below.
  Turn 1: get_schema. Understand columns + identify identifying ones.
  Turn 2: sample a few rows via execute_sql to see real content shapes.
          If the policy mentions dates or windows, find the MAX date in
          your sample so you can compute cutoffs in your scope_fn.
  Turn 3: draft a transformer AND call verify_scope_fn with 3-5 tests in
          the SAME turn. Tests: `[{"sql": "...", "params": [], "rows": [...]}]`.
  Turn 4: if compiles + tests_passed → EMIT the final JSON.
  Turn 5-9: if verify failed — fix and re-verify.
  **Turn 10: HARD DEADLINE.** Whatever you have, emit the JSON now.
             The runtime will hard-fail the query if you don't emit.
             Do NOT spend more turns trying to improve. SHIP.
  Turn 11-20: only if the runtime gave you a correction message after
              an emit attempt failed to parse — emit again immediately.

BIAS TOWARD EMITTING EARLY. An over-aggregating transform that ships is
better than a perfect transform that times out.

# FIRST PRINCIPLES — privacy reasoning

  - Individuals vs aggregates. A row with someone's name identifies them.
    A COUNT over 1000 people does not.
  - Enumeration. Ten rows with a "title" column still identifies ten
    things. A single row with COUNT=10 is the aggregate with nothing
    identifying left.
  - Cross-referencing. Even "safe" columns (timestamps, categories) can
    re-identify when combined. Consider k-anonymity heuristically.
  - Principle of least information. Return the smallest shape that still
    answers the user's question. Prefer aggregation over raw rows.

# OUTPUT — HARD REQUIREMENT

Your FINAL MESSAGE must be a single JSON object on its own line:

  {"scope_fn": "def scope(sql, params, rows): ..."}

No prose before or after. No markdown fences. No "Here is". Just the JSON.

# TRANSFORMATION PATTERNS — the menu

Every scope_fn returns `{"allow": True, "rows": <something>}`. The
variation is in what `rows` becomes.

## Pattern A — pass through unchanged
When rows are ALREADY safe (COUNT/SUM/AVG result, GROUP BY on
non-identifying column, schema introspection).

    def scope(sql, params, rows):
        return {"allow": True, "rows": rows}

## Pattern B — per-row redaction of identifying fields
When the row shape is safe but specific fields (titles, content, emails,
handles) carry PII. Preserve structure, strip the dangerous values.

    def scope(sql, params, rows):
        sensitive = {"content", "title", "body", "message", "email", "phone"}
        out = []
        for r in rows:
            out.append({k: ("<redacted>" if k in sensitive else v)
                        for k, v in r.items()})
        return {"allow": True, "rows": out}

## Pattern C — collapse to aggregate
When rows contain individual records but the user's question can be
answered by counts, histograms, or top-N buckets. Strip individuals.

    def scope(sql, params, rows):
        return {"allow": True, "rows": [{"match_count": len(rows)}]}

Or by category:

    def scope(sql, params, rows):
        buckets = {}
        for r in rows:
            key = r.get("role") or "unknown"
            buckets[key] = buckets.get(key, 0) + 1
        return {"allow": True, "rows": [
            {"category": k, "count": v} for k, v in buckets.items()
        ]}

## Pattern D — marker row for extraction attempts
When the user is clearly trying to extract specific individuals (emails,
names, messages matching a regex). Return a single neutral row telling
the query agent that content is redacted by policy.

    def scope(sql, params, rows):
        return {"allow": True, "rows": [{
            "policy_note": "individual-record content redacted by policy",
            "match_count": len(rows),
        }]}

The query agent will write a graceful reply acknowledging the question,
noting the redaction, and reporting the count if safe to share.

## Pattern E — row filter (exclusion)
When the policy describes a ROW-LEVEL predicate — "only rows where X",
"within last N days", "not about topic Y", "from source Z only" — the
presence or absence of the whole row is the privacy signal, not which
fields it carries. Filter rows by the predicate and return only the
qualifying ones.

    def scope(sql, params, rows):
        def qualifies(r):
            # derive the predicate from POLICY_CONTEXT:
            #   date within allowed window,
            #   content not matching blocked topic terms,
            #   source in allowed set, etc.
            ...
            return True / False

        kept = [r for r in rows if qualifies(r)]
        if not kept:
            # don't leak "there are zero rows" either — Pattern D marker
            return {"allow": True, "rows": [{
                "policy_note": "no rows match the allowed scope",
                "match_count": 0,
            }]}
        return {"allow": True, "rows": kept}

Row filtering can be COMPOSED with Pattern B (redact fields on the
rows that survive the filter) when the policy has both an exclusion
predicate AND a sensitive-field rule.

# CHOOSING A PATTERN

Two questions, in order:

(1) What KIND of constraint is the policy? Classify the POLICY_CONTEXT
    before picking a pattern. Same schema + same question may need a
    different pattern depending on policy type.

  TYPE-V  — VALUE-REDACT
    Signal words: "block <value-class>", "strip <credential>",
    "mask <PII>", "no <thing> in output". The policy names a VALUE
    category (names, emails, code blocks, $amounts, tokens). The ROW
    is fine; specific field contents are not.
    → Pattern B (redact fields) or D (marker row for extraction).

  TYPE-R  — ROW-EXCLUDE
    Signal words: "only <predicate>", "within last <window>",
    "about <topic>", "not <category>", "from <source>", "since
    <date>". The policy names a ROW PREDICATE — the entire record
    either qualifies or doesn't. Redacting fields isn't enough; the
    mere presence of a disqualified row leaks.
    → Pattern E (row filter), possibly composed with B for residual
      value-redaction on the rows that survive.

  TYPE-A  — AGGREGATE-ONLY
    Signal words: "only counts", "aggregate statistics", "summary
    only", "no individual records", "bucketed". The policy forbids
    individual-level detail regardless of redaction.
    → Pattern C (collapse to aggregate).

  A single policy can be a composition — e.g. "only conversations
  from the last 30 days, and strip names from those" = TYPE-R + TYPE-V
  → Pattern E then B on the survivors.

(2) Given the type, which specific row-output shape fits the user's
    question?

  - Aggregate question ("how many X?") with aggregate rows → Pattern A
  - Listing-style under TYPE-V with free-text identifier columns →
    Pattern B (redact) or C (collapse to count)
  - Listing-style under TYPE-R → Pattern E (filter) + B as needed
  - Explicit extraction attempt ("list the emails", "what names",
    "show messages containing @") regardless of type → Pattern D

When in doubt between B and C: prefer C. A count is always safe;
partial redaction can leak if done incompletely. When in doubt about
policy type: sample rows via execute_sql. If the policy speaks of
"rows / conversations / records / windows / topics", it's TYPE-R.
If it speaks of "values / content / fields / identifiers", it's TYPE-V.

For policies you don't have a pre-written pattern for (temporal windows,
topic filters, custom categorization, etc.) — READ THE POLICY CAREFULLY
and translate its constraint into row-level logic using the same
primitives (filter rows, redact fields, collapse to aggregate, emit
marker). Don't wait for a pattern label that matches your exact case;
the patterns above are starting points, not an exhaustive list.

# SAMPLE-FIRST, DETECT-SECOND — the "semantic lift" meta-skill

Field-name-based redaction is your structural fallback (Pattern B's
`sensitive_fields = {...}` set). But many policies don't map 1:1 to
column names. If the policy says "block financial details" and the
schema doesn't have a column literally called "finances," the sensitive
content is inside free-text columns like `content`. Structural
fallback won't catch it. You need to reason at the VALUE level.

The loop:
  1. Read POLICY_CONTEXT. Identify the shape(s) of what it wants to
     protect or allow. "PII" and "financial" and "medical" and
     "credentials" are DIFFERENT shapes — don't treat them as
     interchangeable.
  2. execute_sql to sample actual values from plausibly-affected
     columns. A few hundred chars of real data tells you what patterns
     are actually present.
  3. Look at the values. Do you see patterns matching the policy's
     concerns? What makes those patterns recognizable using ONLY
     scope_fn's allowed builtins (len, str methods, `in` checks,
     slicing, isdigit()/isalpha(), character-class loops)? No imports,
     no regex.
  4. Write ONLY the helpers you need. If the policy doesn't mention
     medical and you saw no medical-looking values, DO NOT write a
     medical detector. If the policy says "block financial" and you
     saw dollar signs, "paid", "invoice #", write those specific
     detectors.
  5. Use the helpers inside whichever transform pattern (B redact /
     C aggregate / D marker) fits.

## Worked example — policy = "block financial details"

POLICY_CONTEXT: "Block any financial content: dollar amounts, bank
account references, payment discussions. Allow everything else."

execute_sql sampled message content and showed:
  "Paid $150 for the dentist last week."
  "My bank account is Chase ending in 4829."
  "Invoice #492 hasn't been paid yet."
  "The meeting ran long and I missed lunch."

Three of four are financial-shaped. Your scope_fn:

    def scope(sql, params, rows):
        def looks_financial(v):
            s = str(v).lower()
            if '$' in s:
                i = s.index('$')
                tail = s[i+1:i+6].replace(',', '').replace('.', '')
                if any(c.isdigit() for c in tail):
                    return True
            for term in ('paid', 'invoice', 'bank account', 'payment', 'usd'):
                if term in s:
                    return True
            return False

        out = []
        for r in rows:
            cleaned = {}
            for k, v in r.items():
                if isinstance(v, str) and looks_financial(v):
                    cleaned[k] = "<redacted: financial>"
                else:
                    cleaned[k] = v
            out.append(cleaned)
        return {"allow": True, "rows": out}

Notice: this scope_fn does NOT redact emails, names, phones, or
@handles. The policy didn't mention those. "email me at bob@ex.com"
passes through unchanged under this policy.

## Anti-pattern — do not build a universal PII library

A scope_fn that always contains:
    def looks_like_email(v): ...
    def looks_like_phone(v): ...
    def looks_like_handle(v): ...
regardless of policy is WRONG. It over-blocks when the policy is about
financials or medical or anything else. Your helpers must be derived
from (policy ∩ observed data), not from a boilerplate PII template.

The right approach is always:
  policy says X → sample data → find patterns matching X → detector for X
Not:
  scope_fn → include the standard PII detectors → done

A leak cannot be un-leaked. Aim for USEFUL + SAFE — the transform that
preserves the most ANSWER while leaking the least INDIVIDUAL-CONTENT.
"""


# Override with external prompt file if present (CLI-fused agents)
_PROMPT_FILE = Path("/app/prompt.md")
if _PROMPT_FILE.exists():
    SYSTEM_PROMPT = _PROMPT_FILE.read_text()


def _extract_scope_json(text: str) -> dict | None:
    """Extract a scope JSON object from LLM output.

    Returns the parsed dict if a {"scope_fn": "..."} object is found,
    or None if nothing parseable is present.
    """
    if not isinstance(text, str):
        return None
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "scope_fn" in parsed:
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 3 and lines[-1].strip() == "```":
            inner = "\n".join(lines[1:-1]).strip()
        else:
            inner = "\n".join(lines[1:]).strip()
        try:
            parsed = json.loads(inner)
            if isinstance(parsed, dict) and "scope_fn" in parsed:
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

    for i, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        for j in range(i, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            if depth == 0:
                candidate = text[i : j + 1]
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict) and "scope_fn" in parsed:
                        return parsed
                except (json.JSONDecodeError, ValueError):
                    pass
                break

    # Fallback: the model often wraps the scope_fn in a markdown code block
    # inside prose ("Here's the function: ```python def scope(...): ... ```").
    # Extract the first `def scope(...)` code block and wrap it ourselves.
    import re
    # Match a fenced code block whose body contains `def scope(`.
    fence_re = re.compile(
        r"```(?:python|py)?\s*\n(.*?def\s+scope\s*\(.*?)\n```",
        re.DOTALL,
    )
    m = fence_re.search(text)
    if m:
        return {"scope_fn": m.group(1).strip()}

    # Bare-source fallback: a `def scope(...)` line followed by an indented
    # body, no fence at all. Allow type annotations — the host's
    # compile_scope_fn will reject them, but emitting something parseable
    # lets downstream logging show why, vs silently dropping a valid-looking
    # function.
    bare_re = re.compile(
        r"(def\s+scope\s*\([^)]*\)\s*(?:->\s*[^:]+)?\s*:.+?)(?=\n\S|\Z)",
        re.DOTALL,
    )
    m = bare_re.search(text)
    if m:
        return {"scope_fn": m.group(1).strip()}

    return None


def _dump_cli_stderr(captured: list[str], outcome: str) -> None:
    """Force the Node CLI's stderr into our server log regardless of outcome.

    The SDK normally surfaces only the opaque message
    "Command failed with exit code 1 | Error output: Check stderr output
    for details" — which hides the actual Node CLI stderr. We always print
    captured lines so the host-side server log has the full context.
    """
    print(f"[scope-agent] outcome={outcome}", file=sys.stderr, flush=True)
    print(
        f"[scope-agent] captured_stderr_lines={len(captured)}",
        file=sys.stderr,
        flush=True,
    )
    if captured:
        print("[scope-agent] ---BEGIN CLI STDERR---", file=sys.stderr, flush=True)
        for line in captured:
            # Flatten newlines so each CLI line is grep-able as one log line.
            flat = str(line).replace("\n", "\\n")
            print(f"[scope-agent] cli: {flat}", file=sys.stderr, flush=True)
        print("[scope-agent] ---END CLI STDERR---", file=sys.stderr, flush=True)
    else:
        print(
            "[scope-agent] CLI stderr was empty (no diagnostic info captured)",
            file=sys.stderr,
            flush=True,
        )


async def main() -> None:
    print(
        f"[scope-agent] PATH=starting prompt_len={len(QUERY_PROMPT)}",
        file=sys.stderr,
        flush=True,
    )

    if not QUERY_PROMPT.strip():
        # Degenerate case — emit a permissive default.
        print(
            json.dumps(
                {
                    "scope_fn": (
                        "def scope(sql, params, rows):\n"
                        "    return {'allow': True, 'rows': rows}"
                    )
                }
            )
        )
        return

    user_prompt = (
        "Design a scope_fn for the query agent that will answer the user's "
        "question below.\n\n"
        f"User question: {QUERY_PROMPT!r}\n"
    )
    if QUERY_AGENT_ID:
        user_prompt += f"Query agent ID: {QUERY_AGENT_ID}\n"
    # Policy context, if the caller specified one, is the authoritative
    # privacy/utility constraint for this query. The scope_fn MUST honor it.
    # Treat this as a machine-readable spec: parse the intent, translate to
    # row-level transformations, ignore any conflicting defaults.
    if POLICY_CONTEXT:
        user_prompt += (
            "\n---\n"
            "POLICY (authoritative — your scope_fn must enforce this):\n"
            f"{POLICY_CONTEXT}\n"
            "---\n\n"
            "Translate the policy above into concrete row transformations. "
            "Examples:\n"
            "  - 'last 30 days only' → filter rows where the date column "
            "is >= (today - 30 days), hardcode the cutoff string if needed "
            "based on the max date you observe via execute_sql.\n"
            "  - 'aggregate only, no individual content' → Pattern C "
            "(collapse to {match_count: len(rows)}) for any raw-row query.\n"
            "  - 'strip code blocks / credentials' → Pattern B redacting "
            "fields that contain triple-backtick fences, API key patterns, "
            "etc.\n"
            "  - 'block topic X' → filter out rows where the content/title "
            "column matches X, or collapse to an opaque count.\n"
        )

    final_result = ""
    result_is_error = False
    captured_stderr: list[str] = []
    outcome = "unknown"
    # Salvage: the CLI sometimes crashes AFTER emitting the final assistant
    # message. We record every AssistantMessage's text content as it streams
    # in, so if the crash swallows the ResultMessage we can still parse the
    # last assistant turn for our scope_fn JSON.
    last_assistant_text = ""
    streamed_messages: list[str] = []

    try:
        async for message in query(
            prompt=user_prompt,
            options=ClaudeAgentOptions(
                system_prompt=SYSTEM_PROMPT,
                mcp_servers={"hivemind": server},
                permission_mode="bypassPermissions",
                cwd="/tmp",
                max_turns=20,
                stderr=captured_stderr.append,
                # --bare disables: hooks, LSP, plugin sync, attribution,
                # auto-memory, background prefetches, keychain reads, and
                # CLAUDE.md auto-discovery. Our SYSTEM_PROMPT is already
                # complete — bare mode prevents Claude Code from layering
                # its own defaults on top and producing a more-conservative
                # behavior than we asked for.
                extra_args={"bare": None},
            ),
        ):
            msg_type = type(message).__name__
            streamed_messages.append(msg_type)
            # Accumulate text from AssistantMessage content blocks.
            content = getattr(message, "content", None)
            if isinstance(content, list):
                for block in content:
                    block_type = type(block).__name__
                    text = getattr(block, "text", None)
                    if isinstance(text, str) and text.strip():
                        last_assistant_text = text
                    # Telemetry: log the tool NAME (not just "ToolUseBlock")
                    # so we can see which superpowers scope actually invokes.
                    # ToolUseBlock has .name; ToolResultBlock has .content;
                    # TextBlock has .text.
                    tool_name = getattr(block, "name", None)
                    if block_type == "ToolUseBlock" and tool_name:
                        # Capture an argument summary without dumping full
                        # payloads (could be large for scope_fn sources).
                        tool_input = getattr(block, "input", {}) or {}
                        arg_keys = ",".join(sorted(tool_input.keys())[:5])
                        arg_size = len(str(tool_input))
                        print(
                            f"[scope-agent] TOOL_USE name={tool_name!r} "
                            f"arg_keys=[{arg_keys}] arg_size={arg_size}",
                            file=sys.stderr,
                            flush=True,
                        )
                        streamed_messages[-1] += f"({block_type}:{tool_name})"
                    else:
                        streamed_messages[-1] += f"({block_type})"
            if hasattr(message, "result"):
                final_result = message.result
                result_is_error = bool(getattr(message, "is_error", False))
        outcome = "sdk-completed"
    except Exception as exc:
        outcome = f"sdk-crashed:{type(exc).__name__}:{exc}"
        print(
            f"[scope-agent] SDK exception: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )

    # CRITICAL DIAGNOSTIC: always dump captured CLI stderr.
    _dump_cli_stderr(captured_stderr, outcome)

    # Log the stream of messages we actually saw, so we can tell at what
    # point the CLI died (e.g. did we even get an AssistantMessage?).
    print(
        f"[scope-agent] streamed_messages ({len(streamed_messages)}): "
        f"{' -> '.join(streamed_messages[:20])}",
        file=sys.stderr,
        flush=True,
    )
    if last_assistant_text:
        preview = last_assistant_text[:400].replace("\n", "\\n")
        print(
            f"[scope-agent] last_assistant_text len={len(last_assistant_text)} "
            f"preview={preview!r}",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            "[scope-agent] last_assistant_text was empty",
            file=sys.stderr,
            flush=True,
        )

    # Even if SDK crashed, we may have a partial final_result from an
    # earlier ResultMessage OR a captured last_assistant_text. Try both.
    parsed = _extract_scope_json(final_result) if final_result else None
    if parsed is None and last_assistant_text:
        parsed = _extract_scope_json(last_assistant_text)
        if parsed is not None:
            print(
                "[scope-agent] PATH=salvage (parsed last_assistant_text after crash)",
                file=sys.stderr,
                flush=True,
            )
    if parsed is not None and not result_is_error:
        # Dump the FULL scope_fn source so we can audit runtime behavior
        # against its docstring claims — docstrings have said "allow..."
        # while the body actually rejects.
        full_src = str(parsed.get("scope_fn", ""))
        flat_src = full_src.replace("\n", "\\n")
        print(
            f"[scope-agent] PATH=success scope_fn_full len={len(full_src)} src={flat_src}",
            file=sys.stderr,
            flush=True,
        )
        print(json.dumps(parsed))
        return

    # Emit-failure. Use a DENY-ALL scope_fn so the query agent surfaces
    # a clear failure instead of silently returning whatever it likes.
    reason = outcome if not parsed else "parsed JSON but result_is_error=True"
    print(
        f"[scope-agent] PATH=emit-failure REASON={reason[:200]}",
        file=sys.stderr,
        flush=True,
    )
    print(
        json.dumps(
            {
                "scope_fn": (
                    # Never-deny compliant fallback: when the scope agent
                    # itself fails to produce a scope_fn, emit a safe
                    # transformer that collapses any rows to a neutral
                    # marker. The host's AST validator rejects allow=False,
                    # so this is the only legal fallback shape.
                    "def scope(sql, params, rows):\n"
                    "    return {'allow': True, "
                    "'rows': [{"
                    "'policy_note': 'scope agent failed — content "
                    "redacted by safe fallback', "
                    "'match_count': len(rows)"
                    "}]}"
                )
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())

"""Default scope agent — Hermes harness.

Same role as agents/default-scope/agent.py: emit a `scope_fn` that
transforms the query agent's rows before they reach the user, given a
question + an optional room policy. Emits a single JSON object
`{"scope_fn": "..."}` on the final line of stdout.

Uses the sandbox bridge's OpenAI-compatible endpoint directly so the harness
can keep scope design bounded. The scope agent may inspect schema or a small
sample when useful, but the query agent does the research.

Env (set automatically by the sandbox runner):
  BRIDGE_URL, SESSION_TOKEN  — bridge connection
  HIVEMIND_AGENT_ROLE=scope  — plugin registers verify/simulate tools
  HIVEMIND_MODEL             — model id passed to the bridge LLM endpoint
  QUERY_PROMPT               — the user's question
  QUERY_AGENT_ID             — the query agent simulate_* will run
  POLICY_CONTEXT             — optional room policy to enforce
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

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
import hivemind  # noqa: E402, F401

QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
QUERY_AGENT_ID = os.environ.get("QUERY_AGENT_ID", "")
POLICY_CONTEXT = os.environ.get("POLICY_CONTEXT", "").strip()
HIVEMIND_MODEL = os.environ.get("HIVEMIND_MODEL", "moonshotai/kimi-k2.6")

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

Default tools:
- get_schema(): inspect tables, columns, and types.
- execute_sql(sql, params): sample or compute facts needed for the policy.
The harness compiles and verifies the exact scope_fn you emit. Expensive
downstream simulation and query-agent source inspection are advanced tools
that may be enabled by deployment policy, but they are not part of the default
fast path.

Process:
1. Read the policy and question.
2. Use get_schema and small execute_sql checks only when they help understand
   the data shape or policy boundary.
3. Draft the least destructive compliant transform: pass through, filter rows,
   drop or replace fields, derive safer fields, summarize, or return no rows.
4. Emit that transform as final JSON. Do not keep comparing alternatives unless
   the candidate clearly violates the policy.
Stay in your lane: scope designs the privacy transform. The query agent will
do the research. Do not spend turns researching trends, lifecycles, categories,
or final report evidence. If no explicit restrictive policy is present, preserve
useful scoped result rows by default and redact only obvious raw identifiers,
URLs, secrets, or fields whose disclosure would be needlessly invasive for the
question. If policy allows row-level records, do not collapse them into counts.
If policy allows summaries, preserve grouping fields and metric fields. Return
an empty list only when no policy-compliant useful disclosure remains. The
default path is a short decision loop, not a research phase.

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


_NO_REASONING_OVERRIDES = {"extra_body": {"reasoning": {"effort": "none", "exclude": True}}}
_HTTP_TIMEOUT = httpx.Timeout(120.0)
_MAX_TOOL_RESULT_CHARS = 12_000
_MAX_RETRY_CONTEXT_CHARS = 3000
_NO_POLICY_RECOVERY_SCOPE_FN = (
    "def scope(sql, params, rows):\n"
    "    redacted_keys = ('email', 'phone', 'address', 'url', 'link', 'token', 'secret', 'password', 'api_key', 'cookie', 'session', 'auth')\n"
    "    safe_rows = []\n"
    "    for row in rows:\n"
    "        if not isinstance(row, dict):\n"
    "            continue\n"
    "        out = {}\n"
    "        for key, value in row.items():\n"
    "            name = str(key).lower()\n"
    "            redact = False\n"
    "            for marker in redacted_keys:\n"
    "                if marker in name:\n"
    "                    redact = True\n"
    "            if redact:\n"
    "                out[key] = '[redacted]'\n"
    "            else:\n"
    "                out[key] = value\n"
    "        safe_rows.append(out)\n"
    "    return {'allow': True, 'rows': safe_rows}\n"
)
_UTILITY_VERIFY_TESTS: list[dict[str, Any]] = [
    {
        "label": "benign labeled metric rows survive",
        "sql": "SELECT label, value FROM source_rows ORDER BY value DESC",
        "params": [],
        "rows": [
            {"label": "alpha", "value": 42},
            {"label": "beta", "value": 17},
        ],
        "expect_allow": True,
        "expect_min_rows": 2,
        "expect_rows": [
            {"label": "alpha", "value": 42},
            {"label": "beta", "value": 17},
        ],
    },
    {
        "label": "benign record fields survive",
        "sql": "SELECT name, score, note FROM source_rows",
        "params": [],
        "rows": [{"name": "alpha", "score": 9, "note": "summary"}],
        "expect_allow": True,
        "expect_min_rows": 1,
        "expect_rows": [{"name": "alpha", "score": 9, "note": "summary"}],
    },
]
_SCOPE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": (
                "Get the database schema: table names, column names, types, "
                "and defaults. Use only when schema helps design the scope."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_sql",
            "description": (
                "Execute a small read-only PostgreSQL shape check or sample "
                "needed to design the privacy transform. Avoid research queries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string"},
                    "params": {
                        "type": "array",
                        "items": {},
                        "default": [],
                    },
                },
                "required": ["sql"],
            },
        },
    },
]
_ALLOWED_TOOL_NAMES = {"get_schema", "execute_sql"}


def _verification_tests() -> list[dict]:
    """Use generic utility smoke tests, never benchmark/prompt-keyword fixtures."""
    if os.environ.get("HIVEMIND_SCOPE_VERIFY_USEFUL_ROWS", "true").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return []
    return _UTILITY_VERIFY_TESTS


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


def _budget_max_calls(default: int = 12) -> int:
    try:
        return max(1, int(os.environ.get("BUDGET_MAX_CALLS", str(default))))
    except ValueError:
        return default


def _max_tool_turns() -> int:
    if raw := os.environ.get("HIVEMIND_SCOPE_MAX_TOOL_TURNS"):
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return max(0, min(2, _budget_max_calls() - 1))


def _bridge_url() -> str:
    return os.environ["BRIDGE_URL"].rstrip("/")


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['SESSION_TOKEN']}"}


def _post_bridge(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = httpx.post(
        f"{_bridge_url()}{path}",
        json=payload,
        headers=_auth_headers(),
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"bridge {path} returned {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def _chat_completion(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
) -> tuple[dict[str, Any], str]:
    payload: dict[str, Any] = {
        "model": HIVEMIND_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "extra_body": _NO_REASONING_OVERRIDES["extra_body"],
    }
    if tools is not None:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    data = _post_bridge("/v1/chat/completions", payload)
    choices = data.get("choices") or []
    if not choices:
        return {"role": "assistant", "content": ""}, "unknown"
    choice = choices[0]
    return choice.get("message") or {}, str(choice.get("finish_reason") or "unknown")


def _parse_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _compact_tool_result(result: str) -> str:
    text = result if isinstance(result, str) else str(result)
    if len(text) <= _MAX_TOOL_RESULT_CHARS:
        return text
    return (
        text[:_MAX_TOOL_RESULT_CHARS]
        + f"\n[tool result truncated to {_MAX_TOOL_RESULT_CHARS} chars by scope harness]"
    )


def _call_scope_tool(name: str, args: dict[str, Any]) -> str:
    if name not in _ALLOWED_TOOL_NAMES:
        return (
            f"Error: unknown scope tool {name!r}. "
            f"Available: {', '.join(sorted(_ALLOWED_TOOL_NAMES))}"
        )
    payload_args: dict[str, Any] = {}
    if name == "execute_sql":
        payload_args["sql"] = str(args.get("sql") or "")
        params = args.get("params", [])
        payload_args["params"] = params if isinstance(params, list) else []
    data = _post_bridge(f"/tools/{name}", {"arguments": payload_args})
    if data.get("error"):
        return f"Error: {data['error']}"
    return _compact_tool_result(data.get("result") or "")


def _assistant_message_for_history(message: dict[str, Any]) -> dict[str, Any]:
    keep: dict[str, Any] = {
        "role": "assistant",
        "content": message.get("content") or "",
    }
    if message.get("tool_calls"):
        keep["tool_calls"] = message["tool_calls"]
    return keep


def _finalization_instruction(reason: str) -> str:
    return (
        f"FINALIZATION INSTRUCTION ({reason}): stop using tools. Emit exactly "
        'one single-line JSON object: {"scope_fn": "def scope(sql, params, rows):\\n'
        "    ...\"}. The function must enforce the policy, preserve every useful "
        "field the policy allows, and avoid canned assumptions about this dataset."
    )


def _run_scope_agent(body: str) -> str:
    system_prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        "Harness behavior: you may use get_schema and execute_sql for scope "
        "design only. The harness verifies the emitted source after you answer. "
        "Keep the function compact and general; do not research the answer."
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": body},
    ]
    tool_turns = _max_tool_turns()
    per_turn_tokens = _completion_token_cap(default=2048, hard_cap=4096)
    final_tokens = _completion_token_cap(default=2048, hard_cap=4096)
    finalization_reason = (
        "scope tool budget reached" if tool_turns else "no scope tools available"
    )

    for turn_idx in range(tool_turns):
        message, _finish_reason = _chat_completion(
            messages,
            tools=_SCOPE_TOOLS,
            max_tokens=per_turn_tokens,
        )
        tool_calls = message.get("tool_calls") or []
        content = (message.get("content") or "").strip()
        if not tool_calls:
            return content
        messages.append(_assistant_message_for_history(message))
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            args = _parse_tool_args(fn.get("arguments"))
            call_id = str(call.get("id") or f"call_{turn_idx}_{name}")
            try:
                result = _call_scope_tool(name, args)
            except Exception as e:
                result = f"Error: {type(e).__name__}: {e}"
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": result,
                }
            )

    messages.append(
        {
            "role": "user",
            "content": _finalization_instruction(finalization_reason),
        }
    )
    message, _finish_reason = _chat_completion(
        messages,
        tools=None,
        max_tokens=final_tokens,
    )
    return (message.get("content") or "").strip()


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
        retry_response = _run_scope_agent(_retry_body(body, reason, previous_response))
    except Exception as e:
        print(f"scope harness retry error after {reason}: {e}", file=sys.stderr)
        return None
    parsed = _extract_json_emit(retry_response)
    if parsed and isinstance(parsed.get("scope_fn"), str) and parsed["scope_fn"].strip():
        return parsed
    print(
        f"scope harness retry produced no parseable JSON after {reason}. "
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
            json={"source": source, "tests": _verification_tests()},
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
    return True, "ok"


def _emit_verified_scope(source: str) -> tuple[bool, str]:
    verified, reason = _verify_scope_source(source)
    if verified:
        # Re-emit canonically so the pipeline parses cleanly.
        print(json.dumps({"scope_fn": source}))
        return True, "ok"
    print(f"scope self-verify failed: {reason}", file=sys.stderr)
    return False, reason


def _emit_no_policy_recovery_scope(reason: str) -> bool:
    """Last-resort recovery for model authoring errors when no policy exists."""
    if POLICY_CONTEXT:
        return False
    print(
        f"scope no-policy recovery activated after verifier rejection: {reason}",
        file=sys.stderr,
    )
    verified, recovery_reason = _emit_verified_scope(_NO_POLICY_RECOVERY_SCOPE_FN)
    if verified:
        return True
    print(
        f"scope no-policy recovery failed self verification: {recovery_reason}",
        file=sys.stderr,
    )
    return False


def _fail_scope(reason: str, previous_response: str = "") -> None:
    preview = (previous_response or "").strip()[:500]
    if preview:
        print(f"scope agent failed: {reason}. raw={preview!r}", file=sys.stderr)
    else:
        print(f"scope agent failed: {reason}", file=sys.stderr)
    raise SystemExit(2)


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
        _fail_scope("missing query prompt")

    parts: list[str] = []
    if POLICY_CONTEXT:
        parts.append(f"POLICY:\n{POLICY_CONTEXT}")
    parts.append(f"QUESTION:\n{QUERY_PROMPT}")
    body = "\n\n".join(parts)

    response = ""
    try:
        response = _run_scope_agent(body)
    except Exception as e:
        print(f"scope harness error: {e}", file=sys.stderr)

    parsed = _extract_json_emit(response)
    if parsed and isinstance(parsed.get("scope_fn"), str) and parsed["scope_fn"].strip():
        verified, verify_reason = _emit_verified_scope(parsed["scope_fn"])
        if verified:
            return
        retry = _retry_scope_emit(
            body,
            reason=f"scope_fn failed self verification: {verify_reason}",
            previous_response=response,
        )
        if retry:
            retry_verified, retry_reason = _emit_verified_scope(retry["scope_fn"])
            if retry_verified:
                return
            if _emit_no_policy_recovery_scope(retry_reason):
                return
            _fail_scope(
                f"scope_fn failed self verification after retry: {retry_reason}",
                response,
            )
        if _emit_no_policy_recovery_scope(verify_reason):
            return
        _fail_scope("scope_fn failed self verification", response)

    retry = _retry_scope_emit(
        body,
        reason="unparseable or truncated scope JSON",
        previous_response=response,
    )
    if retry:
        retry_verified, retry_reason = _emit_verified_scope(retry["scope_fn"])
        if retry_verified:
            return
        if _emit_no_policy_recovery_scope(retry_reason):
            return
        _fail_scope(
            f"retry scope_fn failed self verification: {retry_reason}",
            response,
        )
    if _emit_no_policy_recovery_scope("unparseable or truncated scope JSON"):
        return
    _fail_scope("unparseable or unverifiable scope JSON", response)


if __name__ == "__main__":
    main()

"""Default query agent — Hermes harness.

Same role as agents/default-query/agent.py but driven by Hermes' Python
`AIAgent` API instead of the Claude Agent SDK / Claude Code CLI.

We deliberately use AIAgent in-process rather than `hermes -z` subprocess:
the oneshot CLI does not expose --max-turns or a --system-prompt flag,
so we'd lose two knobs the role needs. From the sandbox's perspective
the container CMD is still a single Python process.

Env vars (set automatically by the sandbox runner):
  BRIDGE_URL, SESSION_TOKEN  — bridge connection
  HIVEMIND_AGENT_ROLE=query  — tells the hivemind plugin which tools to
                               register (hides scope-only tools entirely)
  HIVEMIND_MODEL             — e.g. openai/gpt-4o, anthropic/claude-sonnet-4
                               Default falls back to a sensible model.
  QUERY_PROMPT               — the user's question
  QUERY_CONTEXT              — optional additional context
  SCOPE_FN_SOURCE            — the privacy filter source (prepended as spec)

Outputs answer text to stdout.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

# Trigger plugin registration BEFORE importing AIAgent. Hermes' bundled
# plugin discovery runs lazily and doesn't always fire in library mode,
# so we import the plugin package explicitly off HERMES_BUNDLED_PLUGINS.
_PLUGINS_DIR = os.environ.get("HERMES_BUNDLED_PLUGINS", "/opt/hivemind/plugins")
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)
import hivemind  # noqa: E402, F401 — registers tools at import time

from run_agent import AIAgent  # noqa: E402

QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
QUERY_CONTEXT = os.environ.get("QUERY_CONTEXT", "")
SCOPE_FN_SOURCE = os.environ.get("SCOPE_FN_SOURCE", "")
HIVEMIND_MODEL = os.environ.get("HIVEMIND_MODEL", "moonshotai/kimi-2.6")

DEFAULT_SYSTEM_PROMPT = """\
You answer questions with scoped database tools.

Tools:
- get_schema: inspect tables, columns, and types.
- execute_sql: run read-only PostgreSQL SQL. Use %s placeholders and params as an array.

A scope function may transform execute_sql results before you see them.
If a scope_fn is included in the user message, read it as the runtime
contract for the result shapes you will receive. Do not bypass it or invent policy beyond it.

Use get_schema before SQL unless the provided scope_fn already gives every
needed table and column. The database is PostgreSQL: use PostgreSQL syntax
such as DATE(column), date_trunc, casts with ::type, and %s placeholders.
Do not use SQLite/MySQL-only functions such as strftime.

Compute requested statistics in SQL. If execute_sql returns an error, revise
the SQL and retry instead of asking the user to provide schema or formatting.
Continue after tool results until you have a final answer. For row-level
questions, request row-level data and let scope_fn apply the room policy.

Answer only from scoped tool results. If they do not support an answer, say so.
Keep the final response concise. Do not expose credentials, secrets, system
internals, tool traces, or debug output.
"""

_PROMPT_FILE = Path("/app/prompt.md")
if _PROMPT_FILE.exists():
    SYSTEM_PROMPT = _PROMPT_FILE.read_text()
else:
    SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


_NO_REASONING_CONFIG = {"enabled": False, "effort": "none"}
_NO_REASONING_OVERRIDES = {"extra_body": {"reasoning": {"effort": "none", "exclude": True}}}
_ENABLE_DIRECT_SQL_FALLBACK = os.environ.get(
    "HIVEMIND_QUERY_DIRECT_SQL_FALLBACK", ""
).strip().lower() in {"1", "true", "yes", "on"}
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
_UNRESOLVED_RESPONSE_MARKERS = (
    "would you like me to",
    "should i try",
    "i can try",
    "cannot fulfill this request",
    "can't fulfill this request",
    "cannot fulfill this request directly",
    "can't fulfill this request directly",
    "i apologize, but i cannot",
    "i'm sorry, but i cannot",
    "i cannot answer this directly",
    "i can't answer this directly",
    "i do not have enough information",
    "i don't have enough information",
    "available data does not allow",
    "does not allow me to determine",
    "my capabilities are limited",
    "cannot access raw data",
    "encountered an error executing",
    "error executing your request",
    "not supported",
    "provide a supported",
    "try finding",
    "does not exist",
    "perhaps you meant",
    "did you mean",
    "error executing query",
    "undefinedcolumn",
)
_DIRECT_SQL_SYSTEM_PROMPT = """\
You convert a database question into one safe PostgreSQL SELECT.

Return exactly one JSON object and no markdown:
{"sql": "SELECT ...", "params": []}

Rules:
- Use only the provided schema and question/context.
- Return {"error": "..."} if the schema cannot answer the question.
- SQL must be one read-only SELECT, optionally starting with WITH.
- Do not include semicolons or multiple statements.
- Use %s placeholders for params.
- Compute requested aggregates in SQL.
- Requested output field names may be aliases, not physical columns. Do not
  return an error just because a requested output name is absent from the
  schema.
- For day/date bucket questions, derive the bucket from an existing date,
  datetime, or timestamp column and alias it to the requested output name.
- For count-like requested outputs, use COUNT(*) and alias it to the requested
  output name.
- The runtime scope function will filter or transform rows after execution;
  do not invent additional privacy policy in this planner.
"""
_DIRECT_SQL_REPAIR_PROMPT = """\
Your previous response was not usable.

If the error was about a requested output name missing from the schema, treat
that name as a SQL alias instead of a required physical column. Derive date/day
buckets from existing date, datetime, or timestamp columns when the question
asks for a day/date. Use COUNT(*) for count-like aggregate outputs. Return a
safe SELECT JSON object, or return {{"error": "..."}} only if the available
physical columns cannot support the computation.

Previous error:
{error}
"""
_MAX_PLANNER_CONTEXT_CHARS = 60_000
_SELECT_START_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE | re.DOTALL)
_MUTATING_SQL_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|do)\b",
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOP_COUNT_TERMS = (
    "highest",
    "largest",
    "biggest",
    "maximum",
    "max",
    "most",
    "top",
)
_COUNT_TERMS = ("count", "counts", "number of", "how many", "total")
_DATE_BUCKET_TERMS = ("day", "date", "daily", "per day", "by day")


def _looks_like_runtime_failure(text: str) -> bool:
    lower = (text or "").lower()
    return any(marker in lower for marker in _HERMES_FAILURE_MARKERS)


def _looks_like_unresolved_response(text: str) -> bool:
    lower = (text or "").lower()
    return any(marker in lower for marker in _UNRESOLVED_RESPONSE_MARKERS)


def _user_facing_fallback() -> str:
    q_trim = (QUERY_PROMPT or "your question").strip().rstrip("?.! ")
    return (
        f"For your question about {q_trim!r}, I wasn't able to produce "
        "an answer from the scoped results available under the current "
        "room policy. Try a narrower question or update the room policy "
        "if this access should be allowed."
    )


def _clip_context(text: str, max_chars: int = _MAX_PLANNER_CONTEXT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[truncated]"


def _post_json(path: str, payload: dict[str, Any], *, timeout: float = 90.0) -> dict[str, Any]:
    base_url = os.environ["BRIDGE_URL"].rstrip("/")
    req = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['SESSION_TOKEN']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"bridge HTTP {e.code}: {body[:500]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"bridge request failed: {e}") from e
    return json.loads(raw or "{}")


def _call_tool(tool_name: str, arguments: dict[str, Any]) -> str:
    payload = _post_json(
        f"/tools/{tool_name}",
        {"arguments": arguments},
        timeout=60.0,
    )
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    return str(payload.get("result") or "")


def _llm_chat(messages: list[dict[str, str]], *, max_tokens: int = 512) -> str:
    payload = _post_json(
        "/v1/chat/completions",
        {
            "model": HIVEMIND_MODEL,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
            "extra_body": {"reasoning": {"effort": "none", "exclude": True}},
            "reasoning": {"effort": "none", "exclude": True},
        },
        timeout=120.0,
    )
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


def _extract_json_object(text: str) -> dict[str, Any]:
    candidate = (text or "").strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(candidate[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("planner did not return a JSON object")
    return parsed


def _normalize_select_sql(sql: str) -> str:
    normalized = (sql or "").strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].strip()
    if ";" in normalized:
        raise ValueError("planner returned multiple SQL statements")
    if not _SELECT_START_RE.match(normalized):
        raise ValueError("planner returned non-SELECT SQL")
    if _MUTATING_SQL_RE.search(normalized):
        raise ValueError("planner returned mutating SQL")
    return normalized


def _quote_ident(name: str) -> str:
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"unsafe SQL identifier {name!r}")
    return f'"{name}"'


def _identifier_mentioned(name: str, text: str) -> bool:
    lower = name.lower()
    return lower in text or lower.replace("_", " ") in text


def _schema_rows(schema: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(schema)
    except json.JSONDecodeError:
        return []

    if isinstance(parsed, dict):
        if isinstance(parsed.get("columns"), list):
            parsed = parsed["columns"]
        elif isinstance(parsed.get("schema"), list):
            parsed = parsed["schema"]
        elif isinstance(parsed.get("tables"), list):
            rows: list[dict[str, Any]] = []
            for table in parsed["tables"]:
                if not isinstance(table, dict):
                    continue
                table_name = table.get("table_name") or table.get("name")
                columns = table.get("columns") or []
                if not isinstance(columns, list):
                    continue
                for column in columns:
                    if isinstance(column, dict):
                        row = dict(column)
                        row.setdefault("table_name", table_name)
                        rows.append(row)
                    elif isinstance(column, str):
                        rows.append({"table_name": table_name, "column_name": column})
            return rows
        else:
            return []

    if not isinstance(parsed, list):
        return []
    return [row for row in parsed if isinstance(row, dict)]


def _safe_aliases_from_question(body: str) -> tuple[str, str]:
    aliases: list[str] = []
    match = re.search(
        r"\breturn\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:,|and)\s*([A-Za-z_][A-Za-z0-9_]*)",
        body,
        re.IGNORECASE,
    )
    if match:
        aliases = [match.group(1).lower(), match.group(2).lower()]

    date_alias = "day"
    count_alias = "count"
    for alias in aliases:
        if not _IDENTIFIER_RE.match(alias):
            continue
        if any(term in alias for term in ("day", "date")):
            date_alias = alias
        else:
            count_alias = alias
    return date_alias, count_alias


def _try_top_date_count_plan(body: str, schema: str) -> dict[str, Any] | None:
    lower = body.lower()
    if not all(
        any(term in lower for term in terms)
        for terms in (_TOP_COUNT_TERMS, _COUNT_TERMS, _DATE_BUCKET_TERMS)
    ):
        return None

    rows = _schema_rows(schema)
    tables: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        table = str(row.get("table_name") or row.get("table") or "").strip()
        column = str(row.get("column_name") or row.get("name") or "").strip()
        if not table or not column:
            continue
        if not (_IDENTIFIER_RE.match(table) and _IDENTIFIER_RE.match(column)):
            continue
        tables.setdefault(table, []).append(row)

    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for table, columns in tables.items():
        table_mentioned = _identifier_mentioned(table, lower)
        if not table_mentioned and len(tables) > 1:
            continue
        for column in columns:
            column_name = str(column.get("column_name") or column.get("name") or "")
            data_type = str(column.get("data_type") or column.get("type") or "").lower()
            column_lower = column_name.lower()
            date_like = (
                "timestamp" in data_type
                or "date" in data_type
                or "time" in data_type
                or "date" in column_lower
                or "day" in column_lower
                or column_lower.endswith("_at")
            )
            if not date_like:
                continue

            score = 0
            if table_mentioned:
                score += 8
            if _identifier_mentioned(column_name, lower):
                score += 5
            if "timestamp" in data_type or "datetime" in data_type:
                score += 4
            elif "date" in data_type:
                score += 3
            if column_lower.endswith("_at"):
                score += 2
            if "date" in column_lower or "day" in column_lower:
                score += 1
            candidates.append((score, table, column))

    if not candidates:
        return None

    _score, table, column = max(candidates, key=lambda item: item[0])
    column_name = str(column.get("column_name") or column.get("name") or "")
    date_alias, count_alias = _safe_aliases_from_question(body)
    sql = (
        f"SELECT DATE({_quote_ident(column_name)}) AS {_quote_ident(date_alias)}, "
        f"COUNT(*)::bigint AS {_quote_ident(count_alias)} "
        f"FROM {_quote_ident(table)} GROUP BY 1 ORDER BY 2 DESC LIMIT 1"
    )
    return {"sql": sql, "params": []}


def _format_scoped_sql_result(result: str) -> str:
    parsed = json.loads(result)
    if isinstance(parsed, dict) and parsed.get("error"):
        raise RuntimeError(str(parsed["error"]))
    return json.dumps(parsed, ensure_ascii=False, default=str)


def _build_sql_planner_messages(body: str, schema: str) -> list[dict[str, str]]:
    user = (
        "SCHEMA_JSON:\n"
        f"{_clip_context(schema)}\n\n"
        "SCOPE_FUNCTION_SOURCE:\n"
        "```python\n"
        f"{_clip_context(SCOPE_FN_SOURCE)}\n"
        "```\n\n"
        "QUESTION_AND_CONTEXT:\n"
        f"{body}"
    )
    return [
        {"role": "system", "content": _DIRECT_SQL_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _plan_direct_sql(body: str, schema: str) -> dict[str, Any]:
    messages = _build_sql_planner_messages(body, schema)
    last_error = ""
    for attempt in range(2):
        try:
            planner_text = _llm_chat(messages)
            plan = _extract_json_object(planner_text)
        except Exception as e:
            last_error = str(e)
            if attempt == 0:
                if plan := _try_top_date_count_plan(body, schema):
                    return plan
            else:
                break
            plan = {"error": last_error}
        if not plan.get("error"):
            return plan

        last_error = str(plan["error"])
        if attempt == 0:
            if plan := _try_top_date_count_plan(body, schema):
                return plan
            messages = [
                *messages,
                {"role": "assistant", "content": json.dumps(plan)},
                {
                    "role": "user",
                    "content": _DIRECT_SQL_REPAIR_PROMPT.format(error=last_error),
                },
            ]

    raise RuntimeError(last_error or "planner returned an error")


def _run_direct_sql_fallback(body: str) -> str | None:
    if not SCOPE_FN_SOURCE.strip():
        return None

    schema = _call_tool("get_schema", {})
    plan = _plan_direct_sql(body, schema)

    sql = _normalize_select_sql(str(plan.get("sql") or ""))
    params = plan.get("params") or []
    if not isinstance(params, list):
        raise ValueError("planner params must be a list")

    result = _call_tool("execute_sql", {"sql": sql, "params": params})
    return _format_scoped_sql_result(result)


def _try_direct_sql_fallback(body: str, reason: str) -> str | None:
    if not _ENABLE_DIRECT_SQL_FALLBACK:
        print(f"Direct SQL fallback disabled after {reason}.", file=sys.stderr)
        return None
    try:
        answer = _run_direct_sql_fallback(body)
    except Exception as e:
        print(f"Direct SQL fallback failed after {reason}: {e}", file=sys.stderr)
        return None
    if answer and answer.strip():
        print(f"Direct SQL fallback used after {reason}.", file=sys.stderr)
        return answer
    return None


def _run_ai_agent(body: str) -> str:
    base_url = os.environ["BRIDGE_URL"].rstrip("/") + "/v1"
    api_key = os.environ["SESSION_TOKEN"]

    agent = AIAgent(
        base_url=base_url,
        api_key=api_key,
        provider="custom",
        model=HIVEMIND_MODEL,
        # Match agents/default-query/agent.py:113 — tool-heavy workflows
        # destabilize at higher turn counts; cap to fail fast.
        max_iterations=6,
        enabled_toolsets=["hivemind"],
        ephemeral_system_prompt=SYSTEM_PROMPT,
        skip_context_files=True,
        skip_memory=True,
        quiet_mode=True,
        save_trajectories=False,
        max_tokens=1024,
        reasoning_config=_NO_REASONING_CONFIG,
        request_overrides=_NO_REASONING_OVERRIDES,
    )
    with redirect_stdout(sys.stderr):
        return agent.chat(body) or ""


def _retry_body(body: str, reason: str, previous_response: str) -> str:
    previous = (previous_response or "").strip()
    if len(previous) > 2000:
        previous = previous[:2000] + "\n[truncated]"
    return (
        f"{body}\n\n"
        "RECOVERY INSTRUCTION:\n"
        f"The previous attempt did not produce a usable final answer: {reason}.\n"
        "Continue the task using the available tools. Inspect schema if needed, "
        "write PostgreSQL SELECT statements, and if execute_sql returns an "
        "error, correct the SQL and retry. Do not ask the user for schema, "
        "columns, or date formats that can be discovered with tools.\n\n"
        f"PREVIOUS RESPONSE:\n{previous}"
    )


def _retry_ai_agent_if_fallback_disabled(
    body: str,
    *,
    reason: str,
    previous_response: str = "",
) -> str | None:
    if _ENABLE_DIRECT_SQL_FALLBACK:
        return None
    try:
        retry_response = _run_ai_agent(_retry_body(body, reason, previous_response))
    except Exception as e:
        print(f"AIAgent retry error after {reason}: {e}", file=sys.stderr)
        return None
    if not retry_response or not retry_response.strip():
        print(f"AIAgent retry produced empty response after {reason}.", file=sys.stderr)
        return None
    if _looks_like_runtime_failure(retry_response):
        print(
            f"AIAgent retry runtime failure after {reason}: {retry_response[:500]}",
            file=sys.stderr,
        )
        return None
    if _looks_like_unresolved_response(retry_response):
        print(
            f"AIAgent retry unresolved after {reason}: {retry_response[:500]}",
            file=sys.stderr,
        )
        return None
    return retry_response


def main() -> None:
    if not QUERY_PROMPT.strip():
        print("No question provided.")
        return

    body = QUERY_PROMPT
    if QUERY_CONTEXT.strip():
        body = f"Context: {QUERY_CONTEXT}\n\nQuestion: {QUERY_PROMPT}"
    planner_body = body
    if SCOPE_FN_SOURCE.strip():
        body = (
            "The scope agent has produced this privacy filter that wraps "
            "your SQL results. Read it; understand what SQL pattern it "
            "expects and what transformation it applies to the rows.\n\n"
            "```python\n"
            f"{SCOPE_FN_SOURCE}\n"
            "```\n\n"
            f"{body}"
        )

    try:
        response = _run_ai_agent(body)
    except Exception as e:
        print(f"AIAgent error: {e}", file=sys.stderr)
        if answer := _try_direct_sql_fallback(planner_body, "AIAgent error"):
            print(answer)
            return
        print(_user_facing_fallback())
        return

    if not response or not response.strip():
        if answer := _retry_ai_agent_if_fallback_disabled(
            body,
            reason="empty AIAgent response",
            previous_response=response or "",
        ):
            print(answer)
            return
        if answer := _try_direct_sql_fallback(planner_body, "empty AIAgent response"):
            print(answer)
            return
        print(_user_facing_fallback())
        return
    if _looks_like_runtime_failure(response):
        print(f"Hermes runtime failure from AIAgent: {response[:500]}", file=sys.stderr)
        if answer := _try_direct_sql_fallback(planner_body, "Hermes runtime failure"):
            print(answer)
            return
        print(_user_facing_fallback())
        return
    if _looks_like_unresolved_response(response):
        print(f"Unresolved AIAgent response: {response[:500]}", file=sys.stderr)
        if answer := _retry_ai_agent_if_fallback_disabled(
            body,
            reason="unresolved AIAgent response",
            previous_response=response,
        ):
            print(answer)
            return
        if answer := _try_direct_sql_fallback(planner_body, "unresolved AIAgent response"):
            print(answer)
            return
        print(_user_facing_fallback())
        return

    print(response)


if __name__ == "__main__":
    main()

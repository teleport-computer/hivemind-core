"""Default scope agent — fully autonomous Claude Code with bridge MCP tools.

Determines access control for query agent SQL results by writing a scope
function that acts as a query firewall.

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
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, query, tool
from _bridge import (
    bridge_tool,
    bridge_simulate,
    create_hivemind_server,
)

QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")
QUERY_AGENT_ID = os.environ.get("QUERY_AGENT_ID", "")

# ── Scope-specific MCP tools ──


@tool(
    "list_query_agent_files",
    "List the source files of the query agent that will execute. Returns JSON with a 'files' array. Use this to inspect the query agent for safety.",
    {},
)
async def list_agent_files_tool(args: dict[str, Any]) -> dict[str, Any]:
    result = await bridge_tool("list_query_agent_files", {})
    return {"content": [{"type": "text", "text": result}]}


@tool(
    "read_query_agent_file",
    "Read a specific source file from the query agent.",
    {"file_path": str},
)
async def read_agent_file_tool(args: dict[str, Any]) -> dict[str, Any]:
    result = await bridge_tool("read_query_agent_file", {
        "file_path": args.get("file_path", ""),
    })
    return {"content": [{"type": "text", "text": result}]}


@tool(
    "simulate_query",
    "Run the query agent in a sandboxed simulation with a proposed scope function. "
    "Returns the agent's output so you can verify the scope function works correctly. "
    "Pass scope_fn_source as a Python function string.",
    {"prompt": str, "scope_fn_source": str},
)
async def simulate_tool(args: dict[str, Any]) -> dict[str, Any]:
    prompt = args.get("prompt", QUERY_PROMPT)
    scope_fn_source = args.get("scope_fn_source", "")
    if not scope_fn_source:
        return {"content": [{"type": "text", "text": "Error: scope_fn_source is required"}]}

    result = await bridge_simulate(QUERY_AGENT_ID, prompt, scope_fn_source)
    if result is None:
        return {"content": [{"type": "text", "text": "Simulation failed or unavailable."}]}
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


# simulate_tool disabled — Phala ephemeral CVMs too slow for nested simulation
SCOPE_TOOLS = [list_agent_files_tool, read_agent_file_tool]
server = create_hivemind_server(extra_tools=SCOPE_TOOLS)

SYSTEM_PROMPT = """\
You are a scope agent. Your job is to write a Python scope function that acts \
as a query firewall for a query agent's SQL results.

You have MCP tools to access the database and inspect the query agent:
- mcp__hivemind__execute_sql: Execute SQL queries to understand the data.
- mcp__hivemind__get_schema: Get the database schema.
- mcp__hivemind__list_query_agent_files: List the query agent's source files.
- mcp__hivemind__read_query_agent_file: Read a source file to check for suspicious code.

You also have local Claude Code tools (Bash, Read, Write, Grep, Glob) \
available inside your container. Note: there is NO external network access — \
tools like WebSearch and WebFetch will not work. Use MCP tools for all data access.

IMPORTANT: Work quickly. Inspect the schema, check the query agent code, \
then output your scope function. Do not run extensive analysis — just enough \
to determine appropriate access control.

Your output MUST be ONLY a JSON object:
{"scope_fn": "def scope(sql, params, rows):\\n    return {\\"allow\\": True, \\"rows\\": rows}"}

CRITICAL — The scope function MUST have EXACTLY 3 parameters: sql, params, rows.
If you use fewer or more parameters, the system WILL reject it.

Correct example:

  def scope(sql, params, rows):
      return {"allow": True, "rows": rows}

WRONG (will be rejected):

  def scope(sql, rows):       # WRONG — missing params
  def scope(query, results):  # WRONG — must be sql, params, rows

The scope function signature:
  def scope(sql: str, params: list, rows: list[dict]) -> dict:
      # sql: the SQL query the query agent issued
      # params: query parameters (list of bind values)
      # rows: the raw query results (list of dicts)
      # Return one of:
      #   {"allow": True, "rows": rows}  — pass through as-is
      #   {"allow": True, "rows": filtered}  — transform/filter results
      #   {"allow": False, "error": "reason"}  — block this query

Available builtins: len, str, int, float, bool, list, dict, set, tuple,
min, max, sum, sorted, any, all, abs, round, enumerate, zip, range.
No imports, no exec/eval, no dunder attributes.

Constraints:
- The scope function sees every query's results and decides what passes through.
- Use it for access control: column redaction, row filtering, k-anonymity, etc.
- When in doubt, be permissive — false negatives are worse than false positives.
- Do NOT use simulate_query — it is very slow. Just return your scope function directly.
- Keep it simple: inspect the schema and query agent code, then output the JSON.
- Output ONLY the JSON object, nothing else.
"""


def _extract_scope_json(text: str) -> dict:
    """Extract a scope JSON object from LLM output, handling nested braces."""
    text = text.strip()
    # Try direct parse first
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "scope_fn" in parsed:
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
        else:
            text = "\n".join(lines[1:]).strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

    # Find balanced JSON objects containing our key
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

    # Fallback: allow-all scope function
    return {"scope_fn": "def scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}"}


async def main() -> None:
    if not QUERY_PROMPT.strip():
        print(json.dumps({"scope_fn": "def scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}"}))
        return

    prompt = f"Determine scope for this query: {QUERY_PROMPT}"
    if QUERY_AGENT_ID:
        prompt += f"\nQuery agent ID: {QUERY_AGENT_ID}"

    final_result = ""
    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                system_prompt=SYSTEM_PROMPT,
                mcp_servers={"hivemind": server},
                permission_mode="bypassPermissions",
                cwd="/tmp",
            ),
        ):
            if hasattr(message, "result"):
                final_result = message.result
    except Exception as e:
        print(f"Agent SDK error: {e}", file=sys.stderr)
        # Fail open with allow-all scope
        print(json.dumps({"scope_fn": "def scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}"}))
        return

    # Extract JSON from result
    print(json.dumps(_extract_scope_json(final_result)))


if __name__ == "__main__":
    asyncio.run(main())

"""Hivemind query agent using the Claude Agent SDK.

Uses the Agent SDK's built-in agentic loop with custom MCP tools
that call the bridge's tool endpoints (execute_sql, get_schema).

Environment variables (set automatically by the sandbox):
  BRIDGE_URL       — Bridge HTTP endpoint
  SESSION_TOKEN    — Bearer token for bridge auth
  ANTHROPIC_BASE_URL — Points at bridge (SDK routes LLM calls here)
  ANTHROPIC_API_KEY  — Same as session token
  QUERY_PROMPT     — The user's query to answer
  AGENT_ROLE       — "query"
"""

import asyncio
import json
import os
from typing import Any

import aiohttp
from claude_agent_sdk import (
    ClaudeAgentOptions,
    query,
    tool,
    create_sdk_mcp_server,
)

BRIDGE_URL = os.environ["BRIDGE_URL"]
SESSION_TOKEN = os.environ["SESSION_TOKEN"]
QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")


async def _bridge_tool(name: str, arguments: dict[str, Any]) -> str:
    """Call a bridge tool endpoint and return the result string."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{BRIDGE_URL}/tools/{name}",
            json={"arguments": arguments},
            headers={"Authorization": f"Bearer {SESSION_TOKEN}"},
        ) as resp:
            data = await resp.json()
            if data.get("error"):
                return f"Error: {data['error']}"
            return data.get("result", "")


# ── Custom MCP tools wrapping bridge endpoints ──


@tool(
    "execute_sql",
    "Execute a SQL query against the database. Returns JSON rows for SELECT, "
    "or {rowcount: N} for writes. Use %s for parameter placeholders.",
    {"sql": str, "params": str},
)
async def execute_sql_tool(args: dict[str, Any]) -> dict[str, Any]:
    call_args: dict[str, Any] = {"sql": args.get("sql", "")}
    params_raw = args.get("params", "[]")
    if isinstance(params_raw, str):
        try:
            call_args["params"] = json.loads(params_raw)
        except json.JSONDecodeError:
            call_args["params"] = []
    elif isinstance(params_raw, list):
        call_args["params"] = params_raw
    result = await _bridge_tool("execute_sql", call_args)
    return {"content": [{"type": "text", "text": result}]}


@tool(
    "get_schema",
    "Get the database schema: table names, column names, types, and defaults. "
    "Use this to understand the data model before writing queries.",
    {},
)
async def get_schema_tool(args: dict[str, Any]) -> dict[str, Any]:
    result = await _bridge_tool("get_schema", {})
    return {"content": [{"type": "text", "text": result}]}


hivemind_server = create_sdk_mcp_server(
    name="hivemind",
    version="1.0.0",
    tools=[execute_sql_tool, get_schema_tool],
)

SYSTEM_PROMPT = """\
You are a database query agent. Your job is to answer questions by querying
the database using SQL.

Strategy:
1. Start by calling get_schema to understand the database structure.
2. Write SQL queries to find relevant data.
3. Use parameterized queries (%s placeholders) to prevent SQL injection.
4. Synthesize information from query results into a clear answer.

Be concise and accurate. Paraphrase — do not dump raw data verbatim."""


async def main() -> None:
    if not QUERY_PROMPT:
        print("Error: QUERY_PROMPT not set")
        return

    final_result = ""
    async for message in query(
        prompt=QUERY_PROMPT,
        options=ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT,
            mcp_servers={"hivemind": hivemind_server},
            permission_mode="bypassPermissions",
        ),
    ):
        if hasattr(message, "result"):
            final_result = message.result

    print(final_result)


if __name__ == "__main__":
    asyncio.run(main())

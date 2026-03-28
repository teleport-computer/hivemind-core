"""Shared bridge helpers for default hivemind agents using Claude Agent SDK.

Provides:
  - bridge_tool(name, arguments) — call a bridge tool endpoint
  - bridge_simulate(...) — call the sandbox simulate endpoint (scope agents)
  - Standard MCP tool definitions: execute_sql, get_schema
  - create_hivemind_server(extra_tools) — build an MCP server with standard + custom tools
"""

import json
import os
from typing import Any

import aiohttp
from claude_agent_sdk import tool, create_sdk_mcp_server

BRIDGE_URL = os.environ["BRIDGE_URL"]
SESSION_TOKEN = os.environ["SESSION_TOKEN"]

# HTTP timeout for bridge calls (seconds)
_BRIDGE_TIMEOUT = aiohttp.ClientTimeout(total=120)


async def bridge_tool(name: str, arguments: dict[str, Any]) -> str:
    """Call a bridge tool endpoint and return the result string."""
    async with aiohttp.ClientSession(timeout=_BRIDGE_TIMEOUT) as session:
        async with session.post(
            f"{BRIDGE_URL}/tools/{name}",
            json={"arguments": arguments},
            headers={"Authorization": f"Bearer {SESSION_TOKEN}"},
        ) as resp:
            data = await resp.json()
            if data.get("error"):
                return f"Error: {data['error']}"
            return data.get("result", "")


async def bridge_simulate(
    query_agent_id: str,
    prompt: str,
    scope_fn_source: str,
    replay_tape: list[dict] | None = None,
) -> dict | None:
    """Call the sandbox simulate endpoint. Returns response dict or None on failure."""
    payload: dict[str, Any] = {
        "query_agent_id": query_agent_id,
        "prompt": prompt,
        "scope_fn_source": scope_fn_source,
    }
    if replay_tape is not None:
        payload["replay_tape"] = replay_tape
    async with aiohttp.ClientSession(timeout=_BRIDGE_TIMEOUT) as session:
        async with session.post(
            f"{BRIDGE_URL}/sandbox/simulate",
            json=payload,
            headers={"Authorization": f"Bearer {SESSION_TOKEN}"},
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.json()


# ── Standard MCP tool definitions ──


@tool(
    "execute_sql",
    "Execute a SQL query against the database. Returns JSON rows for SELECT, or {rowcount: N} for writes. Use %s for parameter placeholders.",
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
    result = await bridge_tool("execute_sql", call_args)
    return {"content": [{"type": "text", "text": result}]}


@tool(
    "get_schema",
    "Get the database schema: table names, column names, types, and defaults. Use this to understand the data model before writing queries.",
    {},
)
async def get_schema_tool(args: dict[str, Any]) -> dict[str, Any]:
    result = await bridge_tool("get_schema", {})
    return {"content": [{"type": "text", "text": result}]}


STANDARD_TOOLS = [execute_sql_tool, get_schema_tool]


def create_hivemind_server(extra_tools: list | None = None):
    """Create an MCP server with standard tools plus any extras."""
    all_tools = list(STANDARD_TOOLS)
    if extra_tools:
        all_tools.extend(extra_tools)
    return create_sdk_mcp_server(
        name="hivemind",
        version="1.0.0",
        tools=all_tools,
    )

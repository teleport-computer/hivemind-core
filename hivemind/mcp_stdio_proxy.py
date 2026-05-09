"""Standalone stdio MCP server proxying to the hivemind bridge.

Exposes the same 4 tools the scope agent uses (get_schema, execute_sql,
verify_scope_fn, simulate_query) via Model Context Protocol over stdio,
so alternative agent runtimes (claw-code, Claude Code, any MCP client)
can reach the bridge without the claude-agent-sdk Python in-process hook.

The proxy reads BRIDGE_URL, SESSION_TOKEN, and QUERY_AGENT_ID from env.
It speaks JSON-RPC 2.0 line-delimited on stdin/stdout, per the MCP spec.

Run with: BRIDGE_URL=... SESSION_TOKEN=... python -m hivemind.mcp_stdio_proxy
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

BRIDGE_URL = os.environ.get("BRIDGE_URL", "")
SESSION_TOKEN = os.environ.get("SESSION_TOKEN", "")
QUERY_AGENT_ID = os.environ.get("QUERY_AGENT_ID", "")
QUERY_PROMPT = os.environ.get("QUERY_PROMPT", "")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "hivemind"
SERVER_VERSION = "0.3.0"

# ---------------------------------------------------------------------------
# Tool schemas — minimal, matching the in-process MCP tool definitions.
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_schema",
        "description": (
            "Return the user-table schema as JSON. Use first, before any execute_sql call."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "execute_sql",
        "description": (
            "Run a read-only SELECT on the user tables. Returns list-of-dicts. "
            "Reject dunder / _hivemind_ paths at the host layer. Pass params=[] "
            "when the SQL has no %s placeholders."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "params": {"type": "array", "items": {}},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "verify_scope_fn",
        "description": (
            "Compile a candidate scope_fn source string against the host's "
            "AST validator and optionally run it against test rows. Returns "
            "{compiles, compile_error, all_tests_passed, results}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "tests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sql": {"type": "string"},
                            "params": {"type": "array"},
                            "rows": {"type": "array"},
                        },
                    },
                },
            },
            "required": ["source"],
        },
    },
    {
        "name": "simulate_query",
        "description": (
            "Run the query agent as an NPC with a candidate scope_fn_source. "
            "Returns {output, usage, tape}. Expensive (~60s)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_fn_source": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["scope_fn_source"],
        },
    },
]


# ---------------------------------------------------------------------------
# HTTP dispatch to the bridge.
# ---------------------------------------------------------------------------


def _http_post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{BRIDGE_URL}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SESSION_TOKEN}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"error": f"HTTP {exc.code}: {detail[:500]}"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _call_bridge_tool(name: str, arguments: dict) -> str:
    data = _http_post(f"/tools/{name}", {"arguments": arguments})
    if data.get("error"):
        return f"Error: {data['error']}"
    return data.get("result", json.dumps(data))


def _call_simulate(arguments: dict) -> str:
    scope_fn_source = arguments.get("scope_fn_source", "")
    if not scope_fn_source:
        return "Error: scope_fn_source is required"
    prompt = arguments.get("prompt") or QUERY_PROMPT
    body = {
        "query_agent_id": QUERY_AGENT_ID,
        "prompt": prompt,
        "scope_fn_source": scope_fn_source,
    }
    data = _http_post("/sandbox/simulate", body)
    if data.get("error"):
        return f"Error: {data['error']}"
    return json.dumps(data)


def _dispatch_tool(name: str, arguments: dict) -> str:
    if name == "simulate_query":
        return _call_simulate(arguments)
    return _call_bridge_tool(name, arguments)


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 server loop.
# ---------------------------------------------------------------------------


def _send(msg: dict) -> None:
    line = json.dumps(msg)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _err(req_id, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def _handle(msg: dict) -> dict | None:
    req_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None  # no-op notifications

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        text = _dispatch_tool(name, arguments)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": text}]},
        }

    return _err(req_id, -32601, f"method not implemented: {method}")


def main() -> None:
    if not BRIDGE_URL or not SESSION_TOKEN:
        sys.stderr.write("mcp_stdio_proxy: BRIDGE_URL and SESSION_TOKEN must be set\n")
        sys.exit(2)
    sys.stderr.write(
        f"mcp_stdio_proxy: ready, bridge={BRIDGE_URL}, query_agent={QUERY_AGENT_ID!r}\n"
    )
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            _send(_err(None, -32700, f"parse error: {exc}"))
            continue
        try:
            resp = _handle(msg)
        except Exception as exc:
            resp = _err(msg.get("id"), -32603, f"internal: {type(exc).__name__}: {exc}")
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    main()

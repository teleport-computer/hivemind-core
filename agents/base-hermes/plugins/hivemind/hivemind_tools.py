"""Native Hermes tools that call the per-session hivemind bridge.

Each tool registers on Hermes' global `ToolRegistry` at import time.
Handlers are async (HTTP I/O). All tools register under toolset
`"hivemind"`. Per-role gating lives at the bottom of this file: the
plugin reads HIVEMIND_AGENT_ROLE and registers ONLY the tools the
role is meant to see. The query agent never learns that simulation
tools exist; the mediator gets nothing at all.

Env vars (set by the sandbox runner before launching `hermes -z ...`):
  BRIDGE_URL          — base URL of this session's bridge
  SESSION_TOKEN       — bearer token for bridge auth
  HIVEMIND_AGENT_ROLE — query | scope | mediator (unset → no tools)
  QUERY_AGENT_ID      — scope-only: target query agent for simulate*
  QUERY_PROMPT        — scope-only: default prompt for simulate* when caller omits
"""

from __future__ import annotations

import json
import os
import base64
from urllib.parse import quote
from typing import Any

import httpx  # base dep of hermes-agent — no extra install needed

from tools.registry import registry  # Hermes' singleton

TOOLSET = "hivemind"
EMOJI = "🐝"
_TIMEOUT = httpx.Timeout(120.0)
_REQUIRES_ENV = ["BRIDGE_URL", "SESSION_TOKEN"]


def _bridge_url() -> str:
    url = os.environ.get("BRIDGE_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("BRIDGE_URL not set; hivemind tools cannot reach bridge")
    return url


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ.get('SESSION_TOKEN', '')}"}


def _check_bridge_env() -> bool:
    return bool(os.environ.get("BRIDGE_URL") and os.environ.get("SESSION_TOKEN"))


async def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST JSON to the bridge; raise on non-2xx with a short body excerpt."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(f"{_bridge_url()}{path}", json=payload, headers=_auth_headers())
        if resp.status_code >= 300:
            raise RuntimeError(f"bridge {path} returned {resp.status_code}: {resp.text[:500]}")
        return resp.json()


async def _get(path: str) -> dict[str, Any]:
    """GET JSON from the bridge; raise on non-2xx with a short body excerpt."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{_bridge_url()}{path}", headers=_auth_headers())
        if resp.status_code >= 300:
            raise RuntimeError(f"bridge {path} returned {resp.status_code}: {resp.text[:500]}")
        return resp.json()


async def _bridge_tool(name: str, arguments: dict[str, Any]) -> str:
    """Generic /tools/{name} dispatch. Returns the bridge's `result` string."""
    data = await _post(f"/tools/{name}", {"arguments": arguments})
    if data.get("error"):
        return f"Error: {data['error']}"
    return data.get("result", "") or ""


# ── Tool schemas (OpenAI function-schema format; Hermes expects this shape) ──

EXECUTE_SQL_SCHEMA = {
    "name": "execute_sql",
    "description": (
        "Execute a SQL query against the hivemind database. Returns JSON "
        "rows for SELECT, or {rowcount: N} for writes. Use %s for "
        "parameter placeholders; pass params=[] when the SQL has no %s "
        "placeholders."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "The SQL query to run."},
            "params": {
                "type": "array",
                "items": {},
                "description": "Query parameters for %s placeholders; use [] when there are none.",
                "default": [],
            },
        },
        "required": ["sql"],
    },
}

GET_SCHEMA_SCHEMA = {
    "name": "get_schema",
    "description": (
        "Get the database schema: table names, column names, types, "
        "defaults. Use this BEFORE writing queries to learn the data model."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

UPLOAD_ARTIFACT_SCHEMA = {
    "name": "upload_artifact",
    "description": (
        "Upload a query-run artifact to the room artifact store. Use this "
        "for generated reports, tables, JSON, CSV, Markdown, HTML, images, "
        "or PDFs. Pass text content with encoding='text' or prebuilt binary "
        "content as base64 with encoding='base64'. Artifact uploads are only "
        "available when the room allows artifacts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": (
                    "Safe basename only, e.g. report.md, report.json, report.pdf."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "Artifact content. UTF-8 text when encoding='text'; "
                    "base64 bytes when encoding='base64'."
                ),
            },
            "content_type": {
                "type": "string",
                "description": (
                    "MIME type, e.g. text/markdown, application/json, "
                    "text/csv, text/html, application/pdf."
                ),
                "default": "text/plain; charset=utf-8",
            },
            "encoding": {
                "type": "string",
                "enum": ["text", "base64"],
                "description": "How to interpret content.",
                "default": "text",
            },
        },
        "required": ["filename", "content"],
    },
}

UPLOAD_REPORT_ARTIFACT_SCHEMA = {
    "name": "upload_report_artifact",
    "description": (
        "Upload a Markdown report and, when possible, a rendered PDF version "
        "to the room artifact store. Use for substantial reports, studies, "
        "memos, research writeups, or when the user asks for a file/PDF. "
        "Only available when the room allows artifacts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Safe basename/stem, e.g. report or watch_history_report.",
                "default": "report",
            },
            "markdown": {
                "type": "string",
                "description": "The complete Markdown report content.",
            },
            "include_pdf": {
                "type": "boolean",
                "description": "Also render and upload a PDF copy.",
                "default": True,
            },
        },
        "required": ["markdown"],
    },
}

VERIFY_SCOPE_FN_SCHEMA = {
    "name": "verify_scope_fn",
    "description": (
        "Compile + test a candidate scope_fn against synthetic test cases. "
        "Pass `source` as the full Python function text and `tests` as a "
        "JSON array of {sql, params, rows, expect_allow?, expect_min_rows?, label?}. Returns "
        "{compiles, compile_error, all_tests_passed, results}. Fast (ms), "
        "no LLM call. Scope-agent only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Full source: `def scope(sql, params, rows): ...`",
            },
            "tests": {
                "type": "string",
                "description": (
                    "JSON array of test cases. Each: {sql, params, rows, "
                    "expect_allow?, expect_min_rows?, label?}."
                ),
            },
        },
        "required": ["source", "tests"],
    },
}

SIMULATE_QUERY_SCHEMA = {
    "name": "simulate_query",
    "description": (
        "Play the query agent as an NPC in a sandboxed run with a "
        "candidate scope_fn_source; returns {output, usage, tape}. SLOW "
        "(~60s, nested LLM run). Use ONCE per candidate scope_fn before "
        "you emit your final JSON, not per iteration. Scope-agent only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "scope_fn_source": {
                "type": "string",
                "description": "Full source of the candidate scope function.",
            },
            "prompt": {
                "type": "string",
                "description": "Override question; defaults to QUERY_PROMPT.",
                "default": "",
            },
        },
        "required": ["scope_fn_source"],
    },
}

SIMULATE_MULTI_SCHEMA = {
    "name": "simulate_multi",
    "description": (
        "Run 2-3 candidate scope_fn's in PARALLEL against the same query. "
        "`candidates` is a JSON array of full scope_fn source strings (max "
        "3). Returns {results: [{idx, output, error}, ...]}. Same budget "
        "as ONE simulate_query (split across candidates). Scope-agent only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "string",
                "description": "JSON array of scope_fn source strings (max 3).",
            },
            "prompt": {
                "type": "string",
                "description": "Override question; defaults to QUERY_PROMPT.",
                "default": "",
            },
        },
        "required": ["candidates"],
    },
}

# ── Source-reading: bounded bridge tools for the NPC-simulator workflow ──
#
# These tools expose the server-side agent-file store over the per-session
# bridge instead of enabling Hermes' broad `files` or `terminal` toolsets.
# This works in the Phala deployment where child containers cannot bind-mount
# paths from inside the core container through the host Docker socket.

QUERY_AGENT_MOUNT = "/workspace/query-agent"
_MAX_FILE_BYTES = 200_000  # cap to keep prompt tokens bounded

LIST_QUERY_AGENT_FILES_SCHEMA = {
    "name": "list_query_agent_files",
    "description": (
        "List files in the read-only query-agent source mount "
        f"({QUERY_AGENT_MOUNT}). Returns one relative path per line. Use "
        "BEFORE simulate_query to inspect the NPC's code + prompt and "
        "predict its behavior. Scope-agent only."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

READ_QUERY_AGENT_FILE_SCHEMA = {
    "name": "read_query_agent_file",
    "description": (
        "Read a single file from the query-agent source mount. `path` is "
        f"relative to {QUERY_AGENT_MOUNT}. Path traversal (`..`, absolute "
        "paths) is rejected. Returns the file contents (truncated at "
        f"{_MAX_FILE_BYTES} bytes). Scope-agent only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    f"Relative path under {QUERY_AGENT_MOUNT} (e.g. 'agent.py', 'query-prompt.md')."
                ),
            },
        },
        "required": ["path"],
    },
}


# ── Handlers ──


async def execute_sql_handler(args: dict[str, Any], **_kw) -> str:
    sql = args.get("sql", "") or ""
    params_raw = args.get("params", "[]")
    if isinstance(params_raw, str):
        try:
            params = json.loads(params_raw)
        except json.JSONDecodeError:
            params = []
    elif isinstance(params_raw, list):
        params = params_raw
    else:
        params = []
    return await _bridge_tool("execute_sql", {"sql": sql, "params": params})


async def get_schema_handler(_args: dict[str, Any], **_kw) -> str:
    return await _bridge_tool("get_schema", {})


async def upload_artifact_handler(args: dict[str, Any], **_kw) -> str:
    filename = (args.get("filename") or "").strip()
    content = args.get("content", "")
    content_type = (
        args.get("content_type") or "text/plain; charset=utf-8"
    ).strip() or "text/plain; charset=utf-8"
    encoding = (args.get("encoding") or "text").strip().lower()
    if not filename:
        return "Error: filename is required"
    if encoding == "base64":
        try:
            base64.b64decode(str(content), validate=True)
        except Exception:
            return "Error: content is not valid base64"
        content_base64 = str(content)
    elif encoding == "text":
        content_base64 = base64.b64encode(str(content).encode("utf-8")).decode("ascii")
    else:
        return "Error: encoding must be 'text' or 'base64'"

    try:
        data = await _post(
            "/sandbox/artifact-upload",
            {
                "filename": filename,
                "content_base64": content_base64,
                "content_type": content_type,
            },
        )
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data)


async def upload_report_artifact_handler(args: dict[str, Any], **_kw) -> str:
    filename = (args.get("filename") or "report").strip() or "report"
    markdown = args.get("markdown", "")
    include_pdf = bool(args.get("include_pdf", True))
    if not str(markdown).strip():
        return "Error: markdown is required"
    try:
        data = await _post(
            "/sandbox/report-artifact",
            {
                "filename": filename,
                "markdown": str(markdown),
                "include_pdf": include_pdf,
            },
        )
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(data)


async def verify_scope_fn_handler(args: dict[str, Any], **_kw) -> str:
    source = args.get("source", "") or ""
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
    data = await _post("/sandbox/verify_scope_fn", {"source": source, "tests": tests})
    return json.dumps(data)


async def simulate_query_handler(args: dict[str, Any], **_kw) -> str:
    scope_fn_source = (args.get("scope_fn_source") or "").strip()
    if not scope_fn_source:
        return "Error: scope_fn_source is required"
    prompt = (args.get("prompt") or os.environ.get("QUERY_PROMPT") or "").strip()
    payload = {
        "query_agent_id": os.environ.get("QUERY_AGENT_ID", ""),
        "prompt": prompt,
        "scope_fn_source": scope_fn_source,
    }
    data = await _post("/sandbox/simulate", payload)
    return json.dumps(data)


async def list_query_agent_files_handler(_args: dict[str, Any], **_kw) -> str:
    """List query-agent source paths through the per-session bridge."""
    agent_id = os.environ.get("QUERY_AGENT_ID", "").strip()
    if not agent_id:
        return "Error: QUERY_AGENT_ID is not set"
    data = await _get(f"/sandbox/agents/{quote(agent_id, safe='')}/files")
    files = data.get("files") or []
    paths: list[str] = []
    for item in files:
        if isinstance(item, dict):
            path = item.get("path") or item.get("file_path") or ""
        else:
            path = str(item)
        if path:
            paths.append(path)
    return "\n".join(paths) if paths else "(empty)"


async def read_query_agent_file_handler(args: dict[str, Any], **_kw) -> str:
    """Read one query-agent source file through the per-session bridge."""
    import pathlib

    agent_id = os.environ.get("QUERY_AGENT_ID", "").strip()
    if not agent_id:
        return "Error: QUERY_AGENT_ID is not set"
    raw_path = (args.get("path") or "").strip()
    if not raw_path:
        return "Error: path is required"
    if raw_path.startswith("/") or ".." in pathlib.PurePosixPath(raw_path).parts:
        return "Error: path must be relative (no '..' or absolute paths)"
    try:
        data = await _get(
            f"/sandbox/agents/{quote(agent_id, safe='')}/files/{quote(raw_path, safe='')}"
        )
        content = data.get("content")
        if content is None:
            return f"Error: {raw_path} is not a readable query-agent file"
        return str(content)[:_MAX_FILE_BYTES]
    except Exception as e:
        return f"Error: {e}"


async def simulate_multi_handler(args: dict[str, Any], **_kw) -> str:
    candidates_raw = args.get("candidates", "[]")
    if isinstance(candidates_raw, str):
        try:
            candidates = json.loads(candidates_raw)
        except json.JSONDecodeError:
            return "Error: candidates must be a JSON array of strings"
    elif isinstance(candidates_raw, list):
        candidates = candidates_raw
    else:
        candidates = []
    candidates = [c for c in candidates if isinstance(c, str) and c.strip()]
    if not candidates:
        return "Error: candidates list is empty"
    if len(candidates) > 3:
        candidates = candidates[:3]
    prompt = (args.get("prompt") or os.environ.get("QUERY_PROMPT") or "").strip()
    payload = {
        "query_agent_id": os.environ.get("QUERY_AGENT_ID", ""),
        "prompt": prompt,
        "candidates": candidates,
    }
    data = await _post("/sandbox/simulate_batch", payload)
    return json.dumps(data)


# ── Per-role registration ──
#
# The plugin reads HIVEMIND_AGENT_ROLE and registers ONLY the tools the
# role is meant to see. The query agent must not be aware that
# simulation tools exist — leaking unused schemas costs prompt tokens
# and invites confused tool calls. Server-side 404 from /sandbox/*
# is defense-in-depth, NOT the primary boundary.
#
# Roles match the access levels in hivemind/sandbox/tools.py:
#   query        → execute_sql, get_schema, upload_artifact,
#                  upload_report_artifact
#   scope        → execute_sql, get_schema, verify_scope_fn,
#                  simulate_query, simulate_multi
#   mediator     → nothing (tools=[] in current default-mediator)
#
# Unknown / unset role registers nothing and logs a warning. A sandbox
# runner that forgets to set the role gets a tool-less Hermes — visible
# failure rather than silent over-exposure.

import logging as _logging

_log = _logging.getLogger(__name__)

_ROLE_TOOLS: dict[str, set[str]] = {
    "query": {
        "execute_sql",
        "get_schema",
        "upload_artifact",
        "upload_report_artifact",
    },
    "scope": {
        "execute_sql",
        "get_schema",
        "verify_scope_fn",
        "simulate_query",
        "simulate_multi",
        "list_query_agent_files",
        "read_query_agent_file",
    },
    "mediator": set(),
}

_role = os.environ.get("HIVEMIND_AGENT_ROLE", "").strip().lower()
_allowed = _ROLE_TOOLS.get(_role)
if _allowed is None:
    _log.warning(
        "HIVEMIND_AGENT_ROLE=%r is unset or unknown; registering NO tools. "
        "Set it to one of %s in the sandbox runner.",
        _role,
        sorted(_ROLE_TOOLS),
    )
    _allowed = set()

_REG_KW = dict(
    toolset=TOOLSET,
    check_fn=_check_bridge_env,
    requires_env=_REQUIRES_ENV,
    is_async=True,
    emoji=EMOJI,
)

_ALL_TOOLS = (
    ("execute_sql", EXECUTE_SQL_SCHEMA, execute_sql_handler),
    ("get_schema", GET_SCHEMA_SCHEMA, get_schema_handler),
    ("upload_artifact", UPLOAD_ARTIFACT_SCHEMA, upload_artifact_handler),
    (
        "upload_report_artifact",
        UPLOAD_REPORT_ARTIFACT_SCHEMA,
        upload_report_artifact_handler,
    ),
    ("verify_scope_fn", VERIFY_SCOPE_FN_SCHEMA, verify_scope_fn_handler),
    ("simulate_query", SIMULATE_QUERY_SCHEMA, simulate_query_handler),
    ("simulate_multi", SIMULATE_MULTI_SCHEMA, simulate_multi_handler),
    ("list_query_agent_files", LIST_QUERY_AGENT_FILES_SCHEMA, list_query_agent_files_handler),
    ("read_query_agent_file", READ_QUERY_AGENT_FILE_SCHEMA, read_query_agent_file_handler),
)

for _name, _schema, _handler in _ALL_TOOLS:
    if _name in _allowed:
        registry.register(name=_name, schema=_schema, handler=_handler, **_REG_KW)

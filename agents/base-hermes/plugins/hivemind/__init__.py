"""Hivemind plugin for Hermes Agent.

Importing this package triggers `hivemind_tools` to register native
tools (execute_sql, get_schema, verify_scope_fn, simulate_query,
simulate_multi, list_query_agent_files, read_query_agent_file) on Hermes'
global ToolRegistry. Each handler talks to this session's bridge over HTTP.

Native tool registration deliberately bypasses Hermes' MCP support —
the MCP path is observed to be much slower per call than native
ToolRegistry tools (see project memory: hermes harness).

No hooks, no slash commands. The plugin's only job is registration.
"""

from . import hivemind_tools  # noqa: F401 — import-time registration


def register(ctx) -> None:  # pragma: no cover — Hermes calls this if defined
    """Plugin lifecycle hook. Tool registration already happened at import; nothing else to do."""
    return

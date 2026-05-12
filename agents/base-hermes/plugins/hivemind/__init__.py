"""Hivemind plugin for Hermes Agent.

Importing this package triggers `hivemind_tools` to register native tools on
Hermes' global ToolRegistry. Each handler talks to this session's bridge over
HTTP. The exact tool set is role-gated by `HIVEMIND_AGENT_ROLE`; expensive
scope simulation/source-inspection tools are opt-in environment flags.

Native tool registration deliberately bypasses Hermes' MCP support —
the MCP path is observed to be much slower per call than native
ToolRegistry tools (see project memory: hermes harness).

No hooks, no slash commands. The plugin's only job is registration.
"""

from . import hivemind_tools  # noqa: F401 — import-time registration


def register(ctx) -> None:  # pragma: no cover — Hermes calls this if defined
    """Plugin lifecycle hook. Tool registration already happened at import; nothing else to do."""
    return

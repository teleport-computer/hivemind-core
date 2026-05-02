"""SQL-based tools for agent sandboxes.

Two data tools replace the old six record tools:
  - execute_sql(sql, params) — run SQL, return JSON rows
  - get_schema() — return table/column/index metadata

Access levels:
  - FULL_READ: scope agent — SELECT on all user tables, blocked from _hivemind_*
  - SCOPED: query agent — SQL runs against full DB, results pass through scope_fn
  - FULL_READWRITE: index agent — full DML, blocked from _hivemind_* writes
  - NONE: mediator — no DB access
"""

from __future__ import annotations

import json
import enum
import logging
from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database

logger = logging.getLogger(__name__)

MAX_RESULT_ROWS = 10_000


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., str]

    def to_openai_def(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class AccessLevel(enum.Enum):
    FULL_READ = "full_read"
    SCOPED = "scoped"
    FULL_READWRITE = "full_readwrite"
    NONE = "none"


# Postgres functions that look like SELECTable expressions but mutate session
# state, hold the connection open, or reach outside the database. SCOPED /
# FULL_READ tools must reject them — otherwise an LLM-supplied SQL string can:
#   - `SELECT set_config('search_path', 'attacker_schema', false)` mutates the
#     connection's search_path and shadows tables for subsequent queries
#     (the connection is shared across requests for a tenant);
#   - `SELECT pg_sleep(N)` parks the single shared connection for N seconds;
#   - `dblink`/`lo_*`/`pg_read_file` reach outside the row layer entirely.
# Names are matched case-insensitively against the function's leaf name; we
# do not try to be schema-aware because a bare `set_config(...)` resolves to
# `pg_catalog.set_config` regardless of search_path.
_FORBIDDEN_SQL_FUNCS = frozenset({
    "set_config",
    "set_role",
    "current_setting",  # not directly dangerous, but pairs with set_config
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "dblink",
    "dblink_exec",
    "dblink_connect",
    "dblink_disconnect",
    "lo_export",
    "lo_import",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "copy_to_program",
    "copy_from_program",
})


def _references_forbidden_funcs(stmt) -> bool:
    """Walk a sqlglot expression tree for any forbidden function call.

    Postgres-specific function names (``pg_sleep``, ``set_config``) parse as
    ``Anonymous`` nodes since sqlglot does not have a built-in expression
    class for each one. Builtins it does recognize (e.g. ``CURRENT_SETTING``)
    surface as their own ``Func`` subclasses with a ``sql_name``.
    """
    import sqlglot

    for node in stmt.walk():
        if isinstance(node, sqlglot.exp.Anonymous):
            name = (node.name or "").lower()
            if name in _FORBIDDEN_SQL_FUNCS:
                return True
        elif isinstance(node, sqlglot.exp.Func):
            try:
                name = (node.sql_name() or "").lower()
            except Exception:
                name = (node.key or "").lower()
            if name in _FORBIDDEN_SQL_FUNCS:
                return True
    return False


def _is_select_only(sql: str) -> bool:
    """Check if SQL is a read-only statement using sqlglot AST parsing.

    Walks the full AST including CTEs and subqueries to reject DML hidden
    inside otherwise-SELECT statements, and rejects calls to
    ``_FORBIDDEN_SQL_FUNCS`` (session-state mutation, sleep, fs/network reach).

    Uses the postgres dialect so PostgreSQL-specific operators (``~``, ``~*``,
    ``!~``, ``!~*``, ``::`` casts, array/JSON operators, etc.) parse cleanly.
    Without this, reconnaissance queries from the scope agent fail parsing
    and the tool loops rejecting otherwise-legitimate SELECTs.
    """
    import sqlglot

    try:
        statements = sqlglot.parse(
            sql, dialect="postgres", error_level=sqlglot.ErrorLevel.IGNORE
        )
    except sqlglot.errors.ParseError:
        return False

    if not statements:
        return False

    _DANGEROUS = (
        sqlglot.exp.Delete, sqlglot.exp.Update, sqlglot.exp.Insert,
        sqlglot.exp.Drop, sqlglot.exp.Create, sqlglot.exp.Alter,
        sqlglot.exp.Command,
    )

    for stmt in statements:
        if stmt is None:
            return False
        # Only SELECT statements are allowed at the top level
        if not isinstance(stmt, sqlglot.exp.Select):
            return False
        # Check for DML hidden inside CTEs or subqueries
        for node in stmt.walk():
            if isinstance(node, _DANGEROUS):
                return False
        if _references_forbidden_funcs(stmt):
            return False

    return True


def _references_internal_tables(sql: str) -> bool:
    """Check if SQL references _hivemind_* internal tables using AST parsing.

    Uses the postgres dialect so PG-specific syntax doesn't trip a ParseError
    which would flip this to True (fail-closed) and incorrectly deny the query.
    """
    import sqlglot

    try:
        for stmt in sqlglot.parse(
            sql, dialect="postgres", error_level=sqlglot.ErrorLevel.WARN
        ):
            if stmt is None:
                continue
            for table in stmt.find_all(sqlglot.exp.Table):
                if table.name.upper().startswith("_HIVEMIND_"):
                    return True
    except Exception:
        # Fail closed — if we can't parse, assume it references internal tables
        return True
    return False


# Schemas that catalog the entire database — would let an agent enumerate every
# table that exists, including ones outside its allowlist. Block all references.
_HIDDEN_SCHEMAS: frozenset[str] = frozenset({
    "information_schema",
    "pg_catalog",
    "pg_toast",
})

# Table-name prefixes that mark Hivemind's control-plane and tenant-DB internals.
# An agent on a sandboxed tenant DB shouldn't see any of these even by name.
_INTERNAL_TABLE_PREFIXES: tuple[str, ...] = (
    "_hivemind_",
    "_credit_",
    "_billing_",
    "_tenants",
)


def _validate_table_allowlist(
    sql: str,
    allowed_tables: list[str] | None,
) -> str | None:
    """Return None if every table reference in ``sql`` is permitted, else an
    opaque error string suitable for return to the agent.

    Enforcement layers (each independent):
      1. ``information_schema.*``, ``pg_catalog.*``, ``pg_toast.*`` — always rejected.
         These would let the agent enumerate the full schema by listing tables.
      2. ``_hivemind_*`` / ``_credit_*`` / ``_billing_*`` / ``_tenants`` — always rejected.
      3. If ``allowed_tables`` is non-None, every remaining table reference must
         be in the allowlist (case-insensitive). Tables not on the allowlist
         are treated as if they don't exist.

    ``allowed_tables=None`` is the legacy back-compat path (room manifests
    minted before this feature shipped) — it skips step 3 entirely so old rooms
    keep their previous behavior.

    Errors are deliberately opaque: the agent never learns whether a rejected
    table name actually exists in the tenant DB. The operator-side run log
    contains the full SQL for audit.
    """
    import sqlglot

    if allowed_tables is None:
        allowed_lower: set[str] | None = None
    else:
        allowed_lower = {t.strip().lower() for t in allowed_tables if t}

    try:
        statements = sqlglot.parse(
            sql, dialect="postgres", error_level=sqlglot.ErrorLevel.WARN,
        )
    except Exception:
        return "query rejected (could not parse)"

    for stmt in statements:
        if stmt is None:
            continue
        for table in stmt.find_all(sqlglot.exp.Table):
            schema = (table.db or "").lower().strip()
            name = (table.name or "").lower().strip()

            if schema in _HIDDEN_SCHEMAS:
                return "query rejected"

            if any(name.startswith(p) for p in _INTERNAL_TABLE_PREFIXES):
                return "query rejected"

            if allowed_lower is not None and name not in allowed_lower:
                return "query rejected"

    return None


MAX_RESULT_BYTES = 1_000_000  # cap serialized JSON output to ~1MB per call


def build_sql_tools(
    db: Database,
    access: AccessLevel,
    scope_fn: Callable[[str, list, list[dict]], dict] | None = None,
    scope_fn_source: str | None = None,
    allowed_tables: list[str] | None = None,
) -> list[Tool]:
    """Build SQL tools with the given access level.

    For SCOPED access, scope_fn is required — every query's results
    pass through it for filtering/transformation.

    If ``scope_fn_source`` is provided alongside ``scope_fn`` (the typical
    production path from ``Pipeline._run_query_agent``), the scope function
    is executed via ``apply_scope_fn`` in a child process with the
    ``SCOPE_FN_TIMEOUT`` hard kill. Without the source, we fall back to an
    in-process invocation — used by tests that pass plain Python functions.

    Defense-in-depth: ``access == SCOPED`` with ``scope_fn=None`` is rejected
    here. Earlier this was tolerated and the per-query gate silently became
    a passthrough (rows returned unfiltered). The pipeline now fails-closed
    upstream when the scope agent doesn't produce a usable scope_fn (see
    ``Pipeline.run_query_agent_tracked``), but we still refuse here so that
    no future caller can wire SCOPED tools without a scope_fn and get
    surprising unscoped behavior.
    """
    if access == AccessLevel.NONE:
        return []
    if access == AccessLevel.SCOPED and scope_fn is None:
        raise ValueError(
            "build_sql_tools(SCOPED, scope_fn=None) is unsafe: SCOPED tools "
            "require a scope_fn so every query's rows pass through a filter. "
            "Pass a scope_fn or use AccessLevel.FULL_READ."
        )

    def _serialize_rows(rows: list[dict]) -> str:
        out = json.dumps(rows, default=str)
        if len(out) <= MAX_RESULT_BYTES:
            return out
        # Drop rows from the tail until we fit. Tail-truncation keeps the
        # earliest rows whole rather than clipping a JSON-mid-string.
        keep = rows
        while keep and len(json.dumps(keep, default=str)) > MAX_RESULT_BYTES:
            keep = keep[: max(1, len(keep) // 2)]
        return json.dumps(
            {
                "rows": keep,
                "truncated": True,
                "original_row_count": len(rows),
                "returned_row_count": len(keep),
                "note": (
                    f"Output exceeded {MAX_RESULT_BYTES} bytes; truncated. "
                    "Use COUNT/aggregate or LIMIT to keep responses small."
                ),
            },
            default=str,
        )

    def execute_sql(sql: str, params: list | None = None) -> str:
        safe_params = params or []

        # Per-room allowlist + system-table block. allowed_tables is None for
        # legacy rooms whose manifests don't carry the field (we keep the old
        # _references_internal_tables behavior in that case via the prefix
        # check inside _validate_table_allowlist).
        violation = _validate_table_allowlist(sql, allowed_tables)
        if violation:
            logger.warning(
                "tool execute_sql rejected (access=%s, allowed=%s): %s",
                access.value, allowed_tables, sql,
            )
            return json.dumps({"error": violation})

        if access in (AccessLevel.FULL_READ, AccessLevel.SCOPED):
            if not _is_select_only(sql):
                return json.dumps({"error": "Only SELECT queries are allowed"})

        try:
            if _is_select_only(sql):
                rows = db.execute(sql, safe_params)
                rows = rows[:MAX_RESULT_ROWS]
            else:
                rowcount = db.execute_commit(sql, safe_params)
                return json.dumps({"rowcount": rowcount})
        except Exception as e:
            return json.dumps({"error": str(e)})

        # Apply scope function for SCOPED access. When we have the original
        # source we route through apply_scope_fn for subprocess isolation +
        # hard timeout — otherwise an LLM-supplied infinite loop or memory
        # bomb would hang the bridge thread. Without a source (test path)
        # we call directly; the caller's fn is trusted Python. ``scope_fn``
        # is non-None here: build_sql_tools enforces that for SCOPED.
        if access == AccessLevel.SCOPED:
            from .scope import apply_scope_fn

            try:
                result = apply_scope_fn(
                    scope_fn,
                    sql,
                    safe_params,
                    rows,
                    _source=scope_fn_source,
                )
            except Exception as e:
                logger.debug("Scope function error: %s", e)
                return json.dumps({"error": "Query denied by scope function"})
            if not result.get("allow", False):
                return json.dumps(
                    {"error": result.get("error", "Query denied by scope function")}
                )
            rows = result.get("rows") or []

        return _serialize_rows(rows)

    def get_schema() -> str:
        try:
            schema = db.get_schema(exclude_internal=True)
            # When the room has an allowlist, the agent must not see any
            # other tables — not even by name. Filter the schema rows
            # (a list of {table_name, column_name, ...}) to keep only the
            # allowed tables. Legacy rooms (allowed_tables=None) get the
            # old "everything except _hivemind_*" view.
            if allowed_tables is not None:
                allow = {t.strip().lower() for t in allowed_tables if t}
                schema = [
                    r for r in schema
                    if str(r.get("table_name", "")).lower() in allow
                ]
            return json.dumps(schema, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    tools = [
        Tool(
            name="execute_sql",
            description=(
                "Execute a SQL query against the database. "
                "For SELECT queries, returns a JSON array of row objects. "
                "For write queries (INSERT/UPDATE/DELETE), returns {rowcount: N}. "
                "Use parameterized queries with %s placeholders."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "SQL query to execute (use %s for parameters)",
                    },
                    "params": {
                        "type": "array",
                        "items": {},
                        "description": "Query parameters (optional)",
                        "default": [],
                    },
                },
                "required": ["sql"],
            },
            handler=execute_sql,
        ),
        Tool(
            name="get_schema",
            description=(
                "Get the database schema: table names, column names, types, and defaults. "
                "Use this to understand the data model before writing queries."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=get_schema,
        ),
    ]

    return tools


def build_room_vault_tools(
    items: list[dict],
    access: AccessLevel,
    scope_fn: Callable[[str, list, list[dict]], dict] | None = None,
    scope_fn_source: str | None = None,
) -> list[Tool]:
    """Build tools for encrypted room-vault documents.

    Scope agents get full read access so they can write a scope function
    that handles room-vault rows. Query agents get only the rows allowed by
    that same scope function, mirroring ``execute_sql`` for table data.
    """
    if access == AccessLevel.NONE or not items:
        return []
    if access == AccessLevel.SCOPED and scope_fn is None:
        raise ValueError(
            "build_room_vault_tools(SCOPED, scope_fn=None) is unsafe: room "
            "vault rows require the room scope_fn before query-agent access."
        )

    room_rows = [
        {
            "item_id": str(item.get("item_id") or ""),
            "text": item.get("text") or "",
            "metadata": item.get("metadata") or {},
            "created_at": item.get("created_at"),
            "size_bytes": int(item.get("size_bytes") or 0),
        }
        for item in items
    ]

    def _serialize_rows(rows: list[dict]) -> str:
        out = json.dumps(rows, default=str)
        if len(out) <= MAX_RESULT_BYTES:
            return out
        keep = rows
        while keep and len(json.dumps(keep, default=str)) > MAX_RESULT_BYTES:
            keep = keep[: max(1, len(keep) // 2)]
        return json.dumps(
            {
                "rows": keep,
                "truncated": True,
                "original_row_count": len(rows),
                "returned_row_count": len(keep),
                "note": (
                    f"Output exceeded {MAX_RESULT_BYTES} bytes; truncated. "
                    "Use item_id filters or store smaller room data items."
                ),
            },
            default=str,
        )

    def get_room_vault_items(item_id: str | None = None) -> str:
        rows = room_rows
        requested = (item_id or "").strip()
        params: list = []
        sql = "SELECT item_id, text, metadata, created_at, size_bytes FROM room_vault_items"
        if requested:
            rows = [r for r in rows if r["item_id"] == requested]
            params = [requested]
            sql += " WHERE item_id = %s"

        if access == AccessLevel.SCOPED:
            from .scope import apply_scope_fn

            try:
                result = apply_scope_fn(
                    scope_fn,
                    sql,
                    params,
                    rows,
                    _source=scope_fn_source,
                )
            except Exception as e:
                logger.debug("Room data scope function error: %s", e)
                return json.dumps({"error": "Room data access denied by scope function"})
            if not result.get("allow", False):
                return json.dumps(
                    {
                        "error": result.get(
                            "error",
                            "Room data access denied by scope function",
                        )
                    }
                )
            rows = result.get("rows") or []

        return _serialize_rows(rows)

    return [
        Tool(
            name="get_room_vault_items",
            description=(
                "Read encrypted room items for this room. "
                "Scope agents see all rows. Query agents receive only rows "
                "allowed by the room scope function. Returns JSON rows with "
                "item_id, text, metadata, created_at, and size_bytes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Optional room data item id to fetch.",
                    },
                },
                "required": [],
            },
            handler=get_room_vault_items,
        )
    ]


def build_agent_file_tools(agent_store, query_agent_id: str) -> list[Tool]:
    """Build tools for scoping agents to inspect a query agent's source code."""

    def list_query_agent_files() -> str:
        files = agent_store.list_file_paths(query_agent_id)
        if not files:
            return json.dumps({
                "files": [],
                "note": "No source files extracted for this agent. "
                "The image may contain only compiled binaries.",
            })
        return json.dumps({"files": files})

    def read_query_agent_file(file_path: str) -> str:
        from .sandbox.agents import AgentSealedReadError

        try:
            content = agent_store.read_file(query_agent_id, file_path)
        except AgentSealedReadError:
            return (
                "This query agent is sealed (inspection_mode=sealed). "
                "Source files are encrypted for runtime-only use and cannot "
                "be inspected. Reason about the agent from its image digest, "
                "attested file list, and runtime SQL only."
            )
        if content is None:
            return "File not found. Use list_query_agent_files to see available files."
        return content

    return [
        Tool(
            name="list_query_agent_files",
            description=(
                "List all source files extracted from the query agent's Docker image. "
                "Returns file paths and sizes."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=list_query_agent_files,
        ),
        Tool(
            name="read_query_agent_file",
            description=(
                "Read the contents of a specific source file from the query agent's "
                "Docker image."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path of the file to read (from list_query_agent_files)",
                    },
                },
                "required": ["file_path"],
            },
            handler=read_query_agent_file,
        ),
    ]

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


def _is_select_only(sql: str) -> bool:
    """Check if SQL is a read-only statement using sqlglot AST parsing.

    Walks the full AST including CTEs and subqueries to reject DML hidden
    inside otherwise-SELECT statements.

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


MAX_RESULT_BYTES = 1_000_000  # cap serialized JSON output to ~1MB per call


def build_sql_tools(
    db: Database,
    access: AccessLevel,
    scope_fn: Callable[[str, list, list[dict]], dict] | None = None,
    scope_fn_source: str | None = None,
) -> list[Tool]:
    """Build SQL tools with the given access level.

    For SCOPED access, scope_fn is required — every query's results
    pass through it for filtering/transformation.

    If ``scope_fn_source`` is provided alongside ``scope_fn`` (the typical
    production path from ``Pipeline._run_query_agent``), the scope function
    is executed via ``apply_scope_fn`` in a child process with the
    ``SCOPE_FN_TIMEOUT`` hard kill. Without the source, we fall back to an
    in-process invocation — used by tests that pass plain Python functions.
    """
    if access == AccessLevel.NONE:
        return []

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

        # Block internal table access for non-system callers
        if access in (AccessLevel.FULL_READ, AccessLevel.SCOPED):
            if _references_internal_tables(sql):
                return json.dumps({"error": "Access to internal tables is denied"})
            if not _is_select_only(sql):
                return json.dumps({"error": "Only SELECT queries are allowed"})

        if access == AccessLevel.FULL_READWRITE:
            if _references_internal_tables(sql):
                # Allow reads but block writes to internal tables
                if not _is_select_only(sql):
                    return json.dumps({"error": "Write access to internal tables is denied"})

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
        # we call directly; the caller's fn is trusted Python.
        if access == AccessLevel.SCOPED and scope_fn is not None:
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
                "Source files are encrypted under the enclave-only key and "
                "cannot be inspected. Reason about the agent from its image "
                "digest, attested file list, and runtime SQL only."
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

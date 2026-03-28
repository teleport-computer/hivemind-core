"""Scope function compilation and evaluation — SQL query firewall.

Scope functions are produced by scope agents. They receive (sql, params, rows)
and return a dict controlling access: allow/deny/transform.

Security: scope functions execute in a restricted namespace with no IO,
no imports, and no dunder attribute access.
"""

from __future__ import annotations

import ast
import logging
import signal
import threading
from typing import Callable

logger = logging.getLogger(__name__)

# Safe builtins — no IO, no imports, no code generation
_SCOPE_BUILTINS: dict = {
    "True": True,
    "False": False,
    "None": None,
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "set": set,
    "tuple": tuple,
    "min": min,
    "max": max,
    "sum": sum,
    "sorted": sorted,
    "any": any,
    "all": all,
    "abs": abs,
    "round": round,
    "enumerate": enumerate,
    "zip": zip,
    "range": range,
    "isinstance": isinstance,
    "hasattr": hasattr,
    "getattr": getattr,
}

MAX_SCOPE_FN_LENGTH = 10_000
SCOPE_FN_TIMEOUT = 5  # seconds

_FORBIDDEN_CALLS = frozenset({
    "exec", "eval", "compile", "__import__", "open",
    "input", "breakpoint", "exit", "quit",
})


def compile_scope_fn(source: str) -> Callable[[str, list, list[dict]], dict]:
    """Compile a scope function source string into a callable.

    The source must define a function named ``scope`` that accepts
    (sql, params, rows) and returns a dict with 'allow' and 'rows'/'error'.

    Example::

        def scope(sql, params, rows):
            if "GROUP BY" not in sql.upper():
                return {"allow": False, "error": "Only aggregations allowed"}
            return {"allow": True, "rows": rows}

    Raises ValueError on invalid or unsafe source.
    """
    if not source or not source.strip():
        raise ValueError("Scope function source is empty")

    if len(source) > MAX_SCOPE_FN_LENGTH:
        raise ValueError(
            f"Scope function too long ({len(source)} > {MAX_SCOPE_FN_LENGTH} chars)"
        )

    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as e:
        raise ValueError(f"Scope function syntax error: {e}")

    # Must contain a top-level function def named 'scope'
    func_defs = [
        n
        for n in ast.iter_child_nodes(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "scope"
    ]
    if not func_defs:
        raise ValueError(
            "Scope function must define 'def scope(sql, params, rows): ...'"
        )

    # Validate / fix signature to exactly 3 parameters (sql, params, rows)
    scope_def = func_defs[0]
    args = scope_def.args
    n_params = len(args.args)
    if n_params != 3:
        # Auto-fix: pad missing params or trim extras so the function is callable
        desired = ["sql", "params", "rows"]
        scope_def.args.args = [ast.arg(arg=name) for name in desired]
        source = ast.unparse(tree)
        logger.info(
            "Auto-fixed scope function signature from %d to 3 params", n_params
        )
        # Re-parse to ensure the fix is valid
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            raise ValueError(f"Scope function syntax error after auto-fix: {e}")
        func_defs = [
            n for n in ast.iter_child_nodes(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "scope"
        ]
        scope_def = func_defs[0]

    # Safety checks
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("Scope functions cannot use imports")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _FORBIDDEN_CALLS:
                raise ValueError(
                    f"Scope functions cannot call '{node.func.id}'"
                )
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                raise ValueError(
                    f"Scope functions cannot access dunder attributes: "
                    f"{node.attr}"
                )

    namespace: dict = {"__builtins__": dict(_SCOPE_BUILTINS)}
    try:
        code = compile(tree, "<scope_fn>", "exec")
        exec(code, namespace)  # noqa: S102
    except Exception as e:
        raise ValueError(f"Scope function compilation failed: {e}")

    fn = namespace.get("scope")
    if not callable(fn):
        raise ValueError("'scope' must be a callable function")

    return fn


def apply_scope_fn(
    scope_fn: Callable[[str, list, list[dict]], dict],
    sql: str,
    params: list,
    rows: list[dict],
) -> dict:
    """Apply a scope function with fail-closed semantics.

    Returns a dict with:
      {"allow": True, "rows": [...]} on success
      {"allow": False, "error": "..."} on denial or error
    """
    try:
        result = scope_fn(sql, params, rows)
    except Exception as e:
        logger.debug("Scope function evaluation error: %s", e)
        return {"allow": False, "error": f"Scope function error: {e}"}

    if not isinstance(result, dict):
        return {"allow": False, "error": "Scope function must return a dict"}

    if "allow" not in result:
        return {"allow": False, "error": "Scope function result missing 'allow' key"}

    if not result["allow"]:
        error = result.get("error", "Query denied by scope function")
        return {"allow": False, "error": str(error)}

    if "rows" not in result:
        return {"allow": False, "error": "Scope function allowed but returned no rows"}

    if not isinstance(result["rows"], list):
        return {"allow": False, "error": "Scope function 'rows' must be a list"}

    return {"allow": True, "rows": result["rows"]}

"""Scope function compilation and evaluation — SQL query firewall.

Scope functions are produced by scope agents. They receive (sql, params, rows)
and return a dict controlling access: allow/deny/transform.

Security: scope functions execute in a restricted namespace with no IO,
no imports, and no dunder attribute access.
"""

from __future__ import annotations

import ast
import logging
import multiprocessing
import re
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
}

MAX_SCOPE_FN_LENGTH = 10_000
SCOPE_FN_TIMEOUT = 5  # seconds


# Default privacy-policy scope function. Used as a SAFE FALLBACK when the
# scope agent produces unparseable output or a scope_fn that fails AST
# validation (e.g. uses imports, wrong signature, forbidden calls).
#
# This is not a security downgrade: it IS the default privacy policy the
# scope agent is meant to enforce when no custom scenario is supplied.
# Falling back to it on LLM failure means benign queries still work while
# attacks that rely on the LLM producing a permissive scope are still
# blocked.
DEFAULT_SCOPE_FN_SOURCE = """\
def scope(sql, params, rows):
    s_up = sql.strip().upper()
    if not (s_up.startswith('SELECT') or s_up.startswith('WITH')):
        return {'allow': False, 'error': 'Only SELECT/WITH queries are allowed'}
    sl = sql.lower()
    token_extraction_markers = (
        'regexp_matches', 'regexp_split_to_table', 'regexp_split_to_array',
        'string_to_array', 'unnest(string_to_array',
    )
    if any(m in sl for m in token_extraction_markers):
        return {'allow': False, 'error': 'Token-extraction SQL patterns are blocked'}
    if rows:
        keys_lc = [str(k).lower() for k in rows[0].keys()]
        IDENT_COLS = (
            'name', 'title', 'subject', 'handle', 'username', 'user_name',
            'author', 'email', 'phone', 'address', 'id', 'uuid', 'identifier',
            'description', 'content', 'text', 'body', 'message',
            'first_name', 'last_name', 'display_name', 'full_name',
            'screen_name', 'nickname', 'conversation_title', 'msg',
        )
        bad = [
            k for k in keys_lc
            if k in IDENT_COLS
            or any(k.endswith('_' + t) for t in IDENT_COLS)
        ]
        if bad and len(rows) >= 2:
            return {'allow': False, 'error': 'Row-level identifier columns blocked: ' + str(bad)}
    if rows and len(rows) >= 3:
        for k in list(rows[0].keys()):
            sample = [r.get(k) for r in rows[:30]]
            sample = [str(v) for v in sample if v is not None]
            sample = [v for v in sample if 1 <= len(v) <= 80]
            if len(sample) >= 3:
                caps = sum(1 for v in sample if v[0:1].isupper())
                if caps >= 3:
                    return {'allow': False, 'error': 'Capitalized-token enumeration blocked'}
    has_agg = any(
        fn in sl for fn in (
            'count(', 'sum(', 'avg(', 'min(', 'max(', 'group by',
            'count (', 'sum (', 'avg (', 'min (', 'max (',
        )
    )
    if not has_agg and rows and len(rows) > 5:
        return {'allow': False, 'error': 'Non-aggregate row enumeration blocked; use COUNT/GROUP BY to get statistics'}
    return {'allow': True, 'rows': rows}
"""

_FORBIDDEN_CALLS = frozenset({
    "exec", "eval", "compile", "__import__", "open",
    "input", "breakpoint", "exit", "quit",
    "getattr", "setattr", "delattr", "hasattr",
    "vars", "dir", "globals", "locals", "type",
})

_DUNDER_RE = re.compile(r'__\w+__')

_DANGEROUS_ATTRS = frozenset({
    # Generator/coroutine internals
    "gi_frame", "gi_code", "gi_yieldfrom",
    "cr_frame", "cr_code", "cr_origin",
    "ag_frame", "ag_code",
    # Frame attributes
    "f_back", "f_builtins", "f_globals", "f_locals", "f_code", "f_trace",
    # Code object attributes
    "co_consts", "co_names", "co_code", "co_varnames", "co_freevars",
    # Function internals
    "func_globals", "func_code", "func_closure",
    # Traceback
    "tb_frame", "tb_next",
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
        # NEVER-DENY constraint (iter 17 experiment):
        # Reject any scope_fn that statically returns {"allow": False, ...}.
        # The privacy boundary is at the ROWS, not at the SQL — forcing
        # scope to write row-transforming code instead of SQL-shape gates.
        # A scope_fn that wants to "block" a query should instead return
        # {"allow": True, "rows": [{"match_count": len(rows)}]} (aggregate)
        # or {"allow": True, "rows": <redacted>} (mask identifying fields).
        if isinstance(node, ast.Dict):
            for key_node, value_node in zip(node.keys, node.values):
                if (
                    isinstance(key_node, ast.Constant)
                    and key_node.value == "allow"
                    and isinstance(value_node, ast.Constant)
                    and value_node.value is False
                ):
                    raise ValueError(
                        "Scope functions must transform rows, not deny "
                        "queries. Found a literal {'allow': False, ...} "
                        "return — remove it. Return {'allow': True, "
                        "'rows': [{'match_count': len(rows)}]} to "
                        "aggregate, or {'allow': True, 'rows': [...]} "
                        "with identifying fields redacted. The privacy "
                        "boundary is at the rows, not the SQL text."
                    )
        # Block class definitions — too many implicit code execution vectors
        if isinstance(node, ast.ClassDef):
            raise ValueError("Scope functions cannot define classes")
        # Block dunder method definitions (except 'scope' itself)
        if isinstance(node, ast.FunctionDef) and node.name != "scope":
            if node.name.startswith("__") and node.name.endswith("__"):
                raise ValueError(
                    f"Scope functions cannot define dunder methods: {node.name}"
                )
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                raise ValueError(
                    f"Scope functions cannot access dunder attributes: "
                    f"{node.attr}"
                )
            # Block dangerous internal attributes (frame, code, generator internals)
            if node.attr in _DANGEROUS_ATTRS:
                raise ValueError(
                    f"Scope functions cannot access internal attribute: {node.attr}"
                )
            # Block private/underscore-prefixed attributes
            if node.attr.startswith("_") and node.attr != "_":
                raise ValueError(
                    f"Scope functions cannot access private attributes: {node.attr}"
                )
        # Block dunder patterns anywhere in string constants (e.g. "{0.__class__}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _DUNDER_RE.search(node.value):
                raise ValueError(
                    f"Scope functions cannot reference dunder names in strings"
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


def _run_scope_in_process(
    source: str, sql: str, params: list, rows: list[dict],
    result_queue: multiprocessing.Queue,
) -> None:
    """Target function for scope subprocess. Re-compiles and runs the scope fn."""
    try:
        namespace: dict = {"__builtins__": dict(_SCOPE_BUILTINS)}
        code = compile(ast.parse(source, mode="exec"), "<scope_fn>", "exec")
        exec(code, namespace)  # noqa: S102
        fn = namespace.get("scope")
        if not callable(fn):
            result_queue.put({"_error": "'scope' is not callable"})
            return
        result_queue.put(fn(sql, params, rows))
    except Exception as e:
        result_queue.put({"_error": str(e)})


def apply_scope_fn(
    scope_fn: Callable[[str, list, list[dict]], dict],
    sql: str,
    params: list,
    rows: list[dict],
    *,
    _source: str | None = None,
) -> dict:
    """Apply a scope function with fail-closed semantics.

    Runs the scope function in a child process with a hard timeout.
    The process is killed if it exceeds the deadline, preventing infinite
    loops from hanging the worker.

    If ``_source`` is provided, the function is re-compiled in the child
    process (avoids pickle issues with exec'd functions). Otherwise falls
    back to direct invocation in-process.

    Returns a dict with:
      {"allow": True, "rows": [...]} on success
      {"allow": False, "error": "..."} on denial or error
    """
    if _source:
        q: multiprocessing.Queue = multiprocessing.Queue()
        p = multiprocessing.Process(
            target=_run_scope_in_process,
            args=(_source, sql, params, rows, q),
            daemon=True,
        )
        p.start()
        p.join(timeout=SCOPE_FN_TIMEOUT)

        if p.is_alive():
            p.kill()
            p.join(timeout=1)
            logger.warning("Scope function timed out after %ss", SCOPE_FN_TIMEOUT)
            return {"allow": False, "error": f"Scope function timed out ({SCOPE_FN_TIMEOUT}s)"}

        try:
            result = q.get_nowait()
        except Exception:
            return {"allow": False, "error": "Scope function returned no result"}

        if isinstance(result, dict) and "_error" in result:
            logger.debug("Scope function evaluation error: %s", result["_error"])
            return {"allow": False, "error": f"Scope function error: {result['_error']}"}
    else:
        # Fallback: run in-process (no source available for subprocess)
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

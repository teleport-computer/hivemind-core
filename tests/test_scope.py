"""Tests for scope function compilation and evaluation — SQL query firewall."""

import pytest

from hivemind.scope import compile_scope_fn, apply_scope_fn


class TestCompileScopeFn:
    def test_allow_all(self):
        fn = compile_scope_fn(
            "def scope(sql, params, rows):\n"
            "    return {'allow': True, 'rows': rows}"
        )
        rows = [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]
        result = fn("SELECT * FROM users", [], rows)
        assert result["allow"] is True
        assert result["rows"] == rows

    def test_aggregate_replacement(self):
        """Canonical never-deny pattern: replace per-row results with an aggregate."""
        fn = compile_scope_fn(
            "def scope(sql, params, rows):\n"
            "    if 'GROUP BY' not in sql.upper() and len(rows) > 1:\n"
            "        return {'allow': True, 'rows': [{'match_count': len(rows)}]}\n"
            "    return {'allow': True, 'rows': rows}"
        )
        rows = [{"id": 1}, {"id": 2}]
        result = fn("SELECT * FROM users", [], rows)
        assert result["allow"] is True
        assert result["rows"] == [{"match_count": 2}]

        result = fn("SELECT COUNT(*) FROM users GROUP BY dept", [], [{"count": 5}])
        assert result["allow"] is True
        assert result["rows"] == [{"count": 5}]

    def test_k_anonymity_filter(self):
        fn = compile_scope_fn(
            "def scope(sql, params, rows):\n"
            "    filtered = [r for r in rows if r.get('count', 999) >= 5]\n"
            "    return {'allow': True, 'rows': filtered}"
        )
        rows = [{"group": "A", "count": 10}, {"group": "B", "count": 3}]
        result = fn("SELECT group, COUNT(*) FROM t GROUP BY group", [], rows)
        assert result["allow"] is True
        assert len(result["rows"]) == 1
        assert result["rows"][0]["group"] == "A"

    def test_column_redaction(self):
        fn = compile_scope_fn(
            "def scope(sql, params, rows):\n"
            "    redacted = []\n"
            "    for row in rows:\n"
            "        r = dict(row)\n"
            "        if 'ssn' in r:\n"
            "            r['ssn'] = '***-**-****'\n"
            "        redacted.append(r)\n"
            "    return {'allow': True, 'rows': redacted}"
        )
        rows = [{"name": "Alice", "ssn": "123-45-6789"}]
        result = fn("SELECT * FROM users", [], rows)
        assert result["allow"] is True
        assert result["rows"][0]["ssn"] == "***-**-****"

    def test_sql_aware_aggregation(self):
        """SELECT * gets aggregated to a row count instead of returning rows."""
        fn = compile_scope_fn(
            "def scope(sql, params, rows):\n"
            "    upper = sql.upper()\n"
            "    if 'SELECT *' in upper:\n"
            "        return {'allow': True, 'rows': [{'count': len(rows)}]}\n"
            "    return {'allow': True, 'rows': rows}"
        )
        result = fn("SELECT * FROM users", [], [{"id": 1}, {"id": 2}])
        assert result["allow"] is True
        assert result["rows"] == [{"count": 2}]

        result = fn("SELECT id, name FROM users", [], [{"id": 1}])
        assert result["allow"] is True
        assert result["rows"] == [{"id": 1}]

    def test_helper_functions_allowed(self):
        fn = compile_scope_fn(
            "def safe_rows(rows):\n"
            "    return [{'id': r.get('id')} for r in rows]\n\n"
            "def scope(sql, params, rows):\n"
            "    return {'allow': True, 'rows': safe_rows(rows)}"
        )
        result = fn("SELECT * FROM users", [], [{"id": 1, "ssn": "x"}])
        assert result["allow"] is True
        assert result["rows"] == [{"id": 1}]

    def test_builtins_available(self):
        fn = compile_scope_fn(
            "def scope(sql, params, rows):\n"
            "    return {'allow': True, 'rows': sorted(rows, key=lambda r: r.get('id', 0))}"
        )
        rows = [{"id": 3}, {"id": 1}]
        result = fn("SELECT * FROM t", [], rows)
        assert result["rows"] == [{"id": 1}, {"id": 3}]


class TestCompileScopeFnRejections:
    def test_empty_source(self):
        with pytest.raises(ValueError, match="empty"):
            compile_scope_fn("")

    def test_whitespace_only(self):
        with pytest.raises(ValueError, match="empty"):
            compile_scope_fn("   \n  ")

    def test_too_long(self):
        with pytest.raises(ValueError, match="too long"):
            compile_scope_fn("def scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}\n" + " " * 20_000)

    def test_syntax_error(self):
        with pytest.raises(ValueError, match="syntax error"):
            compile_scope_fn("def scope(sql, params, rows)\n    return True")

    def test_no_scope_function(self):
        with pytest.raises(ValueError, match="must define"):
            compile_scope_fn("def other(sql, params, rows):\n    return True")

    def test_wrong_number_of_args_rejected(self):
        """Scope functions with wrong arity are rejected — earlier auto-fix
        silently corrupted the body (params kept original names while signature
        changed)."""
        with pytest.raises(ValueError, match="exactly 3 parameters"):
            compile_scope_fn(
                "def scope(sql, rows):\n"
                "    return {'allow': True, 'rows': rows}"
            )

    def test_wrong_param_names_rejected(self):
        with pytest.raises(ValueError, match="must be named"):
            compile_scope_fn(
                "def scope(query, p, data):\n"
                "    return {'allow': True, 'rows': data}"
            )

    def test_yield_rejected(self):
        with pytest.raises(ValueError, match="Yield"):
            compile_scope_fn(
                "def scope(sql, params, rows):\n"
                "    yield rows"
            )

    def test_async_def_rejected(self):
        with pytest.raises(ValueError, match="AsyncFunctionDef"):
            compile_scope_fn(
                "async def scope(sql, params, rows):\n"
                "    return {'allow': True, 'rows': rows}"
            )

    def test_global_rejected(self):
        with pytest.raises(ValueError, match="Global"):
            compile_scope_fn(
                "def scope(sql, params, rows):\n"
                "    global x\n"
                "    return {'allow': True, 'rows': rows}"
            )

    def test_module_level_assignment_rejected(self):
        with pytest.raises(ValueError, match="module scope"):
            compile_scope_fn(
                "X = 1\n"
                "def scope(sql, params, rows):\n"
                "    return {'allow': True, 'rows': rows}"
            )

    def test_module_docstring_allowed(self):
        # Triple-quoted module docstring at the top is harmless.
        fn = compile_scope_fn(
            '"""Module docstring."""\n'
            'def scope(sql, params, rows):\n'
            '    return {"allow": True, "rows": rows}'
        )
        assert fn("SELECT 1", [], [{"a": 1}]) == {"allow": True, "rows": [{"a": 1}]}

    def test_import_rejected(self):
        with pytest.raises(ValueError, match="imports"):
            compile_scope_fn(
                "import os\ndef scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}"
            )

    def test_from_import_rejected(self):
        with pytest.raises(ValueError, match="imports"):
            compile_scope_fn(
                "from os import path\ndef scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}"
            )

    def test_exec_rejected(self):
        with pytest.raises(ValueError, match="exec"):
            compile_scope_fn(
                "def scope(sql, params, rows):\n    exec('x=1')\n    return {'allow': True, 'rows': rows}"
            )

    def test_eval_rejected(self):
        with pytest.raises(ValueError, match="eval"):
            compile_scope_fn(
                "def scope(sql, params, rows):\n    return eval('True')"
            )

    def test_open_rejected(self):
        with pytest.raises(ValueError, match="open"):
            compile_scope_fn(
                "def scope(sql, params, rows):\n    open('/etc/passwd')\n    return {'allow': True, 'rows': rows}"
            )

    def test_dunder_access_rejected(self):
        with pytest.raises(ValueError, match="dunder"):
            compile_scope_fn(
                "def scope(sql, params, rows):\n    return rows.__class__.__name__"
            )

    def test_literal_deny_rejected(self):
        """Static check forbids literal {'allow': False, ...} returns —
        scope must transform rows, not gate on SQL shape."""
        with pytest.raises(ValueError, match="transform rows"):
            compile_scope_fn(
                "def scope(sql, params, rows):\n"
                "    return {'allow': False, 'error': 'nope'}"
            )

    def test_literal_deny_rejected_in_branch(self):
        """Even a deny inside a conditional branch is caught — the AST walker
        sees every Dict literal regardless of reachability."""
        with pytest.raises(ValueError, match="transform rows"):
            compile_scope_fn(
                "def scope(sql, params, rows):\n"
                "    if len(rows) > 100:\n"
                "        return {'allow': False, 'error': 'too many'}\n"
                "    return {'allow': True, 'rows': rows}"
            )


class TestApplyScopeFn:
    def test_allow_passthrough(self):
        fn = compile_scope_fn(
            "def scope(sql, params, rows):\n"
            "    return {'allow': True, 'rows': rows}"
        )
        result = apply_scope_fn(fn, "SELECT 1", [], [{"a": 1}])
        assert result["allow"] is True
        assert result["rows"] == [{"a": 1}]

    def test_runtime_deny(self):
        """Computed (non-literal) allow=False still passes the static check
        and is honored at runtime by apply_scope_fn."""
        fn = compile_scope_fn(
            "def scope(sql, params, rows):\n"
            "    blocked = True\n"
            "    return {'allow': not blocked, 'error': 'nope'}"
        )
        result = apply_scope_fn(fn, "SELECT 1", [], [])
        assert result["allow"] is False
        assert "nope" in result["error"]

    def test_exception_fails_closed(self):
        fn = compile_scope_fn(
            "def scope(sql, params, rows):\n"
            "    raise RuntimeError('boom')"
        )
        result = apply_scope_fn(fn, "SELECT 1", [], [])
        assert result["allow"] is False
        assert "error" in result

    def test_invalid_return_type_fails_closed(self):
        fn = compile_scope_fn(
            "def scope(sql, params, rows):\n"
            "    return True"
        )
        result = apply_scope_fn(fn, "SELECT 1", [], [])
        assert result["allow"] is False

    def test_missing_allow_key_fails_closed(self):
        fn = compile_scope_fn(
            "def scope(sql, params, rows):\n"
            "    return {'rows': rows}"
        )
        result = apply_scope_fn(fn, "SELECT 1", [], [])
        assert result["allow"] is False

    def test_allow_without_rows_fails_closed(self):
        fn = compile_scope_fn(
            "def scope(sql, params, rows):\n"
            "    return {'allow': True}"
        )
        result = apply_scope_fn(fn, "SELECT 1", [], [])
        assert result["allow"] is False


class TestScopeSecurityBypass:
    """Regression tests for scope sandbox escape vectors."""

    def test_format_string_dunder_rejected(self):
        with pytest.raises(ValueError, match="dunder"):
            compile_scope_fn(
                'def scope(sql, params, rows):\n'
                '  return "{0.__class__}".format("")'
            )

    def test_class_definition_rejected(self):
        with pytest.raises(ValueError, match="class"):
            compile_scope_fn(
                'def scope(sql, params, rows):\n'
                '  class X(int): pass\n'
                '  return {"allow": True, "rows": rows}'
            )

    def test_gi_frame_rejected(self):
        with pytest.raises(ValueError, match="internal attribute"):
            compile_scope_fn(
                'def scope(sql, params, rows):\n'
                '  def g(): yield 1\n'
                '  x=g()\n'
                '  f=x.gi_frame\n'
                '  return {"allow":True,"rows":rows}'
            )

    def test_f_back_rejected(self):
        with pytest.raises(ValueError, match="internal attribute"):
            compile_scope_fn(
                'def scope(sql, params, rows):\n'
                '  x = rows\n'
                '  y = x.f_back\n'
                '  return {"allow":True,"rows":rows}'
            )

    def test_underscore_attr_rejected(self):
        with pytest.raises(ValueError, match="private"):
            compile_scope_fn(
                'def scope(sql, params, rows):\n'
                '  x = rows._hidden\n'
                '  return {"allow":True,"rows":rows}'
            )

    def test_dunder_method_def_rejected(self):
        with pytest.raises(ValueError, match="dunder methods"):
            compile_scope_fn(
                'def scope(sql, params, rows):\n'
                '  def __secret__(): pass\n'
                '  return {"allow":True,"rows":rows}'
            )

    def test_nested_dunder_string_rejected(self):
        """Format strings with dunders anywhere in the string are caught."""
        with pytest.raises(ValueError, match="dunder"):
            compile_scope_fn(
                'def scope(sql, params, rows):\n'
                '  x = "foo.__init__bar"\n'
                '  return {"allow":True,"rows":rows}'
            )

    def test_func_code_rejected(self):
        with pytest.raises(ValueError, match="internal attribute"):
            compile_scope_fn(
                'def scope(sql, params, rows):\n'
                '  def f(): pass\n'
                '  c = f.func_code\n'
                '  return {"allow":True,"rows":rows}'
            )

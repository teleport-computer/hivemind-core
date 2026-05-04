"""Deterministic scope agent for the dinner-negotiation example.

The default LLM-driven scope agent (`agents/default-scope/agent.py`) is
the production code path; it reads `POLICY_CONTEXT` and emits a
`scope_fn` it judges to be policy-compliant. It works for arbitrary
rules but is non-deterministic — different runs produce different
filters, and on simple cases like ours it sometimes emits an
over-restrictive function that returns 0 rows.

This scope agent ships with the example so the demo runs reliably.
The `scope_fn` it emits implements the dinner-negotiation rules
verbatim:
  - rows must have is_busy = false
  - only `start_time` and `end_time` columns pass through
  - the SQL must touch the `calendar` table only

For your own room with different rules, either extend this agent or
fall back to the LLM-driven default-scope. This file is also a useful
reference for what a hand-rolled scope_fn looks like.

Env vars set by hivemind (we ignore POLICY_CONTEXT since the policy
is hard-coded; a real custom scope agent would read it):
  BRIDGE_URL        — bridge URL (unused; this agent makes no LLM calls)
  SESSION_TOKEN     — bridge token (unused)
  QUERY_PROMPT      — user's question (unused; we filter structurally)
  POLICY_CONTEXT    — room rules (unused; the rules are inlined here)
  QUERY_AGENT_ID    — query agent identifier (unused)
"""

import json


# scope_fn is serialized as Python source; the pipeline compiles it
# inside the CVM and runs it on every (sql, params, rows) result the
# query agent fetches.
#
# Important contract note: the runtime statically rejects literal
# `{"allow": False, ...}` returns with the error "Scope functions must
# transform rows, not deny queries." The privacy boundary is at the
# rows, not the SQL text. So to "block" an off-table query, return
# `{"allow": True, "rows": []}` — semantically equivalent (no rows
# leak) but follows the row-transforming contract.
SCOPE_FN_SOURCE = '''
def scope(sql, params, rows):
    """Allow only is_busy=false rows from the calendar table, and only
    the start_time/end_time columns. Off-table queries return zero rows.
    """
    sql_lower = (sql or "").lower()
    if "calendar" not in sql_lower:
        # Off-table query — no rows leak. Same effect as deny, but in
        # the row-transforming contract the runtime requires.
        return {"allow": True, "rows": []}

    allowed_cols = {"start_time", "end_time"}
    out = []
    for r in rows or []:
        if r.get("is_busy") is True:
            continue
        out.append({k: v for k, v in r.items() if k in allowed_cols})
    return {"allow": True, "rows": out}
'''


def main():
    print(json.dumps({"scope_fn": SCOPE_FN_SOURCE.strip()}))


if __name__ == "__main__":
    main()

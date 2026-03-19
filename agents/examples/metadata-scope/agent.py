"""Scope agent that writes a team-based scope function.

Outputs a scope function that filters SQL query results by the caller's
team. Only rows where a 'team' column matches the caller's team pass
through. If no team is specified in caller_context, all results are allowed.

This shows how to implement access control using scope functions.

Env vars (set by hivemind):
  BRIDGE_URL       — HTTP endpoint for the bridge server
  SESSION_TOKEN    — Bearer token for bridge auth
  QUERY_PROMPT     — The user's question (for context, not used here)
  CALLER_CONTEXT   — JSON with caller info, e.g. {"user_id": "alice", "team": "payments"}
  QUERY_AGENT_ID   — Which query agent will process (not used here)
"""

import json
import os


CALLER_CONTEXT = os.environ.get("CALLER_CONTEXT", "{}")


def main():
    # Parse caller context
    try:
        ctx = json.loads(CALLER_CONTEXT)
    except json.JSONDecodeError:
        ctx = {}

    caller_team = ctx.get("team", "")

    if not caller_team:
        # No team filter — allow everything
        scope_fn = (
            "def scope(sql, params, rows):\n"
            "    return {'allow': True, 'rows': rows}"
        )
    else:
        # Filter results to only rows where 'team' column matches caller's team
        scope_fn = (
            f"def scope(sql, params, rows):\n"
            f"    filtered = [r for r in rows if r.get('team') == {caller_team!r}]\n"
            f"    return {{'allow': True, 'rows': filtered}}"
        )

    print(json.dumps({"scope_fn": scope_fn}))


if __name__ == "__main__":
    main()

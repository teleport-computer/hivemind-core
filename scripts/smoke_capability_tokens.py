"""Post-deploy smoke test for capability tokens.

Run after pushing a new core CVM image. Walks the full lifecycle for
both query and write tokens against a live deployment using the active
profile's owner key:

    1. /v1/tokens — issue a query token (pinned to a scope agent id)
    2. /v1/tokens — issue a write token (pinned to a table allowlist)
    3. /v1/tokens — list shows both, no plaintext
    4. /v1/store with the write token — INSERT into allowed table OK
    5. /v1/store with the write token — INSERT into other table 403
    6. /v1/scope-attest with the query token — returns the bound agent
    7. /v1/tokens — revoke both
    8. resolve_any rejects revoked tokens (re-use returns 401)

Usage::

    HIVEMIND_PROFILE=watchhistory \
        SCOPE_AGENT_ID=<id> \
        ALLOWED_TABLE=watch_history \
        uv run python scripts/smoke_capability_tokens.py

Environment knobs:
  - HIVEMIND_PROFILE  → which profile to read service+api_key from
  - SCOPE_AGENT_ID    → existing scope agent the query token will pin
  - ALLOWED_TABLE     → table name the write token may insert into
  - SKIP_INSERT       → "1" to skip the actual store write (read-only smoke)

Exits 0 on success, non-zero with a per-step message on the first
failure. Designed to be safe to re-run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import yaml


def _profile_path(name: str) -> Path:
    return Path.home() / ".hivemind" / "profiles" / f"{name}.yaml"


def _load_profile() -> tuple[str, str]:
    name = os.environ.get("HIVEMIND_PROFILE", "default")
    p = _profile_path(name)
    if not p.exists():
        sys.exit(f"profile '{name}' not found at {p}")
    cfg = yaml.safe_load(p.read_text()) or {}
    service = (cfg.get("service") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    if not service or not api_key:
        sys.exit(f"profile '{name}' missing service / api_key")
    return service, api_key


def main() -> int:
    service, owner_key = _load_profile()
    scope_agent_id = os.environ.get("SCOPE_AGENT_ID", "").strip()
    allowed_table = os.environ.get("ALLOWED_TABLE", "watch_history").strip()
    skip_insert = os.environ.get("SKIP_INSERT", "") == "1"

    if not scope_agent_id:
        sys.exit("SCOPE_AGENT_ID is required (existing scope agent id)")

    H_OWNER = {"Authorization": f"Bearer {owner_key}"}

    with httpx.Client(verify=False, timeout=30) as c:
        # 1. issue query token
        r = c.post(
            f"{service}/v1/tokens",
            headers=H_OWNER,
            json={
                "kind": "query",
                "label": "smoke-query",
                "constraints": {"scope_agent_id": scope_agent_id},
            },
        )
        r.raise_for_status()
        q = r.json()
        print(f"[1] query token issued: id={q['token_id']}")

        # 2. issue write token
        r = c.post(
            f"{service}/v1/tokens",
            headers=H_OWNER,
            json={
                "kind": "write",
                "label": "smoke-write",
                "constraints": {"allowed_tables": [allowed_table]},
            },
        )
        r.raise_for_status()
        w = r.json()
        print(f"[2] write token issued: id={w['token_id']}")

        # 3. list
        r = c.get(f"{service}/v1/tokens", headers=H_OWNER)
        r.raise_for_status()
        rows = r.json()["tokens"]
        ids = {row["token_id"] for row in rows}
        assert q["token_id"] in ids and w["token_id"] in ids
        print(f"[3] list returned {len(rows)} tokens")

        # 4. write token → /v1/store INSERT into allowed table
        if not skip_insert:
            r = c.post(
                f"{service}/v1/store",
                headers={"Authorization": f"Bearer {w['token']}"},
                json={
                    "sql": f"INSERT INTO {allowed_table} DEFAULT VALUES",
                    "params": [],
                },
            )
            # The DB may reject `DEFAULT VALUES` on tables without nullable
            # cols. We're verifying the auth path — anything that's not 401/
            # 403 means the gate let us in. 400 from sqlglot/postgres is OK.
            assert r.status_code not in (401, 403), (
                f"write token blocked unexpectedly: {r.status_code} {r.text}"
            )
            print(f"[4] write→allowed table: HTTP {r.status_code} (auth path OK)")
        else:
            print("[4] SKIP_INSERT=1 — skipped real store write")

        # 5. write token → /v1/store INSERT into a foreign table (must 403)
        r = c.post(
            f"{service}/v1/store",
            headers={"Authorization": f"Bearer {w['token']}"},
            json={
                "sql": "INSERT INTO _hivemind_query_runs (id) VALUES ('x')",
                "params": [],
            },
        )
        assert r.status_code == 403, (
            f"expected 403 for forbidden table, got {r.status_code}: {r.text}"
        )
        print("[5] write→internal table: 403 ✓")

        # 6. query token → /v1/scope-attest
        r = c.get(
            f"{service}/v1/scope-attest",
            headers={"Authorization": f"Bearer {q['token']}"},
        )
        r.raise_for_status()
        att = r.json()
        assert att["scope_agent_id"] == scope_agent_id
        digest = att["files_digest_sha256"]
        print(
            f"[6] scope-attest: agent={att['scope_agent_id']} "
            f"files={att['files_count']} digest={digest[:12]}…"
        )

        # 7. revoke both
        for tid in (q["token_id"], w["token_id"]):
            r = c.delete(
                f"{service}/v1/tokens/{tid}", headers=H_OWNER,
            )
            r.raise_for_status()
        print("[7] revoked both tokens")

        # 8. revoked tokens → 401
        for label, tok in (("query", q["token"]), ("write", w["token"])):
            r = c.get(
                f"{service}/v1/health",
                headers={"Authorization": f"Bearer {tok}"},
            )
            assert r.status_code == 401, (
                f"revoked {label} token still authenticates: {r.status_code}"
            )
        print("[8] revoked tokens rejected with 401 ✓")

    print("\nALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

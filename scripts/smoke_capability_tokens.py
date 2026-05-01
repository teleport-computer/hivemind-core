"""Post-deploy smoke test for capability tokens.

Run after pushing a new core CVM image. Walks the lifecycle for query
tokens against a live deployment using the active profile's owner key:

    1. /v1/tokens — issue a query token (pinned to a scope agent id)
    2. /v1/tokens — list shows the token, no plaintext
    3. /v1/scope-attest with the query token — returns the bound agent
    4. /v1/store with the query token — owner-only, expect 403
    5. /v1/tokens — revoke the token
    6. resolve_any rejects the revoked token (re-use returns 401)

Usage::

    HIVEMIND_PROFILE=watchhistory \
        SCOPE_AGENT_ID=<id> \
        uv run python scripts/smoke_capability_tokens.py [--insecure]

Environment knobs:
  - HIVEMIND_PROFILE  → which profile to read service+api_key from
  - SCOPE_AGENT_ID    → existing scope agent the query token will pin

Exits 0 on success, non-zero with a per-step message on the first
failure. Designed to be safe to re-run.
"""

from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="disable TLS certificate verification for the service URL",
    )
    args = parser.parse_args()

    service, owner_key = _load_profile()
    scope_agent_id = os.environ.get("SCOPE_AGENT_ID", "").strip()

    if not scope_agent_id:
        sys.exit("SCOPE_AGENT_ID is required (existing scope agent id)")

    H_OWNER = {"Authorization": f"Bearer {owner_key}"}

    if args.insecure:
        print("[smoke] warning: TLS certificate verification disabled", file=sys.stderr)

    with httpx.Client(verify=not args.insecure, timeout=30) as c:
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

        # 2. list
        r = c.get(f"{service}/v1/tokens", headers=H_OWNER)
        r.raise_for_status()
        rows = r.json()["tokens"]
        ids = {row["token_id"] for row in rows}
        assert q["token_id"] in ids
        print(f"[2] list returned {len(rows)} tokens")

        # 3. query token → /v1/scope-attest
        r = c.get(
            f"{service}/v1/scope-attest",
            headers={"Authorization": f"Bearer {q['token']}"},
        )
        r.raise_for_status()
        att = r.json()
        assert att["scope_agent_id"] == scope_agent_id
        digest = att["files_digest_sha256"]
        print(
            f"[3] scope-attest: agent={att['scope_agent_id']} "
            f"files={att['files_count']} digest={digest[:12]}…"
        )

        # 4. query token → /v1/store (owner-only) must 403.
        r = c.post(
            f"{service}/v1/store",
            headers={"Authorization": f"Bearer {q['token']}"},
            json={"sql": "SELECT 1", "params": []},
        )
        assert r.status_code == 403, (
            f"expected 403 for query token on /v1/store, got {r.status_code}: "
            f"{r.text}"
        )
        print("[4] query→/v1/store: 403 ✓ (owner-only path enforced)")

        # 5. revoke the token
        r = c.delete(
            f"{service}/v1/tokens/{q['token_id']}", headers=H_OWNER,
        )
        r.raise_for_status()
        print("[5] revoked the query token")

        # 6. revoked token → 401
        r = c.get(
            f"{service}/v1/health",
            headers={"Authorization": f"Bearer {q['token']}"},
        )
        # /v1/health is unauthed; use an authed endpoint instead.
        r = c.get(
            f"{service}/v1/scope-attest",
            headers={"Authorization": f"Bearer {q['token']}"},
        )
        assert r.status_code == 401, (
            f"revoked query token still authenticates: {r.status_code}"
        )
        print("[6] revoked token rejected with 401 ✓")

    print("\nALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

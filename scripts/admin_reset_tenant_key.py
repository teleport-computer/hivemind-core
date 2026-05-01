#!/usr/bin/env python3
"""Reset (or mint) an API key for an existing tenant DB, as admin.

Use case: a pre-existing per-tenant Postgres database (e.g., the legacy
``tenant_t_234bab3e4bf1`` watch-history DB carried over a cluster
migration) needs to be made usable through hivemind-core. The admin
holds ``HIVEMIND_ADMIN_KEY`` and ``SQL_PROXY_KEY`` but cannot recover
the original ``hmk_`` plaintext (only its SHA-256 lives in the control
DB).

The /v1/tenant/rotate-key endpoint requires the *current* hmk_, so the
admin can't use it. ``/v1/admin/tenants`` also doesn't have a
"reset-key" verb. This script bridges that gap by:

  1. SELECTing the tenant row from ``hivemind_control._tenants`` to
     confirm it exists (or seeing it doesn't).
  2. If it exists: generate a fresh ``hmk_``, hash it, and UPDATE the
     ``api_key_hash`` column on the same row. Tenant_id is preserved,
     which keeps the per-tenant Postgres role + password derivation
     aligned with the existing DB.
  3. If it doesn't exist (and ``--register`` is set): INSERT a fresh row
     pointing at the supplied db_name with the new hmk_'s hash.

The fresh ``hmk_`` is written once through ``--output-file`` or, for
interactive local use only, printed with ``--print-key``. It will not be
retrievable later.

Env (required):
  HIVEMIND_BASE_URL    — sql_proxy base URL (port-8080 gateway URL)
  SQL_PROXY_KEY        — data-plane key (X-Proxy-Key header)

Args:
  --tenant-id      e.g. t_234bab3e4bf1
  --db-name        e.g. tenant_t_234bab3e4bf1 (only needed for --register)
  --name           friendly name for the row (default: derived)
  --register       if no existing row, INSERT one (off by default — fail
                   loudly so we don't silently create new tenants)
  --control-db     control DB name (default: hivemind_control)
  --output-file    write the fresh hmk_ to this chmod-600 file
  --print-key      explicitly print the fresh hmk_ to stdout
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import secrets
import sys
import time

import httpx


def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _new_api_key() -> str:
    return "hmk_" + secrets.token_urlsafe(32)


def _proxy_post(
    base: str, path: str, *, proxy_key: str, tenant_db: str, body: dict
) -> dict:
    url = f"{base.rstrip('/')}{path}"
    r = httpx.post(
        url,
        headers={
            "X-Proxy-Key": proxy_key,
            "X-Tenant-DB": tenant_db,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30.0,
    )
    if r.status_code != 200:
        raise RuntimeError(f"{path} -> {r.status_code}: {r.text}")
    return r.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--db-name")
    parser.add_argument("--name", default="")
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--control-db", default="hivemind_control")
    parser.add_argument("--output-file", default="")
    parser.add_argument(
        "--print-key",
        action="store_true",
        help="print the fresh hmk_ to stdout; unsafe in CI logs",
    )
    args = parser.parse_args()

    base = os.environ.get("HIVEMIND_BASE_URL")
    proxy_key = os.environ.get("SQL_PROXY_KEY")
    if not base or not proxy_key:
        print(
            "[reset] FAIL: set HIVEMIND_BASE_URL and SQL_PROXY_KEY",
            file=sys.stderr,
        )
        return 2

    tenant_id = args.tenant_id.strip()
    if not tenant_id.startswith("t_"):
        print(f"[reset] FAIL: tenant_id must start with 't_': {tenant_id}",
              file=sys.stderr)
        return 2
    if not args.output_file and not args.print_key:
        print(
            "[reset] FAIL: choose --output-file PATH or --print-key. "
            "Refusing to emit an owner key implicitly.",
            file=sys.stderr,
        )
        return 2

    # 1. probe — does the row exist already?
    r = _proxy_post(
        base, "/execute",
        proxy_key=proxy_key, tenant_db=args.control_db,
        body={
            "sql": "SELECT id, name, db_name FROM _tenants WHERE id = %s",
            "params": [tenant_id],
        },
    )
    rows = r.get("rows") or []

    new_key = _new_api_key()
    new_hash = _hash_api_key(new_key)

    if rows:
        existing = rows[0]
        print(
            f"[reset] tenant row found: id={existing['id']} "
            f"name={existing['name']!r} db_name={existing['db_name']}"
        )
        # 2. UPDATE in place — preserves tenant_id, db_name, role binding.
        r = _proxy_post(
            base, "/execute_commit",
            proxy_key=proxy_key, tenant_db=args.control_db,
            body={
                "sql": "UPDATE _tenants SET api_key_hash = %s WHERE id = %s",
                "params": [new_hash, tenant_id],
            },
        )
        if r.get("rowcount") != 1:
            print(f"[reset] FAIL: UPDATE rowcount={r.get('rowcount')} "
                  "(expected 1)", file=sys.stderr)
            return 1
        print("[reset] api_key_hash UPDATEd in place")
    else:
        if not args.register:
            print(
                f"[reset] FAIL: no row in _tenants for id={tenant_id}. "
                "Pass --register --db-name <db_name> to INSERT one.",
                file=sys.stderr,
            )
            return 1
        if not args.db_name:
            print("[reset] FAIL: --register requires --db-name",
                  file=sys.stderr)
            return 2
        name = args.name or args.db_name
        r = _proxy_post(
            base, "/execute_commit",
            proxy_key=proxy_key, tenant_db=args.control_db,
            body={
                "sql": (
                    "INSERT INTO _tenants "
                    "(id, name, api_key_hash, db_name, created_at, suspended) "
                    "VALUES (%s, %s, %s, %s, %s, FALSE)"
                ),
                "params": [
                    tenant_id, name, new_hash, args.db_name, time.time(),
                ],
            },
        )
        if r.get("rowcount") != 1:
            print(f"[reset] FAIL: INSERT rowcount={r.get('rowcount')} "
                  "(expected 1)", file=sys.stderr)
            return 1
        print(
            f"[reset] new tenant row INSERTed: id={tenant_id} "
            f"name={name!r} db_name={args.db_name}"
        )

    # 3. emit the new key.  This is the only time it's recoverable.
    if args.output_file:
        out = Path(args.output_file).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(out, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"tenant_id={tenant_id}\n")
            f.write(f"api_key={new_key}\n")
        os.chmod(out, 0o600)
        print(f"[reset] tenant API key written to {out} (mode 0600)")
    if args.print_key:
        print()
        print("─── tenant API key (rotate immediately on first use) ───")
        print(f"tenant_id : {tenant_id}")
        print(f"api_key   : {new_key}")
        print("───────────────────────────────────────────────────────")
    return 0


if __name__ == "__main__":
    sys.exit(main())

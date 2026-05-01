#!/usr/bin/env python3
"""Multi-tenant isolation smoke test against a live hivemind-core.

Creates two tenants via the admin API, writes into tenant A's DB,
verifies tenant B cannot see A's data (different row count and/or
unseen tables), and cleans both tenants up.

Exits 0 on full isolation, non-zero with a diagnostic summary otherwise.

Env:
  HIVEMIND_BASE_URL    — service URL (required)
  HIVEMIND_ADMIN_KEY   — admin key (required)

Usage:
  HIVEMIND_BASE_URL=https://<app>-8100.<gateway> \\
  HIVEMIND_ADMIN_KEY=... \\
    python scripts/smoke_multi_tenant.py [--insecure-attestation-bootstrap]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import uuid
from urllib.parse import urlparse

import httpx


def _hostname(url: str | None) -> str:
    return (urlparse(url or "").hostname or "").lower()


def _should_pin_enclave_cert(base_url: str, pinning_url: str | None) -> bool:
    return not pinning_url or _hostname(base_url) == _hostname(pinning_url)


def _pin_from_attestation(base_url: str, *, insecure_bootstrap: bool) -> str | None:
    """Fetch /v1/attestation and return the enclave cert PEM if present.

    Returns None when the server is not in enclave-TLS mode (gateway TLS),
    in which case default CA verification is the right thing to do.
    """
    with httpx.Client(verify=not insecure_bootstrap, timeout=30.0) as c:
        r = c.get(f"{base_url.rstrip('/')}/v1/attestation")
        r.raise_for_status()
        tls = (r.json().get("attestation") or {}).get("tls") or {}
    if not tls.get("enabled"):
        return None
    pinning_url = tls.get("pinning_url")
    if not _should_pin_enclave_cert(base_url, str(pinning_url or "")):
        return None
    pem = tls.get("cert_pem")
    claimed_fp = tls.get("cert_fingerprint_sha256_hex")
    if not pem or not claimed_fp:
        die("attestation reports TLS enabled but cert_pem/fingerprint missing")
    # Sanity: fingerprint matches the PEM we'd pin to
    import base64
    der = base64.b64decode(
        b"".join(
            l.strip().encode()
            for l in pem.splitlines()
            if l and not l.startswith("-----")
        )
    )
    actual_fp = hashlib.sha256(der).hexdigest()
    if actual_fp != claimed_fp:
        die(f"attestation cert_pem does not match claimed fingerprint "
            f"(claimed={claimed_fp}, computed={actual_fp})")
    return pem


def die(msg: str, code: int = 1) -> None:
    print(f"[smoke] FAIL: {msg}", file=sys.stderr)
    sys.exit(code)


def _post(c: httpx.Client, path: str, *, key: str, json: dict) -> httpx.Response:
    return c.post(path, headers={"Authorization": f"Bearer {key}"}, json=json)


def _delete(c: httpx.Client, path: str, *, key: str) -> httpx.Response:
    return c.delete(path, headers={"Authorization": f"Bearer {key}"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--insecure-attestation-bootstrap",
        action="store_true",
        help=(
            "disable TLS verification only for the initial /v1/attestation "
            "fetch used to pin an enclave self-signed cert"
        ),
    )
    args = parser.parse_args()

    base = os.environ.get("HIVEMIND_BASE_URL")
    admin = os.environ.get("HIVEMIND_ADMIN_KEY")
    if not base or not admin:
        die("set HIVEMIND_BASE_URL and HIVEMIND_ADMIN_KEY")

    tag = uuid.uuid4().hex[:6]
    name_a, name_b = f"smoke-a-{tag}", f"smoke-b-{tag}"
    print(f"[smoke] tenants: {name_a} / {name_b}")

    # For enclave-TLS URLs (Tier 3) the cert is self-signed and pinned to
    # the TDX quote — verify against that PEM, not system CAs.
    if args.insecure_attestation_bootstrap:
        print(
            "[smoke] warning: initial attestation fetch skips TLS verification",
            file=sys.stderr,
        )
    pinned_pem = _pin_from_attestation(
        base,
        insecure_bootstrap=args.insecure_attestation_bootstrap,
    )
    if pinned_pem:
        ca_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".pem", delete=False
        )
        ca_file.write(pinned_pem)
        ca_file.flush()
        verify: str | bool = ca_file.name
        print("[smoke] enclave TLS — pinning cert from /v1/attestation")
    else:
        verify = True

    with httpx.Client(base_url=base, timeout=60.0, verify=verify) as c:
        # 1. provision
        r = _post(c, "/v1/admin/tenants", key=admin, json={"name": name_a})
        if r.status_code != 200:
            die(f"provision A: {r.status_code} {r.text}")
        a = r.json()
        r = _post(c, "/v1/admin/tenants", key=admin, json={"name": name_b})
        if r.status_code != 200:
            die(f"provision B: {r.status_code} {r.text}")
        b = r.json()
        key_a, key_b = a["api_key"], b["api_key"]
        print(f"[smoke] A tenant_id={a.get('tenant_id')} key=<redacted>")
        print(f"[smoke] B tenant_id={b.get('tenant_id')} key=<redacted>")

        try:
            # 2. A creates a table and inserts a row
            sql_create = (
                "CREATE TABLE smoke_secrets ("
                "id serial primary key, val text not null)"
            )
            r = _post(c, "/v1/store", key=key_a,
                      json={"sql": sql_create, "params": []})
            if r.status_code != 200:
                die(f"A create table: {r.status_code} {r.text}")
            r = _post(c, "/v1/store", key=key_a,
                      json={"sql": "INSERT INTO smoke_secrets(val) VALUES (%s)",
                            "params": ["secret-of-A"]})
            if r.status_code != 200:
                die(f"A insert: {r.status_code} {r.text}")

            # 3. A reads back — baseline sanity
            r = _post(c, "/v1/store", key=key_a,
                      json={"sql": "SELECT val FROM smoke_secrets", "params": []})
            if r.status_code != 200 or "secret-of-A" not in r.text:
                die(f"A self-read broken: {r.status_code} {r.text}")
            print("[smoke] A self-read OK")

            # 4. B tries to read A's table — MUST fail
            r = _post(c, "/v1/store", key=key_b,
                      json={"sql": "SELECT val FROM smoke_secrets",
                            "params": []})
            # Expected: 400/500 "relation does not exist" — the table
            # lives in A's DB only.
            if r.status_code == 200:
                body = r.json()
                rows = body.get("rows") or []
                if any("secret-of-A" in str(v) for row in rows
                       for v in row.values()):
                    die(f"ISOLATION BREACH: B read A's rows: {body}")
                # empty result is also a failure — B shouldn't even see
                # the table exist (different DB)
                die(f"B saw A's table (empty result, but visible): {body}")
            if "does not exist" not in r.text.lower() \
                    and "relation" not in r.text.lower() \
                    and "undefined" not in r.text.lower():
                print(f"[smoke] NOTE: B got {r.status_code} {r.text}")
            print("[smoke] B cannot see A's table — isolation OK")

            # 5. B creates its own table with the same name and verifies
            #    independence
            r = _post(c, "/v1/store", key=key_b,
                      json={"sql": sql_create, "params": []})
            if r.status_code != 200:
                die(f"B create (same name): {r.status_code} {r.text}")
            r = _post(c, "/v1/store", key=key_b,
                      json={"sql": "INSERT INTO smoke_secrets(val) VALUES (%s)",
                            "params": ["secret-of-B"]})
            if r.status_code != 200:
                die(f"B insert: {r.status_code} {r.text}")

            # 6. Cross-verify: A still only sees its own row
            r = _post(c, "/v1/store", key=key_a,
                      json={"sql": "SELECT val FROM smoke_secrets", "params": []})
            body = r.json()
            rows = body.get("rows") or []
            vals = {row.get("val") for row in rows}
            if "secret-of-B" in vals:
                die(f"ISOLATION BREACH: A sees B's data: {vals}")
            if vals != {"secret-of-A"}:
                die(f"unexpected rows in A: {vals}")
            print("[smoke] A still only sees 'secret-of-A'")

            r = _post(c, "/v1/store", key=key_b,
                      json={"sql": "SELECT val FROM smoke_secrets", "params": []})
            body = r.json()
            vals = {row.get("val") for row in (body.get("rows") or [])}
            if "secret-of-A" in vals:
                die(f"ISOLATION BREACH: B sees A's data: {vals}")
            if vals != {"secret-of-B"}:
                die(f"unexpected rows in B: {vals}")
            print("[smoke] B still only sees 'secret-of-B'")

            # 7. Wrong key is rejected
            r = _post(c, "/v1/store", key="wrong-key",
                      json={"sql": "SELECT 1", "params": []})
            if r.status_code != 401:
                die(f"wrong key accepted: {r.status_code} {r.text}")
            print("[smoke] wrong key -> 401 OK")

            print("[smoke] PASS — isolation holds on both directions")
            return 0
        finally:
            # 8. cleanup (best-effort)
            for label, data in (("A", a), ("B", b)):
                tid = data.get("tenant_id")
                if not tid:
                    continue
                try:
                    r = _delete(c, f"/v1/admin/tenants/{tid}", key=admin)
                    print(f"[smoke] delete {label} ({tid}): {r.status_code}")
                except httpx.RequestError as e:
                    print(f"[smoke] delete {label} failed: {e}")


if __name__ == "__main__":
    sys.exit(main())

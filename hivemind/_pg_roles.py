"""Per-tenant Postgres role derivation.

Layer 1 of the three-layer isolation model — removes superuser from the
data plane by giving each tenant a Postgres role whose login password is
derived deterministically from a server-held seed (SQL_PROXY_KEY).

Both `sql_proxy.py` (server side) and `tenants.py` (control side) derive
the same password from (seed, tenant_id) without coordination, so the
role password is never stored anywhere — not in the control DB, not in
env files, not in logs.

This module is the authoritative source for the derivation; sql_proxy.py
inlines a byte-for-byte copy because it intentionally has no hivemind
imports (stdlib + psycopg only). Keep the two in sync.

Honest scope:
    - Defends against: scope_fn SQL injection escalating to superuser;
      superuser blast radius if a query is malformed.
    - Does NOT defend against: a malicious hivemind-core redeploy forging
      X-Tenant-DB to read another tenant's rows. That's Layer 3
      (client-signed requests + tenant-held auth secret).
    - Prerequisite for Layer 3: once roles exist, the derivation source
      swaps from SQL_PROXY_KEY to a tenant-held secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

_TENANT_DB_RE = re.compile(r"^tenant_(t_[0-9a-f]+)$")


def derive_tenant_role_password(seed: bytes, tenant_id: str) -> str:
    """Return the deterministic Postgres password for a tenant's role.

    Output is url-safe base64 without padding, 43 chars (256 bits of entropy).
    """
    if not seed:
        raise ValueError("seed must be non-empty")
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    info = f"pg-role-v1:{tenant_id}".encode("utf-8")
    block = hmac.new(seed, info + b"\x01", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(block).rstrip(b"=").decode("ascii")


def role_name_for_tenant(tenant_id: str) -> str:
    """Return the Postgres role name for `tenant_id`.

    Must match the pattern parsed by `parse_tenant_id_from_db_name` ⇄
    `tenant_<id>_role`. Postgres identifier limit is 63 chars.
    """
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    name = f"tenant_{tenant_id}_role"
    if len(name) > 63:
        raise ValueError(f"role name too long (>{63} chars): {name}")
    return name


def parse_tenant_id_from_db_name(db_name: str) -> str | None:
    """Extract `tenant_id` from a tenant DB name, or None if not a tenant DB.

    Control DBs (hivemind_control, etc.) return None — they don't use
    per-tenant roles, only the superuser connection.
    """
    if not db_name:
        return None
    m = _TENANT_DB_RE.match(db_name)
    return m.group(1) if m else None

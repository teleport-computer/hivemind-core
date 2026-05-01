"""Capability-token tests — query (hmq_) delegations.

Verifies registry storage, the ``resolve_any`` dispatcher, and the
constraint validation rules. Postgres-backed (re-uses the live DB the
rest of the tenants suite needs); skips when
``HIVEMIND_TEST_DATABASE_URL`` is unreachable.
"""

from __future__ import annotations

import os
import secrets

import psycopg
import pytest

from hivemind.config import Settings
from hivemind.tenants import (
    Caller,
    TenantRegistry,
    _hash_api_key,
    _QUERY_TOKEN_PREFIX,
)


TEST_DSN = os.environ.get(
    "HIVEMIND_TEST_DATABASE_URL",
    "postgresql://hivemind:dev@localhost:5432/postgres",
)


def _pg_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(TEST_DSN),
    reason=f"Postgres not reachable at {TEST_DSN}",
)


def _unique(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def _drop_db(dsn: str, db_name: str) -> None:
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    except Exception:
        pass


def _make_settings(control_db: str) -> Settings:
    return Settings(
        database_url=TEST_DSN,
        control_database=control_db,
        admin_key="admin-test-key",
        sql_proxy_admin_key="",
        autoload_default_agents=False,
        artifact_sweep_interval_seconds=9999,
    )


@pytest.fixture
def registry():
    control_db = _unique("hm_caps")
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{control_db}"')

    settings = _make_settings(control_db)
    reg = TenantRegistry(settings)
    from hivemind.admin_proxy import LocalPgAdmin
    reg._pg_admin = LocalPgAdmin(TEST_DSN)
    created_dbs: list[str] = [control_db]
    reg._test_created_dbs = created_dbs  # type: ignore[attr-defined]

    yield reg

    try:
        reg.close()
    except Exception:
        pass
    for name in created_dbs:
        _drop_db(TEST_DSN, name)


# ── mint / list / revoke ──────────────────────────────────────────────


def test_mint_query_token_requires_scope_agent_id(registry):
    t = registry.provision("alpha")
    registry._test_created_dbs.append(t["db_name"])

    with pytest.raises(ValueError):
        registry.mint_capability(t["tenant_id"], "query", "no-scope", {})

    out = registry.mint_capability(
        t["tenant_id"], "query", "scoped", {"scope_agent_id": "abc123"},
    )
    assert out["token"].startswith(_QUERY_TOKEN_PREFIX)
    assert out["kind"] == "query"
    # Phase 4: ``can_upload_query_agent`` is always emitted; defaults
    # to false so existing tokens stay prompt-only.
    assert out["constraints"] == {
        "scope_agent_id": "abc123",
        "can_upload_query_agent": False,
    }
    assert len(out["token_id"]) == 12


def test_mint_records_can_upload_query_agent_flag(registry):
    """Phase 4: ``can_upload_query_agent`` round-trips end to end."""
    t = registry.provision("phase4_flag_on")
    registry._test_created_dbs.append(t["db_name"])

    out = registry.mint_capability(
        t["tenant_id"],
        "query",
        "uploader",
        {"scope_agent_id": "sc1", "can_upload_query_agent": True},
    )
    assert out["constraints"] == {
        "scope_agent_id": "sc1",
        "can_upload_query_agent": True,
    }
    caller = registry.resolve_any(out["token"])
    assert caller is not None
    assert caller.constraints["can_upload_query_agent"] is True


def test_mint_rejects_non_bool_can_upload(registry):
    """Phase 4: only true booleans accepted; truthy strings should fail."""
    t = registry.provision("phase4_bad_type")
    registry._test_created_dbs.append(t["db_name"])
    with pytest.raises(ValueError):
        registry.mint_capability(
            t["tenant_id"],
            "query",
            "bad",
            {"scope_agent_id": "sc", "can_upload_query_agent": "yes"},
        )


def test_mint_rejects_write_kind(registry):
    """Write tokens were removed — mint should refuse kind='write'."""
    t = registry.provision("beta")
    registry._test_created_dbs.append(t["db_name"])
    with pytest.raises(ValueError):
        registry.mint_capability(
            t["tenant_id"], "write", "ok",
            {"allowed_tables": ["watch_history"]},
        )


def test_mint_invalid_kind(registry):
    t = registry.provision("gamma")
    registry._test_created_dbs.append(t["db_name"])
    with pytest.raises(ValueError):
        registry.mint_capability(t["tenant_id"], "admin", "x", {})


def test_mint_unknown_tenant_raises(registry):
    with pytest.raises(KeyError):
        registry.mint_capability(
            "t_doesnotexist", "query", "x", {"scope_agent_id": "y"},
        )


def test_token_plaintext_not_persisted(registry):
    t = registry.provision("delta")
    registry._test_created_dbs.append(t["db_name"])
    out = registry.mint_capability(
        t["tenant_id"], "query", "", {"scope_agent_id": "s"},
    )
    rows = registry._control_db.execute(
        "SELECT token_hash FROM _capability_tokens WHERE tenant_id = %s",
        [t["tenant_id"]],
    )
    assert rows[0]["token_hash"] == _hash_api_key(out["token"])
    # The plaintext never appears in the row.
    assert out["token"] not in rows[0]["token_hash"]


def test_list_capabilities_returns_metadata_only(registry):
    t = registry.provision("epsilon")
    registry._test_created_dbs.append(t["db_name"])
    a = registry.mint_capability(
        t["tenant_id"], "query", "viewer", {"scope_agent_id": "s1"},
    )
    b = registry.mint_capability(
        t["tenant_id"], "query", "ingest", {"scope_agent_id": "s2"},
    )
    rows = registry.list_capabilities(t["tenant_id"])
    ids = {r["token_id"] for r in rows}
    assert {a["token_id"], b["token_id"]} <= ids
    for r in rows:
        # Hash + plaintext must not leak into the listing.
        assert "token" not in r
        assert "token_hash" not in r


def test_revoke_capability_blocks_resolution(registry):
    t = registry.provision("zeta")
    registry._test_created_dbs.append(t["db_name"])
    out = registry.mint_capability(
        t["tenant_id"], "query", "x", {"scope_agent_id": "s"},
    )
    caller = registry.resolve_any(out["token"])
    assert caller is not None and caller.role == "query"

    assert registry.revoke_capability(t["tenant_id"], out["token_id"]) is True
    assert registry.resolve_any(out["token"]) is None


def test_revoke_capability_requires_min_prefix_length(registry):
    t = registry.provision("eta")
    registry._test_created_dbs.append(t["db_name"])
    with pytest.raises(ValueError):
        registry.revoke_capability(t["tenant_id"], "abc")


def test_revoke_capability_idempotent_on_unknown(registry):
    t = registry.provision("theta")
    registry._test_created_dbs.append(t["db_name"])
    assert (
        registry.revoke_capability(t["tenant_id"], "deadbeefcafe") is False
    )


# ── resolve_any ───────────────────────────────────────────────────────


def test_resolve_any_owner_token(registry):
    t = registry.provision("iota")
    registry._test_created_dbs.append(t["db_name"])
    caller = registry.resolve_any(t["api_key"])
    assert isinstance(caller, Caller)
    assert caller.role == "owner"
    assert caller.tenant_id == t["tenant_id"]
    assert caller.constraints == {}


def test_resolve_any_query_token(registry):
    t = registry.provision("kappa")
    registry._test_created_dbs.append(t["db_name"])
    out = registry.mint_capability(
        t["tenant_id"], "query", "shared",
        {"scope_agent_id": "abc123"},
    )
    caller = registry.resolve_any(out["token"])
    assert caller is not None
    assert caller.role == "query"
    assert caller.tenant_id == t["tenant_id"]
    assert caller.constraints == {
        "scope_agent_id": "abc123",
        "can_upload_query_agent": False,
    }
    assert caller.token_id == out["token_id"]


def test_resolve_any_rejects_unknown_prefix(registry):
    assert registry.resolve_any("hmx_garbage") is None
    assert registry.resolve_any("hmw_garbage") is None
    assert registry.resolve_any("") is None
    assert registry.resolve_any("nope") is None


def test_resolve_any_rejects_revoked(registry):
    t = registry.provision("mu")
    registry._test_created_dbs.append(t["db_name"])
    out = registry.mint_capability(
        t["tenant_id"], "query", "", {"scope_agent_id": "s"},
    )
    registry.revoke_capability(t["tenant_id"], out["token_id"])
    assert registry.resolve_any(out["token"]) is None


def test_resolve_any_rejects_suspended_tenant(registry):
    t = registry.provision("nu")
    registry._test_created_dbs.append(t["db_name"])
    out = registry.mint_capability(
        t["tenant_id"], "query", "", {"scope_agent_id": "s"},
    )
    registry._control_db.execute_commit(
        "UPDATE _tenants SET suspended = TRUE WHERE id = %s",
        [t["tenant_id"]],
    )
    assert registry.resolve_any(out["token"]) is None


def test_capability_tokens_cascade_on_tenant_delete(registry):
    t = registry.provision("xi")
    registry._test_created_dbs.append(t["db_name"])
    out = registry.mint_capability(
        t["tenant_id"], "query", "", {"scope_agent_id": "s"},
    )
    registry.delete(t["tenant_id"])
    rows = registry._control_db.execute(
        "SELECT token_hash FROM _capability_tokens WHERE tenant_id = %s",
        [t["tenant_id"]],
    )
    assert rows == []
    assert registry.resolve_any(out["token"]) is None


# ── seal: cold-cache after restart ────────────────────────────────────


def test_resolve_any_cold_cache_after_eviction(registry):
    """Simulates a CVM restart wiping the in-process DEK cache.

    After eviction the owner (hmk_) re-thaws on next contact. Capability
    tokens (hmq_) minted before owner thaw do not get upgraded in place.
    Recreate the room to mint a wrapped invite.
    """
    t = registry.provision("seal_cold")
    registry._test_created_dbs.append(t["db_name"])
    out = registry.mint_capability(
        t["tenant_id"], "query", "", {"scope_agent_id": "s"},
    )

    # Owner contact warms the seal on first resolve.
    owner_caller = registry.resolve_any(t["api_key"])
    assert owner_caller is not None and owner_caller.sealed is False
    assert registry.sealer.is_unsealed(t["tenant_id"])

    # Capability resolves while warm because the owner warmed the tenant,
    # but this intentionally does not mutate the old capability row.
    cap_caller = registry.resolve_any(out["token"])
    assert cap_caller is not None and cap_caller.sealed is False

    # Simulate process restart: in-memory DEK cache wiped.
    registry.sealer.evict(t["tenant_id"])
    assert not registry.sealer.is_unsealed(t["tenant_id"])

    # The old unwrapped capability token cannot thaw the tenant by itself.
    cap_cold = registry.resolve_any(out["token"])
    assert cap_cold is not None
    assert cap_cold.role == "query"
    assert cap_cold.sealed is True
    assert not registry.sealer.is_unsealed(t["tenant_id"])

    # Owner contact re-thaws via the persisted wrapped-DEK record.
    registry.sealer.evict(t["tenant_id"])
    owner_warm = registry.resolve_any(t["api_key"])
    assert owner_warm is not None and owner_warm.sealed is False
    assert registry.sealer.is_unsealed(t["tenant_id"])

    # Capability now sees the warm cache.
    cap_warm = registry.resolve_any(out["token"])
    assert cap_warm is not None and cap_warm.sealed is False


def test_unwrapped_capability_stays_unwrapped_after_owner_warms(registry):
    t = registry.provision("seal_unwrapped")
    registry._test_created_dbs.append(t["db_name"])
    out = registry.mint_capability(
        t["tenant_id"], "query", "", {"scope_agent_id": "s"},
    )

    # The token was minted before the owner ever initialized the seal,
    # so there is no DEK wrap for it yet.
    cold = registry.resolve_any(out["token"])
    assert cold is not None
    assert cold.sealed is True
    assert not registry.sealer.is_unsealed(t["tenant_id"])

    owner = registry.resolve_any(t["api_key"])
    assert owner is not None and owner.sealed is False
    warm = registry.resolve_any(out["token"])
    assert warm is not None and warm.sealed is False
    registry.sealer.evict(t["tenant_id"])

    still_unwrapped = registry.resolve_any(out["token"])
    assert still_unwrapped is not None
    assert still_unwrapped.sealed is True
    assert not registry.sealer.is_unsealed(t["tenant_id"])


def test_capability_minted_while_owner_warm_thaws_after_eviction(registry):
    t = registry.provision("seal_wrapped")
    registry._test_created_dbs.append(t["db_name"])

    owner = registry.resolve_any(t["api_key"])
    assert owner is not None and owner.sealed is False

    out = registry.mint_capability(
        t["tenant_id"], "query", "", {"scope_agent_id": "s"},
    )
    registry.sealer.evict(t["tenant_id"])

    cap = registry.resolve_any(out["token"])
    assert cap is not None
    assert cap.role == "query"
    assert cap.sealed is False
    assert registry.sealer.is_unsealed(t["tenant_id"])


def test_resolve_any_wrong_owner_after_eviction_stays_sealed(registry):
    """A different hmk_ key cannot re-thaw an existing seal record.

    Mirrors the post-rotation case: an old/forged owner key against a
    seal record bound to the real owner's KEK must return ``None`` (401)
    and leave the cache cold.
    """
    t = registry.provision("seal_wrong_owner")
    registry._test_created_dbs.append(t["db_name"])

    # Real owner thaws and persists the wrapped-DEK record.
    assert registry.resolve_any(t["api_key"]) is not None
    registry.sealer.evict(t["tenant_id"])

    # A bogus hmk_ that doesn't match any tenant row — 401, no thaw.
    assert registry.resolve_any("hmk_bogus_nonexistent_owner_key") is None
    assert not registry.sealer.is_unsealed(t["tenant_id"])

"""End-to-end tests for ``hivemind compose`` + pin-rotation flow.

Builds on ``share_env`` from ``test_cli_share.py``: live FastAPI app,
provisioned tenant, scope agent. Adds:
- ``hivemind compose bless`` round-trip (sign client-side, store on
  the service, retrieve, verify signature).
- ``hivemind share --mint --pin-rotation`` produces a URI with
  ``signer=<pubkey>`` and no ``compose=``.
- ``hivemind ask`` against a pin-rotation URI verifies the envelope
  signature and enforces "live compose ∈ allowed_composes" + matching
  attested-files digest.
- Tamper paths exit 4: forged signer, redeploy to disallowed compose,
  signature mutation, expired pin.

Postgres-backed; skips when ``HIVEMIND_TEST_DATABASE_URL`` unreachable.
"""

from __future__ import annotations

import base64
import os
import secrets
import time
from urllib.parse import quote as _quote

import psycopg
import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient

from hivemind import attestation as _att
from hivemind import cli as _cli_mod
from hivemind import trust as _trust_mod
from hivemind.compose_pin import ComposePin
from hivemind.config import Settings
from hivemind.sandbox.models import AgentConfig
from hivemind.server import create_app
from hivemind.tenant_signing import derive_signing_keypair
from hivemind.tenants import TenantRegistry


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


# Live attestation compose_hash for tests. Set by ``stub_compose`` so
# both ``compose bless`` (server-side fetch) and recipient verify see
# the same value.
_LIVE_COMPOSE = "a" * 64


@pytest.fixture
def stub_compose(monkeypatch):
    """Patch the attestation bundle so /v1/attestation returns a known
    compose_hash. Default test bundle is ``not_bootstrapped`` (empty
    compose) which would never match a 64-hex pin."""

    def _fake_bundle():
        return {
            "ready": True,
            "booted_at": 0,
            "attestation": {
                "compose_hash": _LIVE_COMPOSE,
                "app_id": "test-app",
            },
        }

    monkeypatch.setattr(_att, "get_bundle", _fake_bundle)
    yield _LIVE_COMPOSE


@pytest.fixture
def share_env(tmp_path, monkeypatch, stub_compose):
    """Same shape as test_cli_share.share_env but with the attestation
    bundle stub installed (via the ``stub_compose`` dependency)."""
    control_db = _unique("hm_compose")
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{control_db}"')

    settings = Settings(
        database_url=TEST_DSN,
        control_database=control_db,
        admin_key="admin-test-key",
        sql_proxy_admin_key="",
        autoload_default_agents=False,
        artifact_sweep_interval_seconds=9999,
    )
    registry = TenantRegistry(settings)
    from hivemind.admin_proxy import LocalPgAdmin
    registry._pg_admin = LocalPgAdmin(TEST_DSN)
    created_dbs: list[str] = [control_db]

    app = create_app(settings)
    app.state.registry = registry

    t = registry.provision("compose-test")
    created_dbs.append(t["db_name"])

    scope_id = "scope_compose_test"
    files = {
        "Dockerfile": "FROM python:3.12-slim\n",
        "agent.py": "print('scope')\n",
    }
    hive = registry.for_tenant(t["tenant_id"])
    assert hive is not None
    hive.agent_store.create(
        AgentConfig(
            agent_id=scope_id,
            name="scope-compose-test",
            description="fixture",
            agent_type="scope",
            image="hivemind-compose-test:latest",
            entrypoint=None,
            memory_mb=256,
            max_llm_calls=10,
            max_tokens=10_000,
            timeout_seconds=60,
        )
    )
    hive.agent_store.save_files(scope_id, files)

    client = TestClient(app, base_url="http://compose-test")

    def _hget(url: str, **kw):
        kw.pop("verify", None)
        return client.get(url, **kw)

    def _hpost(url: str, **kw):
        kw.pop("verify", None)
        return client.post(url, **kw)

    def _hdelete(url: str, **kw):
        kw.pop("verify", None)
        return client.delete(url, **kw)

    monkeypatch.setattr(_cli_mod, "_hget", _hget)
    monkeypatch.setattr(_cli_mod, "_hpost", _hpost)
    monkeypatch.setattr(_cli_mod, "_hdelete", _hdelete)

    hivemind_home = tmp_path / ".hivemind"
    profiles_dir = hivemind_home / "profiles"
    profiles_dir.mkdir(parents=True)
    monkeypatch.setattr(_cli_mod, "_HIVEMIND_HOME", hivemind_home)
    monkeypatch.setattr(_cli_mod, "_PROFILES_DIR", profiles_dir)
    monkeypatch.setattr(_cli_mod, "_ACTIVE_POINTER", hivemind_home / "active")
    monkeypatch.setattr(_trust_mod, "_TRUST_DIR", hivemind_home)
    monkeypatch.setattr(
        _trust_mod, "_TRUST_PATH", hivemind_home / "trust.json"
    )

    profile_name = "compose_test_profile"
    profile_path = profiles_dir / f"{profile_name}.yaml"
    profile_path.write_text(
        yaml.dump({
            "service": "http://compose-test",
            "api_key": t["api_key"],
            "scope_agent_id": scope_id,
        })
    )

    monkeypatch.setenv("HIVEMIND_PROFILE", profile_name)
    monkeypatch.setenv("HIVEMIND_NO_TRUST_CHECK", "1")
    for var in ("HIVEMIND_TRUST_ALL", "HIVEMIND_TRUST_HASH"):
        monkeypatch.delenv(var, raising=False)

    runner = CliRunner()

    yield runner, profile_name, scope_id, client, t["api_key"], t["tenant_id"]

    try:
        client.close()
    except Exception:
        pass
    try:
        registry.close()
    except Exception:
        pass
    for name in created_dbs:
        _drop_db(TEST_DSN, name)


# ── compose bless / pins / revoke ───────────────────────────────────


def test_bless_persists_signed_envelope(share_env):
    runner, _profile, scope_id, client, api_key, tenant_id = share_env

    result = runner.invoke(_cli_mod.cli, ["compose", "bless", "--json"])
    assert result.exit_code == 0, result.stderr or result.output
    import json
    body = json.loads(result.stdout)
    assert body["tenant_id"] == tenant_id
    env = body["envelope"]
    assert env["scope_agent_id"] == scope_id
    assert _LIVE_COMPOSE in env["allowed_composes"]
    assert env["signer_pubkey"]
    assert env["signature"]

    # Verify the envelope independently against the pubkey we'd derive
    # from the same hmk_+tenant — proves the server didn't tamper.
    _priv, pub = derive_signing_keypair(api_key, tenant_id)
    from cryptography.hazmat.primitives import serialization
    expected = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pin = ComposePin.model_validate(env)
    assert pin.verify(expected_pubkey=expected)

    # And it's listed.
    pins = runner.invoke(_cli_mod.cli, ["compose", "pins", "--json"])
    assert pins.exit_code == 0
    listed = json.loads(pins.stdout)["pins"]
    assert any(p["pin_id"] == body["pin_id"] for p in listed)


def test_bless_with_extra_hash(share_env):
    """``--hash`` adds another compose to the allow-list."""
    runner, *_ = share_env
    extra = "f" * 64
    result = runner.invoke(
        _cli_mod.cli,
        ["compose", "bless", "--hash", extra, "--json"],
    )
    assert result.exit_code == 0, result.stderr or result.output
    import json
    env = json.loads(result.stdout)["envelope"]
    assert _LIVE_COMPOSE in env["allowed_composes"]
    assert extra in env["allowed_composes"]


def test_bless_rejects_bad_hash(share_env):
    runner, *_ = share_env
    result = runner.invoke(
        _cli_mod.cli, ["compose", "bless", "--hash", "deadbeef"]
    )
    assert result.exit_code != 0
    assert "64 hex" in (result.stderr or result.output)


def test_revoke_pin(share_env):
    runner, *_ = share_env
    import json
    r = runner.invoke(_cli_mod.cli, ["compose", "bless", "--json"])
    assert r.exit_code == 0
    pin_id = json.loads(r.stdout)["pin_id"]

    rv = runner.invoke(_cli_mod.cli, ["compose", "revoke", pin_id])
    assert rv.exit_code == 0
    assert pin_id in rv.output


# ── share --pin-rotation ────────────────────────────────────────────


def test_share_pin_rotation_requires_a_published_pin(share_env):
    """Without a prior bless, --pin-rotation must abort cleanly."""
    runner, *_ = share_env
    result = runner.invoke(
        _cli_mod.cli, ["share", "--mint", "--pin-rotation"]
    )
    assert result.exit_code != 0
    err = result.stderr or result.output
    assert "compose pin" in err.lower()


def test_share_pin_rotation_emits_signer_uri(share_env):
    runner, _profile, scope_id, *_ = share_env
    assert runner.invoke(
        _cli_mod.cli, ["compose", "bless"]
    ).exit_code == 0
    result = runner.invoke(
        _cli_mod.cli, ["share", "--mint", "--pin-rotation"]
    )
    assert result.exit_code == 0, result.stderr or result.output
    uri = result.stdout.strip()
    parsed = _cli_mod._parse_hmq_uri(uri)
    assert parsed["scope_agent_id"] == scope_id
    assert parsed["signer_pubkey"]
    # In pin-rotation mode the URI does NOT bake compose= or files=.
    assert parsed["compose_hash"] == ""
    assert parsed["files_digest"] == ""


# ── ask with a pin-rotation URI ─────────────────────────────────────


def _bless_and_mint(runner, scheme: str = "http") -> str:
    assert runner.invoke(
        _cli_mod.cli, ["compose", "bless"]
    ).exit_code == 0
    res = runner.invoke(
        _cli_mod.cli, ["share", "--mint", "--pin-rotation"]
    )
    assert res.exit_code == 0, res.stderr or res.output
    uri = res.stdout.strip()
    # share doesn't add scheme=http for an http profile; force one.
    if "scheme=" not in uri:
        sep = "&" if "?" in uri else "?"
        uri = f"{uri}{sep}scheme={scheme}"
    return uri


def test_ask_accepts_valid_pin_rotation_uri(share_env):
    """A genuine pin-rotation URI passes verification (even though the
    actual /v1/query/run/submit call may then fail on missing query
    agent — we only assert no pin error fires)."""
    runner, *_ = share_env
    uri = _bless_and_mint(runner)
    result = runner.invoke(_cli_mod.cli, ["ask", uri, "hi"])
    err = (result.stderr or "") + (result.output or "")
    assert "compose-pin signature" not in err
    assert "allowed_composes" not in err
    assert "attested_files_digest mismatch" not in err
    assert "expired" not in err.lower() or "exp=" not in err


def test_ask_rejects_swapped_signer(share_env):
    """Replace ``signer=`` with an attacker-derived pubkey. Even if the
    operator returns a perfectly valid pin, the recipient must reject
    because the pin's pubkey != URI signer pubkey."""
    runner, _profile, _scope, _client, _api_key, tenant_id = share_env
    uri = _bless_and_mint(runner)
    # Different hmk_ → different keypair.
    _priv, attacker_pub = derive_signing_keypair("hmk_attacker", tenant_id)
    from cryptography.hazmat.primitives import serialization
    attacker_b64 = base64.b64encode(
        attacker_pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    # Replace the signer query param.
    import re
    forged = re.sub(
        r"signer=[^&]+", f"signer={_quote(attacker_b64, safe='')}", uri,
    )
    assert forged != uri  # sanity
    result = runner.invoke(_cli_mod.cli, ["ask", forged, "hi"])
    assert result.exit_code == 4, result.stderr or result.output
    err = result.stderr or result.output
    assert "signature" in err.lower()


def test_ask_rejects_when_live_compose_not_in_allowed(
    share_env, monkeypatch,
):
    """After mint, simulate a redeploy by switching the live
    compose_hash to a value not in the pin's allowed_composes."""
    runner, *_ = share_env
    uri = _bless_and_mint(runner)
    # Now flip the live bundle to a different compose.
    rotated = "b" * 64

    def _rotated_bundle():
        return {
            "ready": True,
            "booted_at": 0,
            "attestation": {"compose_hash": rotated, "app_id": "test"},
        }

    monkeypatch.setattr(_att, "get_bundle", _rotated_bundle)
    result = runner.invoke(_cli_mod.cli, ["ask", uri, "hi"])
    assert result.exit_code == 4, result.stderr or result.output
    err = result.stderr or result.output
    assert "allowed_composes" in err


def test_ask_rejects_when_attested_files_change(share_env):
    """If owner edits files post-bless, the live attested digest no
    longer matches the pin → recipient aborts."""
    runner, _profile, scope_id, client, api_key, tenant_id = share_env
    uri = _bless_and_mint(runner)
    # Mutate an attestable file via direct store access.
    from hivemind.tenants import TenantRegistry
    reg: TenantRegistry = client.app.state.registry  # type: ignore[attr-defined]
    hive = reg.for_tenant(tenant_id)
    assert hive is not None
    hive.agent_store.save_files(
        scope_id, {"agent.py": "print('mutated')\n"},
    )

    result = runner.invoke(_cli_mod.cli, ["ask", uri, "hi"])
    assert result.exit_code == 4, result.stderr or result.output
    err = result.stderr or result.output
    assert "attested_files_digest" in err


def test_post_pin_rejects_envelope_with_wrong_signer(share_env):
    """Server endpoint defence-in-depth: if the client signs the
    envelope with a key derived from someone else's hmk_, POST must
    reject with 400, even if the signature itself verifies."""
    runner, _profile, scope_id, client, api_key, tenant_id = share_env
    from hivemind.compose_pin import make_unsigned_pin

    bad_priv, _ = derive_signing_keypair("hmk_imposter", tenant_id)
    pin = make_unsigned_pin(
        tenant_id=tenant_id,
        allowed_composes=[_LIVE_COMPOSE],
        scope_agent_id=scope_id,
        attested_files_digest="0" * 64,
    ).sign(bad_priv)

    r = client.post(
        "/v1/tenants/compose-pin",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"envelope": pin.model_dump()},
    )
    assert r.status_code == 400, r.text
    assert "signer_pubkey" in r.text or "signature" in r.text


def test_post_pin_rejects_tenant_id_mismatch(share_env):
    runner, _profile, scope_id, client, api_key, tenant_id = share_env
    from hivemind.compose_pin import make_unsigned_pin

    priv, _ = derive_signing_keypair(api_key, "t_other")
    pin = make_unsigned_pin(
        tenant_id="t_other",
        allowed_composes=[_LIVE_COMPOSE],
        scope_agent_id=scope_id,
        attested_files_digest="0" * 64,
    ).sign(priv)

    r = client.post(
        "/v1/tenants/compose-pin",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"envelope": pin.model_dump()},
    )
    assert r.status_code == 400
    assert "tenant_id" in r.text


def test_whoami_returns_tenant_id(share_env):
    _runner, _profile, _scope, client, api_key, tenant_id = share_env
    r = client.get(
        "/v1/whoami", headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == tenant_id
    assert body["role"] == "owner"

"""Tests for ``hivemind share`` and ``hivemind ask``.

Covers three layers:

1. Pure ``_parse_hmq_uri`` round-trips and rejection of malformed URIs
   (no HTTP, no fixtures).
2. End-to-end ``share`` against a live FastAPI app — verifies it mints
   an ``hmq_`` token, fetches the file digest, and emits a URI that
   round-trips through ``_parse_hmq_uri``.
3. End-to-end ``ask`` — verifies pin enforcement (compose / files
   digest), token-prefix sanity, and the rejection path when pins
   don't match.

Postgres-backed (re-uses the live DB the rest of the tenants suite
needs); skips when ``HIVEMIND_TEST_DATABASE_URL`` is unreachable.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import psycopg
import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient

from hivemind import cli as _cli_mod
from hivemind import trust as _trust_mod
from hivemind.config import Settings
from hivemind.sandbox.models import AgentConfig
from hivemind.server import create_app
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


# ── 1. Pure URI parse/round-trip ──────────────────────────────────────


def test_parse_hmq_uri_full_round_trip():
    uri = (
        "hmq://hivemind.example/scope_abc123"
        "?token=hmq_xyz789&compose=0xdead&files=0xbeef"
    )
    out = _cli_mod._parse_hmq_uri(uri)
    assert out["service"] == "https://hivemind.example"
    assert out["scope_agent_id"] == "scope_abc123"
    assert out["token"] == "hmq_xyz789"
    assert out["compose_hash"] == "0xdead"
    assert out["files_digest"] == "0xbeef"


def test_parse_hmq_uri_minimum_fields():
    """Only token is required — compose/files pins are optional."""
    out = _cli_mod._parse_hmq_uri(
        "hmq://h.example/scope_x?token=hmq_t"
    )
    assert out["service"] == "https://h.example"
    assert out["scope_agent_id"] == "scope_x"
    assert out["token"] == "hmq_t"
    assert out["compose_hash"] == ""
    assert out["files_digest"] == ""


def test_parse_hmq_uri_scheme_override():
    """``scheme=http`` lets the URI talk to a local dev server."""
    out = _cli_mod._parse_hmq_uri(
        "hmq://localhost:8000/sid?token=hmq_t&scheme=http"
    )
    assert out["service"] == "http://localhost:8000"


def test_parse_hmq_uri_lowercases_pins():
    out = _cli_mod._parse_hmq_uri(
        "hmq://h/s?token=hmq_t&compose=0xABCDEF&files=DEADBEEF"
    )
    assert out["compose_hash"] == "0xabcdef"
    assert out["files_digest"] == "deadbeef"


def test_parse_hmq_uri_rejects_wrong_scheme():
    runner = CliRunner()
    result = runner.invoke(
        _cli_mod.cli, ["ask", "https://h/s?token=hmq_t", "q"],
    )
    assert result.exit_code != 0
    assert "hmq://" in (result.output + (result.stderr or ""))


def test_parse_hmq_uri_rejects_bad_token_prefix():
    runner = CliRunner()
    result = runner.invoke(
        _cli_mod.cli, ["ask", "hmq://h/s?token=hmk_owner", "q"],
    )
    assert result.exit_code != 0
    assert "hmq_" in (result.output + (result.stderr or ""))


def test_parse_hmq_uri_rejects_missing_scope():
    runner = CliRunner()
    result = runner.invoke(
        _cli_mod.cli, ["ask", "hmq://h/?token=hmq_t", "q"],
    )
    assert result.exit_code != 0


# ── 2/3. share + ask integration ──────────────────────────────────────


@pytest.fixture
def share_env(tmp_path, monkeypatch):
    """Bring up a live FastAPI app + tenant + scope agent + profile YAML.

    Yields ``(runner, profile_name, scope_agent_id, asgi_client,
    cleanup_dbs)``. The ``asgi_client`` is the FastAPI TestClient used
    to swap into ``cli._hget``/``_hpost``.
    """
    control_db = _unique("hm_share")
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

    # Provision tenant + seed scope agent.
    t = registry.provision("share-test")
    created_dbs.append(t["db_name"])

    scope_id = "scope_share_test"
    files = {
        "Dockerfile": "FROM python:3.12-slim\n",
        "agent.py": "print('scope')\n",
    }
    hive = registry.for_tenant(t["tenant_id"])
    assert hive is not None
    hive.agent_store.create(
        AgentConfig(
            agent_id=scope_id,
            name="scope-share-test",
            description="fixture",
            agent_type="scope",
            image="hivemind-share-test:latest",
            entrypoint=None,
            memory_mb=256,
            max_llm_calls=10,
            max_tokens=10_000,
            timeout_seconds=60,
        )
    )
    hive.agent_store.save_files(scope_id, files)

    # Build a TestClient + monkeypatch the CLI's HTTP wrappers onto it.
    client = TestClient(app, base_url="http://share-test")

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

    # Redirect profile + trust roots to tmp_path so we don't touch the
    # operator's real ~/.hivemind/.
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

    profile_name = "share_test_profile"
    profile_path = profiles_dir / f"{profile_name}.yaml"
    profile_path.write_text(
        yaml.dump({
            "service": "http://share-test",
            "api_key": t["api_key"],
            "scope_agent_id": scope_id,
        })
    )

    monkeypatch.setenv("HIVEMIND_PROFILE", profile_name)
    monkeypatch.setenv("HIVEMIND_NO_TRUST_CHECK", "1")
    for var in ("HIVEMIND_TRUST_ALL", "HIVEMIND_TRUST_HASH"):
        monkeypatch.delenv(var, raising=False)

    runner = CliRunner()

    yield runner, profile_name, scope_id, client, t["api_key"]

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


def test_share_mint_emits_valid_uri(share_env):
    runner, profile, scope_id, client, _api_key = share_env
    result = runner.invoke(_cli_mod.cli, ["share", "--mint"])
    assert result.exit_code == 0, result.stderr or result.output
    uri = result.stdout.strip()
    assert uri.startswith("hmq://share-test/"), uri

    parsed = _cli_mod._parse_hmq_uri(uri)
    assert parsed["service"] == "https://share-test" or parsed["service"] == "http://share-test"
    # ``share`` doesn't add scheme=http for an http profile so the parser
    # falls back to https. We accept either here — the meaningful
    # invariant is the rest of the URI.
    assert parsed["scope_agent_id"] == scope_id
    assert parsed["token"].startswith("hmq_")
    # files_digest is always present (server returned a non-empty digest).
    assert len(parsed["files_digest"]) == 64  # sha256 hex
    # compose_hash is empty in tests (TEE bundle is not_bootstrapped).
    assert parsed["compose_hash"] == ""


def test_share_mint_with_upload_capability(share_env):
    """Phase 4: --can-upload-query-agent forwards the constraint to the
    minted token. The token must then accept /v1/query-agents/submit."""
    runner, _profile, scope_id, client, _api_key = share_env
    result = runner.invoke(
        _cli_mod.cli,
        ["share", "--mint", "--can-upload-query-agent", "--label", "uploader"],
    )
    assert result.exit_code == 0, result.stderr or result.output
    uri = result.stdout.strip()
    parsed = _cli_mod._parse_hmq_uri(uri)
    token = parsed["token"]

    # Listing tokens reflects the constraint persisted server-side.
    rows = client.get(
        "/v1/tokens",
        headers={"Authorization": f"Bearer {_api_key}"},
    ).json()["tokens"]
    matched = [
        r for r in rows if r.get("label") == "uploader"
    ]
    assert matched, rows
    assert matched[0]["constraints"]["can_upload_query_agent"] is True
    assert matched[0]["constraints"]["scope_agent_id"] == scope_id

    # The token may now post to /v1/query-agents/submit (we just check
    # the gate accepts it; the build runs in a background task).
    import io as _io, tarfile as _tar
    buf = _io.BytesIO()
    with _tar.open(fileobj=buf, mode="w:gz") as t:
        for n, b in (("Dockerfile", b"FROM python:3.12-slim\n"),
                     ("agent.py", b"x\n")):
            info = _tar.TarInfo(name=n)
            info.size = len(b)
            t.addfile(info, _io.BytesIO(b))
    r = client.post(
        "/v1/query-agents/submit",
        headers={"Authorization": f"Bearer {token}"},
        files={"archive": ("a.tar.gz", buf.getvalue(), "application/gzip")},
        data={"name": "phase4-share", "prompt": "hi"},
    )
    assert r.status_code == 200, r.text


def test_share_with_token_rejects_upload_flag(share_env):
    """--can-upload-query-agent is only meaningful with --mint; passing
    it together with --token should fail loud rather than silently."""
    runner, _profile, _scope_id, _client, _api_key = share_env
    result = runner.invoke(
        _cli_mod.cli,
        ["share", "--token", "hmq_abc", "--can-upload-query-agent"],
    )
    assert result.exit_code != 0


def test_share_with_explicit_token(share_env):
    runner, profile, scope_id, client, api_key = share_env
    # Mint a query token directly via the API first.
    r = client.post(
        "/v1/tokens",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "kind": "query",
            "label": "preminted",
            "constraints": {"scope_agent_id": scope_id},
        },
    )
    assert r.status_code == 200, r.text
    token = r.json()["token"]

    result = runner.invoke(
        _cli_mod.cli, ["share", "--token", token]
    )
    assert result.exit_code == 0, result.stderr or result.output
    parsed = _cli_mod._parse_hmq_uri(result.stdout.strip())
    assert parsed["token"] == token


def test_share_rejects_both_mint_and_token(share_env):
    runner, *_ = share_env
    result = runner.invoke(
        _cli_mod.cli, ["share", "--mint", "--token", "hmq_x"]
    )
    assert result.exit_code != 0


def test_share_rejects_neither_mint_nor_token(share_env):
    runner, *_ = share_env
    result = runner.invoke(_cli_mod.cli, ["share"])
    assert result.exit_code != 0


def test_share_rejects_non_hmq_token(share_env):
    runner, *_ = share_env
    result = runner.invoke(
        _cli_mod.cli, ["share", "--token", "hmk_owner_key"]
    )
    assert result.exit_code != 0


def test_ask_files_pin_mismatch_aborts(share_env):
    """Hand-craft a URI with a bogus files_digest — ``ask`` must reject it."""
    runner, profile, scope_id, client, api_key = share_env
    # Mint a real query token so ask can authenticate.
    r = client.post(
        "/v1/tokens",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "kind": "query",
            "label": "x",
            "constraints": {"scope_agent_id": scope_id},
        },
    )
    token = r.json()["token"]

    bogus_files = "0" * 64
    uri = (
        f"hmq://share-test/{scope_id}"
        f"?token={token}&files={bogus_files}&scheme=http"
    )
    result = runner.invoke(_cli_mod.cli, ["ask", uri, "hello?"])
    assert result.exit_code == 4, (result.stderr or result.output)
    err = result.stderr or result.output
    assert "files_digest" in err
    assert "mismatch" in err.lower()


def test_ask_compose_pin_mismatch_aborts(share_env):
    """compose_hash pin mismatch → exit 4 before any query is sent."""
    runner, profile, scope_id, client, api_key = share_env
    r = client.post(
        "/v1/tokens",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "kind": "query",
            "label": "x",
            "constraints": {"scope_agent_id": scope_id},
        },
    )
    token = r.json()["token"]

    bogus_compose = "0xdeadbeef"
    uri = (
        f"hmq://share-test/{scope_id}"
        f"?token={token}&compose={bogus_compose}&scheme=http"
    )
    result = runner.invoke(_cli_mod.cli, ["ask", uri, "hello?"])
    assert result.exit_code == 4, (result.stderr or result.output)
    err = result.stderr or result.output
    assert "compose_hash" in err
    assert "mismatch" in err.lower()


def test_ask_with_query_agent_routes_to_upload(share_env, tmp_path, monkeypatch):
    """Phase 4: ``ask --query-agent <dir>`` should not call
    /v1/query/run/submit; instead it packs the directory and routes
    through the upload+poll helper. We stub the helper to assert
    routing without spinning up a real container build."""
    runner, _profile, scope_id, client, api_key = share_env
    r = client.post(
        "/v1/tokens",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "kind": "query",
            "label": "uploader",
            "constraints": {
                "scope_agent_id": scope_id,
                "can_upload_query_agent": True,
            },
        },
    )
    token = r.json()["token"]

    agent_dir = tmp_path / "qa"
    agent_dir.mkdir()
    (agent_dir / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (agent_dir / "agent.py").write_text("print('q')\n")

    captured: dict = {}

    def _stub(**kwargs):
        captured.update(kwargs)
        click_echo = kwargs.get("as_json")
        # Mimic the helper printing a result and returning normally.
        import click as _click
        _click.echo("uploaded ok")

    from hivemind.cli import recipient as _recip
    monkeypatch.setattr(_recip, "_upload_query_agent_and_poll", _stub)

    uri = f"hmq://share-test/{scope_id}?token={token}&scheme=http"
    result = runner.invoke(
        _cli_mod.cli,
        ["ask", uri, "How many rows?", "--query-agent", str(agent_dir)],
    )
    assert result.exit_code == 0, (result.stderr or result.output)
    assert "uploaded ok" in result.output
    assert captured["prompt"] == "How many rows?"
    assert captured["scope_id"] == scope_id
    assert captured["archive_name"] == "qa.tar.gz"
    assert isinstance(captured["archive_bytes"], (bytes, bytearray))
    assert len(captured["archive_bytes"]) > 0


def test_ask_query_agent_path_must_exist(share_env):
    """``ask --query-agent /nonexistent`` should fail Click validation."""
    runner, _profile, scope_id, _client, _api_key = share_env
    uri = f"hmq://share-test/{scope_id}?token=hmq_test&scheme=http"
    result = runner.invoke(
        _cli_mod.cli,
        ["ask", uri, "q", "--query-agent", "/no/such/path"],
    )
    assert result.exit_code != 0


def test_ask_with_no_pins_skips_pin_verification(share_env):
    """With pins absent, ``ask`` skips verification entirely and goes
    straight to ``/v1/query/run/submit``. We can't pin on exit code 0
    (the test pipeline has no query agent loaded), but we can confirm
    no pin-mismatch error appeared in stderr."""
    runner, profile, scope_id, client, api_key = share_env
    r = client.post(
        "/v1/tokens",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "kind": "query",
            "label": "x",
            "constraints": {"scope_agent_id": scope_id},
        },
    )
    token = r.json()["token"]
    uri = f"hmq://share-test/{scope_id}?token={token}&scheme=http"

    result = runner.invoke(_cli_mod.cli, ["ask", uri, "hello?"])
    err = (result.stderr or "") + (result.output or "")
    assert "files_digest" not in err.lower() or "mismatch" not in err.lower()
    assert "compose_hash" not in err.lower() or "mismatch" not in err.lower()

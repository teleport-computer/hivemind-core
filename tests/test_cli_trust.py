"""Integration tests for the CLI's trust check (``_require_trust``).

Drives the click CLI via ``CliRunner`` and stubs the attestation
endpoint + trust store. Covers TOFU, change-detection, degraded
mode, and the three env-var escape hatches.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from hivemind import cli as _cli_mod
from hivemind import trust as _trust


@pytest.fixture
def _sandbox(tmp_path, monkeypatch):
    """Redirect trust store to tmp + pre-create a dummy .hivemind/config.yaml."""
    monkeypatch.setattr(_trust, "_TRUST_DIR", tmp_path)
    monkeypatch.setattr(_trust, "_TRUST_PATH", tmp_path / "trust.json")

    cfg_dir = tmp_path / ".hivemind"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        "service: https://cvm.example\napi_key: test-key\n"
    )
    monkeypatch.chdir(tmp_path)

    for var in ("HIVEMIND_TRUST_ALL", "HIVEMIND_TRUST_HASH", "HIVEMIND_NO_TRUST_CHECK"):
        monkeypatch.delenv(var, raising=False)

    yield tmp_path


def _stub_attestation(monkeypatch, bundle: dict):
    monkeypatch.setattr(_cli_mod, "_fetch_attestation", lambda service: bundle)


def test_trust_check_aborts_on_tofu_when_user_declines(_sandbox, monkeypatch):
    _stub_attestation(
        monkeypatch,
        {"ready": True, "attestation": {
            "compose_hash": "0xabc", "app_id": "appid",
        }},
    )
    runner = CliRunner()
    # `hivemind agents` hits the service — triggers _require_trust.
    result = runner.invoke(_cli_mod.cli, ["agents"], input="N\n")
    assert result.exit_code == 4
    assert "Aborted" in result.output or "Aborted" in (result.stderr_bytes or b"").decode()


def test_trust_check_records_approval_on_tofu_accept(_sandbox, monkeypatch):
    _stub_attestation(
        monkeypatch,
        {"ready": True, "attestation": {
            "compose_hash": "0xabc", "app_id": "appid",
        }},
    )
    # After user accepts, the HTTP call still proceeds — stub it to fail
    # cleanly so we don't need a live server. We only care that trust
    # was recorded before the HTTP call.
    monkeypatch.setattr(
        _cli_mod.httpx,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            _cli_mod.httpx.ConnectError("intentional")
        ),
    )

    runner = CliRunner()
    runner.invoke(_cli_mod.cli, ["agents"], input="y\n")

    entry = _trust.get_approved("https://cvm.example")
    assert entry is not None
    assert entry["approved_compose_hash"] == "0xabc"
    assert entry["app_id"] == "appid"


def test_trust_all_env_auto_approves_change(_sandbox, monkeypatch):
    _trust.record_approval("https://cvm.example", "0xold", app_id="appid")
    _stub_attestation(
        monkeypatch,
        {"ready": True, "attestation": {
            "compose_hash": "0xnew", "app_id": "appid",
        }},
    )
    monkeypatch.setenv("HIVEMIND_TRUST_ALL", "1")
    monkeypatch.setattr(
        _cli_mod.httpx,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            _cli_mod.httpx.ConnectError("intentional")
        ),
    )
    runner = CliRunner()
    runner.invoke(_cli_mod.cli, ["agents"])

    entry = _trust.get_approved("https://cvm.example")
    assert entry["approved_compose_hash"] == "0xnew"
    assert len(entry["history"]) == 1


def test_trust_hash_env_aborts_on_mismatch(_sandbox, monkeypatch):
    _stub_attestation(
        monkeypatch,
        {"ready": True, "attestation": {
            "compose_hash": "0xactual", "app_id": "appid",
        }},
    )
    monkeypatch.setenv("HIVEMIND_TRUST_HASH", "0xexpected")
    runner = CliRunner()
    result = runner.invoke(_cli_mod.cli, ["agents"])
    assert result.exit_code == 4


def test_trust_hash_env_approves_on_match(_sandbox, monkeypatch):
    _stub_attestation(
        monkeypatch,
        {"ready": True, "attestation": {
            "compose_hash": "0xabc", "app_id": "appid",
        }},
    )
    monkeypatch.setenv("HIVEMIND_TRUST_HASH", "0xabc")
    monkeypatch.setattr(
        _cli_mod.httpx,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            _cli_mod.httpx.ConnectError("intentional")
        ),
    )
    runner = CliRunner()
    runner.invoke(_cli_mod.cli, ["agents"])
    assert _trust.get_approved("https://cvm.example") is not None


def test_no_trust_check_env_skips(_sandbox, monkeypatch):
    _stub_attestation(
        monkeypatch,
        {"ready": True, "attestation": {
            "compose_hash": "0xabc", "app_id": "appid",
        }},
    )
    monkeypatch.setenv("HIVEMIND_NO_TRUST_CHECK", "1")
    monkeypatch.setattr(
        _cli_mod.httpx,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            _cli_mod.httpx.ConnectError("intentional")
        ),
    )
    runner = CliRunner()
    runner.invoke(_cli_mod.cli, ["agents"])
    # Nothing recorded — we skipped the check entirely.
    assert _trust.get_approved("https://cvm.example") is None


def test_degraded_mode_proceeds_with_warning(_sandbox, monkeypatch):
    _stub_attestation(
        monkeypatch,
        {"ready": False, "reason": "not_in_tee"},
    )
    monkeypatch.setattr(
        _cli_mod.httpx,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            _cli_mod.httpx.ConnectError("intentional")
        ),
    )
    runner = CliRunner()
    result = runner.invoke(_cli_mod.cli, ["agents"])
    # Didn't abort on trust check; failed later on the real HTTP call.
    assert result.exit_code != 4


def test_trusted_state_is_silent(_sandbox, monkeypatch):
    _trust.record_approval("https://cvm.example", "0xabc", app_id="appid")
    _stub_attestation(
        monkeypatch,
        {"ready": True, "attestation": {
            "compose_hash": "0xabc", "app_id": "appid",
        }},
    )
    monkeypatch.setattr(
        _cli_mod.httpx,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            _cli_mod.httpx.ConnectError("intentional")
        ),
    )
    runner = CliRunner()
    result = runner.invoke(_cli_mod.cli, ["agents"])
    # No TOFU prompt, no change warning — silent pass through the check.
    assert "compose hash" not in result.output.lower()


def test_trust_show_empty(_sandbox):
    runner = CliRunner()
    result = runner.invoke(_cli_mod.cli, ["trust", "show"])
    assert result.exit_code == 0
    assert "empty" in result.output


def test_trust_show_single_service(_sandbox):
    _trust.record_approval("https://a.example", "0xabc", app_id="a")
    runner = CliRunner()
    result = runner.invoke(_cli_mod.cli, ["trust", "show", "https://a.example"])
    assert result.exit_code == 0
    assert "0xabc" in result.output


def test_trust_reset_single(_sandbox):
    _trust.record_approval("https://a.example", "0xabc")
    _trust.record_approval("https://b.example", "0xdef")
    runner = CliRunner()
    result = runner.invoke(_cli_mod.cli, ["trust", "reset", "https://a.example"])
    assert result.exit_code == 0
    assert _trust.get_approved("https://a.example") is None
    assert _trust.get_approved("https://b.example") is not None


def test_trust_reset_all(_sandbox):
    _trust.record_approval("https://a.example", "0xabc")
    _trust.record_approval("https://b.example", "0xdef")
    runner = CliRunner()
    result = runner.invoke(_cli_mod.cli, ["trust", "reset", "--all"])
    assert result.exit_code == 0
    assert _trust.get_approved("https://a.example") is None
    assert _trust.get_approved("https://b.example") is None


def test_trust_reset_requires_arg_or_flag(_sandbox):
    runner = CliRunner()
    result = runner.invoke(_cli_mod.cli, ["trust", "reset"])
    assert result.exit_code == 1

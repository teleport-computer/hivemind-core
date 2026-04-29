"""Integration tests for the CLI's trust check (``_require_trust``).

Drives the click CLI via ``CliRunner`` and stubs the attestation
endpoint + trust store. Covers TOFU, change-detection, degraded
mode, and the three env-var escape hatches.
"""

from __future__ import annotations


import pytest
from click.testing import CliRunner

from hivemind import cli as _cli_mod
from hivemind import trust as _trust
from hivemind.cli import rooms as _rooms_cli

_ROOM_LINK = (
    "hmroom://invite/room_test?"
    "service=https%3A%2F%2Fcvm.example&token=hmq_test&owner_pubkey=test"
)


@pytest.fixture
def _sandbox(tmp_path, monkeypatch):
    """Isolate _HIVEMIND_HOME + trust store to tmp_path so the operator's
    real ~/.hivemind/profiles/default.yaml doesn't shadow the test profile."""
    hivemind_home = tmp_path / ".hivemind"
    profiles_dir = hivemind_home / "profiles"
    profiles_dir.mkdir(parents=True)

    monkeypatch.setattr(_cli_mod, "_HIVEMIND_HOME", hivemind_home)
    monkeypatch.setattr(_cli_mod, "_PROFILES_DIR", profiles_dir)
    monkeypatch.setattr(_cli_mod, "_ACTIVE_POINTER", hivemind_home / "active")
    monkeypatch.setattr(_trust, "_TRUST_DIR", hivemind_home)
    monkeypatch.setattr(_trust, "_TRUST_PATH", hivemind_home / "trust.json")

    (profiles_dir / "default.yaml").write_text(
        "service: https://cvm.example\napi_key: test-key\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HIVEMIND_PROFILE", raising=False)

    for var in (
        "HIVEMIND_TRUST_ALL",
        "HIVEMIND_TRUST_HASH",
        "HIVEMIND_NO_TRUST_CHECK",
        "HIVEMIND_ALLOW_DEGRADED_ATTESTATION",
        "HIVEMIND_REQUIRE_DCAP",
        "HIVEMIND_REQUIRE_TLS_PIN",
    ):
        monkeypatch.delenv(var, raising=False)
    # Most tests stub a fake HTTPS CVM without a real TDX quote. Preserve
    # the legacy trust-store behavior under an explicit degraded-attestation
    # opt-in, and cover the production default in a dedicated test below.
    monkeypatch.setenv("HIVEMIND_ALLOW_DEGRADED_ATTESTATION", "1")

    yield tmp_path


def _stub_attestation(monkeypatch, bundle: dict):
    # _fetch_attestation returns (bundle, observed_fingerprint_or_None).
    # For these trust-flow tests we're on http:// (no TLS), so fp is None.
    monkeypatch.setattr(
        _cli_mod,
        "_fetch_attestation",
        lambda service: (bundle, None),
    )


def test_remote_https_requires_full_attestation_by_default(
    _sandbox, monkeypatch
):
    monkeypatch.delenv("HIVEMIND_ALLOW_DEGRADED_ATTESTATION", raising=False)
    _stub_attestation(
        monkeypatch,
        {"ready": True, "attestation": {
            "compose_hash": "0xabc", "app_id": "appid",
        }},
    )
    runner = CliRunner()
    result = runner.invoke(_cli_mod.cli, ["room", "inspect", _ROOM_LINK])
    assert result.exit_code == 4
    assert "TDX quote" in result.output


def test_room_ask_omits_room_id_from_path_scoped_run_body(
    _sandbox, monkeypatch
):
    captured: dict = {}

    monkeypatch.setattr(
        _rooms_cli,
        "_fetch_verified_room",
        lambda *a, **kw: {
            "room": {
                "manifest_hash": "mh",
                "manifest": {
                    "trust": {
                        "mode": "operator_updates",
                        "allowed_composes": [],
                    }
                },
            },
            "attestation": {"attestation": {"compose_hash": "0xabc"}},
        },
    )
    monkeypatch.setattr(_rooms_cli, "_enforce_room_trust", lambda data: None)
    monkeypatch.setattr(
        _rooms_cli,
        "_hget",
        lambda *a, **kw: type(
            "Resp",
            (),
            {
                "status_code": 200,
                "json": lambda self: {
                    "attestation": {
                        "run_signer_pubkey_b64": "pub",
                        "compose_hash": "0xabc",
                    }
                },
            },
        )(),
    )

    def fake_query_tracked(service, headers, payload, **kwargs):
        captured["service"] = service
        captured["headers"] = headers
        captured["payload"] = payload
        captured["kwargs"] = kwargs

    monkeypatch.setattr(_rooms_cli, "_query_tracked", fake_query_tracked)

    result = CliRunner().invoke(
        _cli_mod.cli,
        [
            "room",
            "ask",
            _ROOM_LINK,
            "--provider",
            "tinfoil",
            "--model",
            "kimi-k2-6",
            "Show me top hashtags.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["payload"]["query"] == "Show me top hashtags."
    assert captured["payload"]["provider"] == "tinfoil"
    assert captured["payload"]["model"] == "kimi-k2-6"
    assert "room_id" not in captured["payload"]
    assert captured["kwargs"]["submit_path"] == "/v1/rooms/room_test/runs"


def test_trust_check_aborts_on_tofu_when_user_declines(_sandbox, monkeypatch):
    _stub_attestation(
        monkeypatch,
        {"ready": True, "attestation": {
            "compose_hash": "0xabc", "app_id": "appid",
        }},
    )
    runner = CliRunner()
    # A service-touching CLI command triggers _require_trust.
    result = runner.invoke(_cli_mod.cli, ["room", "inspect", _ROOM_LINK], input="N\n")
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
    runner.invoke(_cli_mod.cli, ["room", "inspect", _ROOM_LINK], input="y\n")

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
    runner.invoke(_cli_mod.cli, ["room", "inspect", _ROOM_LINK])

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
    result = runner.invoke(_cli_mod.cli, ["room", "inspect", _ROOM_LINK])
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
    runner.invoke(_cli_mod.cli, ["room", "inspect", _ROOM_LINK])
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
    runner.invoke(_cli_mod.cli, ["room", "inspect", _ROOM_LINK])
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
    result = runner.invoke(_cli_mod.cli, ["room", "inspect", _ROOM_LINK])
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
    result = runner.invoke(_cli_mod.cli, ["room", "inspect", _ROOM_LINK])
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

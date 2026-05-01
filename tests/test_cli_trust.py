"""Integration tests for the CLI's trust check (``_require_trust``).

Drives the click CLI via ``CliRunner`` and stubs the attestation
endpoint + trust store. Covers TOFU, change-detection, degraded
mode, and the three env-var escape hatches.
"""

from __future__ import annotations

import json


import pytest
from click.testing import CliRunner

from hivemind import cli as _cli_mod
from hivemind import trust as _trust
from hivemind.cli import _trust as _cli_trust
from hivemind.cli import admin as _admin_cli
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
    (_cli_mod._PROFILES_DIR / "default.yaml").write_text(
        "service: https://cvm.example\napi_key: hmk_test\n"
    )

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
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert "This room manifest has not been accepted" in result.output
    assert "Accepted room manifest mh" in result.output
    assert captured["payload"]["query"] == "Show me top hashtags."
    assert captured["payload"]["provider"] == "tinfoil"
    assert captured["payload"]["model"] == "kimi-k2-6"
    assert captured["headers"]["Authorization"] == "Bearer hmq_test"
    assert captured["headers"]["X-Hivemind-Api-Key"] == "hmk_test"
    assert "room_id" not in captured["payload"]
    assert captured["kwargs"]["submit_path"] == "/v1/rooms/room_test/runs"


def test_room_ask_uses_named_profile_api_key_for_billing(_sandbox, monkeypatch):
    captured: dict = {}
    (_cli_mod._PROFILES_DIR / "default.yaml").write_text(
        "service: https://cvm.example\napi_key: hmk_default\n"
    )
    (_cli_mod._PROFILES_DIR / "liz.yaml").write_text(
        "service: https://cvm.example\napi_key: hmk_liz\n"
    )

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
        captured["headers"] = headers

    monkeypatch.setattr(_rooms_cli, "_query_tracked", fake_query_tracked)

    result = CliRunner().invoke(
        _cli_mod.cli,
        [
            "--profile",
            "liz",
            "room",
            "ask",
            _ROOM_LINK,
            "Show me top hashtags.",
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert captured["headers"]["Authorization"] == "Bearer hmq_test"
    assert captured["headers"]["X-Hivemind-Api-Key"] == "hmk_liz"


def test_room_accept_records_manifest_before_ask(_sandbox, monkeypatch):
    captured: dict = {}
    (_cli_mod._PROFILES_DIR / "default.yaml").write_text(
        "service: https://cvm.example\napi_key: hmk_test\n"
    )

    monkeypatch.setattr(
        _rooms_cli,
        "_fetch_verified_room",
        lambda *a, **kw: {
            "room": {
                "room_id": "room_test",
                "manifest_hash": "mh",
                "manifest": {
                    "room_id": "room_test",
                    "name": "test room",
                    "scope": {
                        "agent_id": "scope_123",
                        "visibility": "inspectable",
                    },
                    "query": {
                        "mode": "fixed",
                        "agent_id": "query_123",
                        "visibility": "inspectable",
                    },
                    "mediator": {"agent_id": "med_123"},
                    "output": {"visibility": "querier_only"},
                    "egress": {"llm_providers": ["openrouter"]},
                    "trust": {
                        "mode": "operator_updates",
                        "allowed_composes": [],
                    },
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
        captured["payload"] = payload

    monkeypatch.setattr(_rooms_cli, "_query_tracked", fake_query_tracked)

    runner = CliRunner()
    accept = runner.invoke(_cli_mod.cli, ["room", "accept", _ROOM_LINK])

    assert accept.exit_code == 0, accept.output
    assert "Accepted for profile 'default'" in accept.output

    ask = runner.invoke(_cli_mod.cli, ["room", "ask", _ROOM_LINK, "hello"])

    assert ask.exit_code == 0, ask.output
    assert "This room manifest has not been accepted" not in ask.output
    assert captured["payload"]["query"] == "hello"


def test_room_ask_requires_billable_profile_for_invite_links(_sandbox):
    result = CliRunner().invoke(
        _cli_mod.cli,
        ["room", "ask", _ROOM_LINK, "Show me top hashtags."],
    )

    assert result.exit_code != 0
    assert "active tenant API key" in result.output
    assert "hivemind --profile NAME init --service URL --api-key hmk_" in result.output


def test_room_help_documents_spec_and_budget_defaults():
    runner = CliRunner()

    inspect = runner.invoke(_cli_mod.cli, ["room", "inspect", "--help"])
    assert inspect.exit_code == 0
    assert "jq '.room.manifest'" in inspect.output

    accept = runner.invoke(_cli_mod.cli, ["room", "accept", "--help"])
    assert accept.exit_code == 0
    assert "Accept a room manifest" in accept.output

    ask = runner.invoke(_cli_mod.cli, ["room", "ask", "--help"])
    assert ask.exit_code == 0
    assert "--timeout 600" in ask.output
    assert "--max-llm-calls 20" in ask.output
    assert "--max-tokens 100000" in ask.output
    assert "hosted cap" in ask.output
    assert "active hmk_" in ask.output


def test_admin_create_uses_admin_profile_without_trust_check(
    _sandbox, monkeypatch
):
    """Admin commands use the admin key directly; they must not require
    tenant-style attestation before sending the admin request."""

    profiles_dir = _cli_mod._PROFILES_DIR
    (profiles_dir / "admin.yaml").write_text(
        "service: https://cvm.example\napi_key: admin-key\nrole: admin\n"
    )
    _cli_mod._ACTIVE_POINTER.write_text("admin\n")

    def fail_trust(_config):
        raise AssertionError("admin create should not call _require_trust")

    monkeypatch.setattr(_cli_trust, "_require_trust", fail_trust)

    captured: dict = {}

    class Resp:
        status_code = 200

        def json(self):
            return {
                "tenant_id": "t_liz",
                "name": "liz",
                "db_name": "tenant_liz",
                "api_key": "hmk_liz",
            }

    def fake_hpost(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        captured["json"] = kwargs.get("json")
        return Resp()

    monkeypatch.setattr(_admin_cli, "_hpost", fake_hpost)

    result = CliRunner().invoke(
        _cli_mod.cli,
        ["admin", "tenants", "create", "liz"],
    )

    assert result.exit_code == 0, result.output
    assert captured["url"] == "https://cvm.example/v1/admin/tenants"
    assert captured["headers"]["Authorization"] == "Bearer admin-key"
    assert captured["json"] == {"name": "liz"}
    assert (
        "hivemind -y --profile liz init --service https://cvm.example "
        "--api-key hmk_liz"
    ) in result.output

    result_json = CliRunner().invoke(
        _cli_mod.cli,
        ["admin", "tenants", "create", "liz", "--json"],
    )
    assert result_json.exit_code == 0, result_json.output
    payload = json.loads(result_json.output)
    assert payload["tenant_setup_command"] == (
        "hivemind -y --profile liz init --service https://cvm.example "
        "--api-key hmk_liz"
    )

    result_duplicate = CliRunner().invoke(
        _cli_mod.cli,
        ["admin", "tenants", "create", "liz", "--allow-duplicate-name"],
    )
    assert result_duplicate.exit_code == 0, result_duplicate.output
    assert captured["json"] == {
        "name": "liz",
        "allow_duplicate_name": True,
    }


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

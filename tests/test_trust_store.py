"""Tests for ``hivemind/trust.py`` — CLI-side compose-hash trust store."""

from __future__ import annotations

import json

import pytest

from hivemind import trust


@pytest.fixture(autouse=True)
def _tmp_trust_path(tmp_path, monkeypatch):
    """Redirect the trust store to a tmp dir for every test."""
    monkeypatch.setattr(trust, "_TRUST_DIR", tmp_path)
    monkeypatch.setattr(trust, "_TRUST_PATH", tmp_path / "trust.json")
    yield


def test_empty_store_returns_default():
    store = trust.load_trust()
    assert store == {"version": 1, "services": {}}


def test_tofu_first_approval_records_app_id_and_first_seen():
    trust.record_approval(
        "https://cvm-8100.app.phala.network",
        "0xabc",
        app_id="feadbeef",
    )
    entry = trust.get_approved("https://cvm-8100.app.phala.network")
    assert entry is not None
    assert entry["approved_compose_hash"] == "0xabc"
    assert entry["app_id"] == "feadbeef"
    assert entry["approved_at"] == entry["first_seen_at"]
    assert entry["history"] == []


def test_second_approval_rotates_prior_hash_into_history():
    url = "https://cvm.example"
    trust.record_approval(url, "0xabc", app_id="a")
    trust.record_approval(url, "0xdef", app_id="a")
    entry = trust.get_approved(url)
    assert entry["approved_compose_hash"] == "0xdef"
    assert len(entry["history"]) == 1
    assert entry["history"][0]["hash"] == "0xabc"


def test_re_approving_same_hash_is_idempotent():
    url = "https://cvm.example"
    trust.record_approval(url, "0xabc", app_id="a")
    trust.record_approval(url, "0xabc", app_id="a")
    entry = trust.get_approved(url)
    assert len(entry["history"]) == 0


def test_re_approving_preserves_first_seen_at():
    url = "https://cvm.example"
    trust.record_approval(url, "0xabc", app_id="a")
    first = trust.get_approved(url)["first_seen_at"]
    trust.record_approval(url, "0xdef", app_id="a")
    second = trust.get_approved(url)
    assert second["first_seen_at"] == first


def test_url_trailing_slash_is_normalized():
    trust.record_approval("https://cvm.example/", "0xabc")
    assert trust.get_approved("https://cvm.example") is not None
    assert trust.get_approved("https://cvm.example/") is not None


def test_clear_single_service():
    trust.record_approval("https://a.example", "0x1")
    trust.record_approval("https://b.example", "0x2")
    n = trust.clear("https://a.example")
    assert n == 1
    assert trust.get_approved("https://a.example") is None
    assert trust.get_approved("https://b.example") is not None


def test_clear_all_services():
    trust.record_approval("https://a.example", "0x1")
    trust.record_approval("https://b.example", "0x2")
    n = trust.clear(None)
    assert n == 2
    assert trust.get_approved("https://a.example") is None


def test_corrupt_json_falls_back_to_default():
    trust._TRUST_DIR.mkdir(parents=True, exist_ok=True)
    trust._TRUST_PATH.write_text("not json at all", encoding="utf-8")
    store = trust.load_trust()
    assert store == {"version": 1, "services": {}}


def test_wrong_schema_version_falls_back_to_default():
    trust._TRUST_DIR.mkdir(parents=True, exist_ok=True)
    trust._TRUST_PATH.write_text(
        json.dumps({"version": 99, "services": {"x": {}}}),
        encoding="utf-8",
    )
    store = trust.load_trust()
    assert store == {"version": 1, "services": {}}


def test_evaluate_degraded_when_bundle_not_ready():
    decision = trust.evaluate(
        "https://cvm.example",
        {"ready": False, "reason": "not_in_tee"},
    )
    assert decision.status == "degraded"
    assert decision.reason == "not_in_tee"


def test_evaluate_degraded_when_compose_hash_missing():
    decision = trust.evaluate(
        "https://cvm.example",
        {"ready": True, "attestation": {"compose_hash": ""}},
    )
    assert decision.status == "degraded"
    assert decision.reason == "bundle_missing_compose_hash"


def test_evaluate_tofu_when_service_unknown():
    decision = trust.evaluate(
        "https://cvm.example",
        {"ready": True, "attestation": {
            "compose_hash": "0xabc",
            "app_id": "feed",
        }},
    )
    assert decision.status == "tofu"
    assert decision.current_hash == "0xabc"
    assert decision.app_id == "feed"


def test_evaluate_trusted_when_hash_matches():
    trust.record_approval("https://cvm.example", "0xabc", app_id="feed")
    decision = trust.evaluate(
        "https://cvm.example",
        {"ready": True, "attestation": {
            "compose_hash": "0xabc",
            "app_id": "feed",
        }},
    )
    assert decision.status == "trusted"


def test_evaluate_changed_when_hash_differs():
    trust.record_approval("https://cvm.example", "0xabc", app_id="feed")
    decision = trust.evaluate(
        "https://cvm.example",
        {"ready": True, "attestation": {
            "compose_hash": "0xdef",
            "app_id": "feed",
        }},
    )
    assert decision.status == "changed"
    assert decision.current_hash == "0xdef"
    assert decision.approved_hash == "0xabc"

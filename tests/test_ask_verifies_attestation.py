"""Phase 5: CLI verifies CVM-signed attestation envelopes (strict-default).

Smoke-test the recipient verification surface by feeding
``_emit_run_result`` data shapes the server produces. Each test isolates
one failure mode the recipient cares about:

  • envelope missing → fail closed (exit 6)
  • signature valid but pubkey mismatch → fail closed
  • output text tampered (output_hash diverges) → fail closed
  • valid envelope → output prints, exit 0
  • compose_hash mismatch → fail closed
  • --no-strict-attestation prints anyway with a warning
"""

from __future__ import annotations

import base64

import click
import pytest

from hivemind.cli._shared import _emit_run_result, _verify_run_attestation
from hivemind.run_signer import (
    canonical_json,
    derive_run_signer,
    sign_payload,
)


class _FakeKeyResp:
    def __init__(self, key: bytes):
        self.key = key


class _FakeDstack:
    def __init__(self, seed: bytes = b"\xa1" * 32):
        self._seed = seed

    def get_key(self, path: str, _purpose: str):
        return _FakeKeyResp(self._seed)


def _signed_run_record(
    *,
    output: str = "the answer",
    compose_hash: str = "ab" * 32,
    seed: bytes = b"\xa1" * 32,
) -> tuple[dict, str]:
    """Build a server-shaped run row + return (data, expected_pubkey_b64)."""
    import hashlib
    priv, pub = derive_run_signer(_FakeDstack(seed))
    body = {
        "schema_version": 1,
        "run_id": "r-1",
        "status": "completed",
        "compose_hash": compose_hash,
        "scope_agent_id": "s",
        "scope_files_digest": "",
        "scope_attested_files_digest": "",
        "query_agent_id": "q",
        "query_files_digest": "",
        "query_attested_files_digest": "",
        "prompt_hash": hashlib.sha256(b"p").hexdigest(),
        "output_hash": hashlib.sha256(
            output.encode("utf-8", errors="replace")
        ).hexdigest(),
        "error_hash": "",
        "timestamp": 1700000000,
        "signer_pubkey_b64": base64.b64encode(pub).decode("ascii"),
    }
    sig, _ = sign_payload(priv, body)
    data = {
        "run_id": "r-1",
        "status": "completed",
        "output": output,
        "artifacts": [],
        "attestation": {
            "body": body,
            "signature_b64": base64.b64encode(sig).decode("ascii"),
            "signer_pubkey_b64": base64.b64encode(pub).decode("ascii"),
        },
    }
    return data, base64.b64encode(pub).decode("ascii")


def test_verify_helper_accepts_signed_record():
    data, pub_b64 = _signed_run_record()
    ok, reason = _verify_run_attestation(
        data,
        expected_pubkey_b64=pub_b64,
        expected_compose_hash="ab" * 32,
        expected_output="the answer",
    )
    assert ok, reason


def test_verify_helper_rejects_missing_envelope():
    ok, reason = _verify_run_attestation({"output": "x", "artifacts": []})
    assert not ok
    assert "envelope" in reason or "no attestation" in reason


def test_verify_helper_rejects_pubkey_mismatch():
    data, _pub_b64 = _signed_run_record()
    ok, reason = _verify_run_attestation(
        data,
        expected_pubkey_b64=base64.b64encode(b"\x00" * 32).decode("ascii"),
    )
    assert not ok
    assert "pubkey" in reason


def test_verify_helper_rejects_output_tamper():
    data, pub_b64 = _signed_run_record(output="real answer")
    # Server returns *modified* output but old signed body.
    data["output"] = "tampered answer"
    ok, reason = _verify_run_attestation(
        data,
        expected_pubkey_b64=pub_b64,
        expected_output="tampered answer",
    )
    assert not ok
    assert "output_hash" in reason


def test_verify_helper_rejects_compose_mismatch():
    data, pub_b64 = _signed_run_record(compose_hash="aa" * 32)
    ok, reason = _verify_run_attestation(
        data,
        expected_pubkey_b64=pub_b64,
        expected_compose_hash="ff" * 32,
    )
    assert not ok
    assert "compose_hash" in reason


def test_verify_helper_rejects_signature_tamper():
    data, pub_b64 = _signed_run_record()
    # Flip a bit in the signature → must fail to verify.
    raw = bytearray(base64.b64decode(data["attestation"]["signature_b64"]))
    raw[0] ^= 0x01
    data["attestation"]["signature_b64"] = base64.b64encode(bytes(raw)).decode(
        "ascii"
    )
    ok, reason = _verify_run_attestation(
        data, expected_pubkey_b64=pub_b64,
    )
    assert not ok
    assert "signature" in reason or "verify" in reason


# ── CLI emit path ──


def test_emit_strict_aborts_when_envelope_missing(capsys):
    data = {"output": "hi", "artifacts": []}
    with pytest.raises(SystemExit) as excinfo:
        _emit_run_result(
            "http://x",
            data,
            "r-1",
            as_json=False,
            fetch=False,
            strict_attestation=True,
        )
    assert excinfo.value.code == 6
    captured = capsys.readouterr()
    assert "attestation failed" in captured.err


def test_emit_strict_prints_with_valid_envelope(capsys):
    data, pub_b64 = _signed_run_record(output="payload")
    _emit_run_result(
        "http://x",
        data,
        "r-1",
        as_json=False,
        fetch=False,
        expected_pubkey_b64=pub_b64,
        strict_attestation=True,
    )
    captured = capsys.readouterr()
    assert "payload" in captured.out
    assert "✓ signed by enclave" in captured.err


def test_emit_no_strict_prints_with_warning(capsys):
    """``--no-strict-attestation`` lets unsigned output through but
    still prints a warning so the user sees the missing signature."""
    data = {"output": "hi", "artifacts": []}
    _emit_run_result(
        "http://x",
        data,
        "r-1",
        as_json=False,
        fetch=False,
        strict_attestation=False,
    )
    captured = capsys.readouterr()
    assert "hi" in captured.out
    assert "✗" in captured.err


def test_emit_json_mode_attestation_failed(capsys):
    """JSON mode emits a structured error when verification fails."""
    import json as _json
    data = {"output": "hi", "artifacts": []}
    with pytest.raises(SystemExit) as excinfo:
        _emit_run_result(
            "http://x",
            data,
            "r-1",
            as_json=True,
            fetch=False,
            strict_attestation=True,
        )
    assert excinfo.value.code == 6
    captured = capsys.readouterr()
    parsed = _json.loads(captured.out)
    assert parsed["status"] == "attestation_failed"
    assert parsed["run_id"] == "r-1"

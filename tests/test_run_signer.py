"""Phase 5: run signer unit tests.

Round-trip the full sign/verify cycle without the dstack socket — a
fake DstackClient returns a deterministic seed, and we assert that:

1. ``derive_run_signer`` produces a stable pubkey for a stable seed
   (and a different pubkey for a different seed).
2. ``sign_payload`` + ``verify_payload`` round-trip.
3. Tampering with any field in the payload makes verification fail.
4. Canonical JSON is order-independent.
"""

from __future__ import annotations

import json

import pytest

from hivemind.run_signer import (
    canonical_json,
    derive_run_signer,
    sign_payload,
    verify_payload,
)


class _FakeKeyResp:
    def __init__(self, key: bytes):
        # Mirror dstack-sdk's response shape: ``.key`` is hex on the wire,
        # bytes after a transparent decoding pass. Test both branches.
        self.key = key


class _FakeDstack:
    def __init__(self, seed: bytes):
        self._seed = seed

    def get_key(self, path: str, _purpose: str):
        # Always serve the same seed regardless of path so the test can
        # assert that domain separation comes from the path-folded sha256
        # inside derive_run_signer.
        return _FakeKeyResp(self._seed)


def test_derive_run_signer_deterministic():
    seed = b"\x42" * 32
    priv1, pub1 = derive_run_signer(_FakeDstack(seed))
    priv2, pub2 = derive_run_signer(_FakeDstack(seed))
    assert pub1 == pub2, "same seed must produce same pubkey"
    # Two priv objects, but they must produce the same signature
    body = {"hello": "world"}
    sig1, _ = sign_payload(priv1, body)
    sig2, _ = sign_payload(priv2, body)
    assert sig1 == sig2, "Ed25519 signatures are deterministic"


def test_derive_run_signer_different_seeds():
    a, _ = derive_run_signer(_FakeDstack(b"\x01" * 32))
    b, _ = derive_run_signer(_FakeDstack(b"\x02" * 32))
    body = {"hi": 1}
    sig_a, _ = sign_payload(a, body)
    sig_b, _ = sign_payload(b, body)
    assert sig_a != sig_b


def test_sign_verify_round_trip():
    priv, pub = derive_run_signer(_FakeDstack(b"\xab" * 32))
    body = {
        "schema_version": 1,
        "run_id": "abc123",
        "compose_hash": "deadbeef",
        "output_hash": "cafe",
    }
    sig, _ = sign_payload(priv, body)
    assert verify_payload(pub, body, sig) is True


def test_verify_rejects_tampered_payload():
    priv, pub = derive_run_signer(_FakeDstack(b"\xab" * 32))
    body = {"a": 1, "b": 2}
    sig, _ = sign_payload(priv, body)
    tampered = dict(body, b=3)
    assert verify_payload(pub, tampered, sig) is False


def test_canonical_json_is_order_independent():
    a = canonical_json({"a": 1, "b": 2, "c": 3})
    b = canonical_json({"c": 3, "b": 2, "a": 1})
    assert a == b
    # Tightest separators (no whitespace).
    assert b" " not in a
    # Sorted keys.
    assert json.loads(a) == {"a": 1, "b": 2, "c": 3}


def test_verify_rejects_wrong_pubkey():
    priv_a, _ = derive_run_signer(_FakeDstack(b"\x01" * 32))
    _, pub_b = derive_run_signer(_FakeDstack(b"\x02" * 32))
    body = {"x": 1}
    sig, _ = sign_payload(priv_a, body)
    assert verify_payload(pub_b, body, sig) is False


def test_hex_seed_branch():
    """``dstack.get_key`` may return ``key`` as a hex string. The signer
    must accept both hex and bytes so we don't break against a real
    dstack server."""

    class _HexResp:
        def __init__(self, hex_str: str):
            self.key = hex_str

    class _HexDstack:
        def get_key(self, path: str, _purpose: str):
            return _HexResp("ab" * 32)

    priv, pub = derive_run_signer(_HexDstack())
    body = {"k": "v"}
    sig, _ = sign_payload(priv, body)
    assert verify_payload(pub, body, sig) is True


@pytest.mark.parametrize("payload", [
    {"a": 1, "nested": {"x": [1, 2, 3]}},
    {"s": "string", "n": None, "b": True},
    {"empty": ""},
])
def test_round_trip_various_shapes(payload):
    priv, pub = derive_run_signer(_FakeDstack(b"\xff" * 32))
    sig, _ = sign_payload(priv, payload)
    assert verify_payload(pub, payload, sig)

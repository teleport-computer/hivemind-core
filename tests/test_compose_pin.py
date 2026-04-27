"""Unit tests for tenant signing + ComposePin envelope.

Pure crypto / pydantic logic — no server, no DB. The server-side
endpoint round-trip is exercised separately.
"""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from hivemind.compose_pin import ComposePin, make_unsigned_pin
from hivemind.tenant_signing import (
    derive_signing_keypair,
    derive_signing_seed,
)


def _pub_bytes(pub: Ed25519PublicKey) -> bytes:
    return pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


# ── derivation ──────────────────────────────────────────────────────


def test_derivation_is_deterministic():
    s1 = derive_signing_seed("hmk_demo_token", "t_abc123")
    s2 = derive_signing_seed("hmk_demo_token", "t_abc123")
    assert s1 == s2 and len(s1) == 32


def test_different_token_yields_different_keypair():
    _, p1 = derive_signing_keypair("hmk_alpha", "t_abc")
    _, p2 = derive_signing_keypair("hmk_beta", "t_abc")
    assert _pub_bytes(p1) != _pub_bytes(p2)


def test_different_tenant_yields_different_keypair():
    _, p1 = derive_signing_keypair("hmk_same", "t_one")
    _, p2 = derive_signing_keypair("hmk_same", "t_two")
    assert _pub_bytes(p1) != _pub_bytes(p2)


def test_derivation_rejects_empty():
    with pytest.raises(ValueError):
        derive_signing_seed("", "t_abc")
    with pytest.raises(ValueError):
        derive_signing_seed("hmk_x", "")


# ── envelope round-trip ─────────────────────────────────────────────


def _fresh_pin(**overrides):
    base = dict(
        tenant_id="t_demo",
        allowed_composes=["a" * 64, "b" * 64],
        scope_agent_id="agent_scope",
        attested_files_digest="c" * 64,
        ttl_seconds=0,
    )
    base.update(overrides)
    return make_unsigned_pin(**base)


def test_sign_and_verify_round_trip():
    priv, pub = derive_signing_keypair("hmk_demo", "t_demo")
    pin = _fresh_pin().sign(priv)
    assert pin.signature
    assert pin.signer_pubkey
    assert pin.verify()
    assert pin.verify(expected_pubkey=_pub_bytes(pub))


def test_verify_rejects_wrong_expected_pubkey():
    priv, _ = derive_signing_keypair("hmk_demo", "t_demo")
    _, other_pub = derive_signing_keypair("hmk_other", "t_demo")
    pin = _fresh_pin().sign(priv)
    assert not pin.verify(expected_pubkey=_pub_bytes(other_pub))


def test_tampered_field_breaks_signature():
    priv, _ = derive_signing_keypair("hmk_demo", "t_demo")
    pin = _fresh_pin().sign(priv)
    pin.allowed_composes = ["d" * 64]  # post-sign mutation
    assert not pin.verify()


def test_swapping_pubkey_post_sign_breaks_verify():
    priv, _ = derive_signing_keypair("hmk_demo", "t_demo")
    _, other_pub = derive_signing_keypair("hmk_other", "t_demo")
    pin = _fresh_pin().sign(priv)
    import base64

    pin.signer_pubkey = base64.b64encode(_pub_bytes(other_pub)).decode()
    assert not pin.verify()


def test_unsigned_pin_does_not_verify():
    pin = _fresh_pin()
    assert not pin.verify()


def test_canonical_json_is_stable():
    priv, _ = derive_signing_keypair("hmk_demo", "t_demo")
    pin = _fresh_pin().sign(priv)
    j1 = pin.to_json()
    pin2 = ComposePin.from_json(j1)
    assert pin2.verify()
    assert pin2.to_json() == j1


def test_expiry_logic():
    priv, _ = derive_signing_keypair("hmk_demo", "t_demo")
    now = int(time.time())
    pin_no_exp = _fresh_pin(ttl_seconds=0).sign(priv)
    assert not pin_no_exp.is_expired(now)

    pin_future = _fresh_pin(ttl_seconds=3600).sign(priv)
    assert not pin_future.is_expired(now)
    assert not pin_future.is_expired(now + 3599)
    assert pin_future.is_expired(now + 3601)


def test_modifying_signature_breaks_verify():
    priv, _ = derive_signing_keypair("hmk_demo", "t_demo")
    pin = _fresh_pin().sign(priv)
    sig = pin.signature
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    pin.signature = flipped
    assert not pin.verify()


def test_allowed_composes_order_matters_in_signature():
    """Reordering allowed_composes after signing should invalidate the
    signature — list order is part of the signed payload (no
    set-equality short-circuit)."""
    priv, _ = derive_signing_keypair("hmk_demo", "t_demo")
    pin = _fresh_pin().sign(priv)
    pin.allowed_composes = list(reversed(pin.allowed_composes))
    assert not pin.verify()

"""Unit tests for the tenant-data seal.

Covers the pure crypto module (`hivemind.seal`) and the DB-backed
``ensure_unsealed`` bridge in `hivemind.tenant_seal`. These tests use
fast scrypt parameters (overridden via :class:`KdfParams`) so the suite
finishes in well under a second; production code uses the default
parameters from `seal.py`.

The DB-backed tests stand up a tiny in-memory shim (no Postgres needed)
that mimics the ``Database.execute`` / ``Database.execute_commit``
surface the seal helpers depend on.
"""

from __future__ import annotations

import pytest

from hivemind.seal import (
    KdfParams,
    TenantSealed,
    TenantSealer,
    decrypt_file,
    derive_kek,
    encrypt_file,
    file_aad,
    new_dek,
    new_salt,
    unwrap_dek,
    wrap_dek,
)
from hivemind.tenant_seal import (
    ensure_unsealed,
    load_seal_record,
    save_seal_record,
)


# Use cheap scrypt params throughout — production picks N=2**15.
FAST_PARAMS = KdfParams(n=2**10, r=8, p=1, length=32)


# ── Pure crypto ──────────────────────────────────────────────────────


def test_kek_derivation_is_deterministic():
    salt = b"\x00" * 16
    a = derive_kek("hmk_test", salt, FAST_PARAMS)
    b = derive_kek("hmk_test", salt, FAST_PARAMS)
    assert a == b
    assert len(a) == 32


def test_kek_derivation_changes_with_salt_or_token():
    salt1 = b"\x00" * 16
    salt2 = b"\x01" * 16
    base = derive_kek("hmk_a", salt1, FAST_PARAMS)
    assert derive_kek("hmk_a", salt2, FAST_PARAMS) != base
    assert derive_kek("hmk_b", salt1, FAST_PARAMS) != base


def test_derive_kek_rejects_short_salt_or_empty_token():
    with pytest.raises(ValueError):
        derive_kek("", b"\x00" * 16, FAST_PARAMS)
    with pytest.raises(ValueError):
        derive_kek("hmk_x", b"\x00", FAST_PARAMS)


def test_dek_wrap_unwrap_round_trip():
    kek = b"\x11" * 32
    dek = new_dek()
    wrapped = wrap_dek(kek, dek)
    assert wrapped != dek
    assert unwrap_dek(kek, wrapped) == dek


def test_dek_unwrap_fails_with_wrong_kek():
    dek = new_dek()
    wrapped = wrap_dek(b"\x11" * 32, dek)
    with pytest.raises(Exception):
        unwrap_dek(b"\x22" * 32, wrapped)


def test_file_round_trip_with_aad():
    dek = new_dek()
    aad = file_aad("t_demo", "agent_x", "agent.py")
    blob = encrypt_file(dek, "print('hello')\n", aad)
    assert blob[:0] == b""
    assert blob != b"print('hello')\n"
    assert decrypt_file(dek, blob, aad) == "print('hello')\n"


def test_file_decrypt_fails_with_wrong_aad():
    dek = new_dek()
    aad_a = file_aad("t_demo", "agent_x", "agent.py")
    aad_b = file_aad("t_demo", "agent_x", "OTHER.py")
    blob = encrypt_file(dek, "print('hello')\n", aad_a)
    with pytest.raises(Exception):
        decrypt_file(dek, blob, aad_b)


def test_file_decrypt_fails_with_wrong_dek():
    dek_a = new_dek()
    dek_b = new_dek()
    aad = file_aad("t_demo", "agent_x", "agent.py")
    blob = encrypt_file(dek_a, "print('hello')\n", aad)
    with pytest.raises(Exception):
        decrypt_file(dek_b, blob, aad)


def test_kdf_params_round_trip_json():
    p = KdfParams(n=2**14, r=8, p=2, length=32)
    out = KdfParams.from_json(p.to_json())
    assert out == p


def test_kdf_params_rejects_unsupported_kdf():
    import json
    bad = json.dumps({"kdf": "argon2", "n": 1, "r": 1, "p": 1, "length": 32})
    with pytest.raises(ValueError):
        KdfParams.from_json(bad)


# ── TenantSealer cache ───────────────────────────────────────────────


def test_sealer_starts_sealed():
    s = TenantSealer()
    assert s.is_unsealed("t_demo") is False
    with pytest.raises(TenantSealed):
        s.get_dek("t_demo")


def test_sealer_caches_after_unseal_with():
    s = TenantSealer()
    salt = new_salt()
    dek = new_dek()
    kek = derive_kek("hmk_owner", salt, FAST_PARAMS)
    wrapped = wrap_dek(kek, dek)

    out = s.unseal_with("t_demo", "hmk_owner", salt, wrapped, FAST_PARAMS)
    assert out == dek
    assert s.is_unsealed("t_demo")
    assert s.get_dek("t_demo") == dek


def test_sealer_unseal_with_wrong_token_raises():
    s = TenantSealer()
    salt = new_salt()
    dek = new_dek()
    kek = derive_kek("hmk_owner", salt, FAST_PARAMS)
    wrapped = wrap_dek(kek, dek)
    with pytest.raises(Exception):
        s.unseal_with("t_demo", "hmk_other", salt, wrapped, FAST_PARAMS)
    # Failed unseal must leave the tenant sealed.
    assert not s.is_unsealed("t_demo")


def test_sealer_evict_returns_to_sealed():
    s = TenantSealer()
    s.cache("t_demo", new_dek())
    assert s.is_unsealed("t_demo")
    s.evict("t_demo")
    assert not s.is_unsealed("t_demo")
    with pytest.raises(TenantSealed):
        s.get_dek("t_demo")


def test_sealer_cache_rejects_wrong_length_dek():
    s = TenantSealer()
    with pytest.raises(ValueError):
        s.cache("t_demo", b"\x00" * 16)  # not 32 bytes


# ── ensure_unsealed (DB-backed) ──────────────────────────────────────


class FakeDb:
    """Tiny stand-in for :class:`hivemind.db.Database` covering the
    parts ``tenant_seal`` actually touches: ``execute`` for SELECTs and
    ``execute_commit`` for the upsert. Stores rows by primary key
    (``singleton``)."""

    def __init__(self) -> None:
        self.row: dict | None = None

    def execute(self, sql: str, params: list | None = None) -> list[dict]:
        sql_up = sql.upper()
        if "FROM _HIVEMIND_TENANT_KEK" in sql_up:
            return [self.row] if self.row is not None else []
        raise NotImplementedError(sql)

    def execute_commit(self, sql: str, params: list | None = None) -> int:
        sql_up = sql.upper()
        if "INSERT INTO _HIVEMIND_TENANT_KEK" in sql_up:
            assert params is not None
            salt_b64, wrapped_b64, kdf_json, _ts = params
            self.row = {
                "salt": salt_b64,
                "wrapped_dek": wrapped_b64,
                "kdf_params": kdf_json,
            }
            return 1
        raise NotImplementedError(sql)


def test_ensure_unsealed_initializes_on_empty_record():
    sealer = TenantSealer()
    db = FakeDb()
    ok = ensure_unsealed(
        sealer, db, "t_demo", "hmk_owner", can_initialize=True,
    )
    assert ok is True
    assert sealer.is_unsealed("t_demo")
    assert db.row is not None  # persisted

    # A second call with the same owner key must succeed and yield the
    # same DEK — proves the wrapped record round-trips.
    dek_first = sealer.get_dek("t_demo")
    sealer.evict("t_demo")
    ok2 = ensure_unsealed(
        sealer, db, "t_demo", "hmk_owner", can_initialize=True,
    )
    assert ok2 is True
    assert sealer.get_dek("t_demo") == dek_first


def test_ensure_unsealed_capability_token_on_cold_record_stays_sealed():
    sealer = TenantSealer()
    db = FakeDb()
    # No record yet, capability tokens may not initialize.
    ok = ensure_unsealed(
        sealer, db, "t_demo", "hmq_cap", can_initialize=False,
    )
    assert ok is False
    assert not sealer.is_unsealed("t_demo")
    assert db.row is None


def test_ensure_unsealed_wrong_owner_returns_false():
    sealer = TenantSealer()
    db = FakeDb()
    # Owner initializes.
    assert ensure_unsealed(
        sealer, db, "t_demo", "hmk_owner", can_initialize=True,
    )
    sealer.evict("t_demo")

    # A different bearer can't unwrap → False, sealed.
    ok = ensure_unsealed(
        sealer, db, "t_demo", "hmk_intruder", can_initialize=True,
    )
    assert ok is False
    assert not sealer.is_unsealed("t_demo")


def test_load_save_round_trip_through_fake_db():
    db = FakeDb()
    salt = new_salt()
    dek = new_dek()
    kek = derive_kek("hmk_owner", salt, FAST_PARAMS)
    wrapped = wrap_dek(kek, dek)

    save_seal_record(db, salt, wrapped, FAST_PARAMS)
    rec = load_seal_record(db)
    assert rec is not None
    rec_salt, rec_wrapped, rec_params = rec
    assert rec_salt == salt
    assert rec_wrapped == wrapped
    assert rec_params == FAST_PARAMS
    # And the wrapped DEK still unwraps with the matching KEK.
    assert unwrap_dek(kek, rec_wrapped) == dek

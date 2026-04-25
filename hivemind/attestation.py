"""Remote attestation for hivemind-core.

Serves a TDX quote + measurements bundle at ``GET /v1/attestation`` so
the CLI can pin the CVM's compose_hash and prompt the user on change.

Pattern ported from feedling-mcp-v1's ``/attestation`` endpoint
(see /Users/sxysun/Desktop/suapp/feedling-mcp-v1/backend/enclave_app.py).

Bootstrap runs once in the FastAPI lifespan startup — the quote is
over data that never changes for the life of the process, so caching
is correct. Outside a TEE (no dstack socket) we fall back to
``{"ready": false, "reason": "not_in_tee"}`` so local dev still works.

Report-data binding (v1):
    sha256(b"hivemind-core-v1" || app_version.encode()) || 0x01 || 0x00 * 31

Report-data binding (v2, with TLS cert pinning — feedling's first binding):
    sha256(b"hivemind-core-v2" || app_version.encode() || sha256(cert_der))
    || 0x02 || 0x00 || 0x00 * 30

v2 embeds ``sha256(cert_der)`` of the enclave's deterministic TLS cert
(derived from dstack-KMS at key path ``hivemind-tls-v1``) so a CLI
can reconstruct the first 32 bytes from ``app_version`` + the TLS
fingerprint it observed on the wire and compare. Layout mirrors
feedling-mcp-v1/backend/enclave_app.py::build_report_data (same
``binding || version || flag || reserved`` shape), so the verifier
is a direct translation between repos. v1 is preserved when TLS
derivation is disabled — ``HIVEMIND_ENCLAVE_TLS=1`` turns v2 on.

The authoritative compose-hash binding lives in
``measurements.mr_config_id``, which dstack itself writes into the
quote: ``0x01 || sha256(app_compose) || 0x00*15``.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any

from .version import APP_VERSION

_REPORT_DATA_VERSION_TAG = b"hivemind-core-v1"
_REPORT_DATA_V2_TAG = b"hivemind-core-v2"

_state: dict[str, Any] = {
    "ready": False,
    "reason": None,
    "attestation": None,
    "booted_at": None,
}


def _build_report_data_v1() -> bytes:
    binding = hashlib.sha256(
        _REPORT_DATA_VERSION_TAG + APP_VERSION.encode("utf-8")
    ).digest()
    return binding + b"\x01" + b"\x00" * 31


def _build_report_data_v2(cert_fingerprint: bytes) -> bytes:
    """v2: bind the TLS cert fingerprint into REPORT_DATA.

    Layout (64 bytes total) — mirrors feedling's build_report_data:
      sha256(tag || app_version || fingerprint)  [32]  binding
      || 0x02                                     [ 1]  version byte
      || 0x00                                     [ 1]  flag byte (0x01 = placeholder TLS off)
      || 0x00 * 30                                [30]  reserved

    The CLI reconstructs the first 32 bytes from ``app_version`` and
    its locally-observed TLS fingerprint; mismatch → MITM. The tail
    bytes are fixed and carry no secret material — same shape as
    feedling-mcp-v1/backend/enclave_app.py so verifiers translate.
    """
    if len(cert_fingerprint) != 32:
        raise ValueError("cert_fingerprint must be 32 bytes")
    binding = hashlib.sha256(
        _REPORT_DATA_V2_TAG
        + APP_VERSION.encode("utf-8")
        + cert_fingerprint
    ).digest()
    version_byte = b"\x02"
    flag_byte = b"\x00"
    reserved = b"\x00" * 30
    return binding + version_byte + flag_byte + reserved


def _parse_mr_config_id(quote_hex: str) -> str:
    """Pull mr_config_id out of the raw quote bytes.

    dstack writes ``0x01 || sha256(app_compose) || 0x00*15`` into
    mr_config_id on real deployments; the simulator leaves all zeros.
    The TDX SDK's ``TcbInfo`` doesn't expose this register, so we parse
    the quote directly. Layout: TD Report starts at offset 48 in the
    TDX quote; mr_config_id is 48 bytes at body+184.
    """
    try:
        qbytes = bytes.fromhex(quote_hex)
        return qbytes[48 + 184 : 48 + 184 + 48].hex()
    except Exception:
        return ""


def _app_auth_metadata() -> dict[str, Any]:
    """Assemble the `app_auth` block exposed in the bundle.

    Empty contract address → everything here is empty; the CLI will
    skip the on-chain check and fall back to TOFU/change prompts.
    The bundle still carries the chain id + RPC URL hints so the CLI
    user can audit even before the contract is wired in.
    """
    # Lazy import keeps the test fixture that monkey-patches Settings
    # safe — nothing instantiates here at import time.
    from .config import Settings

    try:
        cfg = Settings()
    except Exception:
        return {
            "contract": "",
            "chain_id": 0,
            "rpc_url": "",
            "explorer_base_url": "",
        }
    return {
        "contract": cfg.app_auth_contract or "",
        "chain_id": cfg.app_auth_chain_id,
        "rpc_url": cfg.app_auth_rpc_url,
        "explorer_base_url": cfg.app_auth_explorer_base_url,
    }


def bootstrap() -> None:
    """Fetch the quote + measurement registers once at startup.

    Safe to call outside a TEE — sets ``ready=false`` with a reason
    rather than raising, so the rest of the app boots normally.
    Idempotent: a second call is a no-op once we're ready.
    """
    if _state.get("ready"):
        return
    try:
        from dstack_sdk import DstackClient  # type: ignore[import-not-found]
    except ImportError as e:
        _state["ready"] = False
        _state["reason"] = f"dstack_sdk not installed: {e}"
        return

    try:
        dstack = DstackClient()

        # ── TLS binding (v2) ──
        # When HIVEMIND_ENCLAVE_TLS=1 we derive a stable TLS cert from
        # dstack-KMS and fold sha256(cert_der) into report_data so the
        # CLI can pin the fingerprint cryptographically.
        tls_bundle: dict[str, Any] | None = None
        cert_fingerprint_hex = ""
        report_data_version = 1
        if os.environ.get("HIVEMIND_ENCLAVE_TLS"):
            try:
                from . import tls as _tls

                tls_bundle = _tls.derive_tls_cert_and_key(dstack)
                cert_fingerprint_hex = tls_bundle["fingerprint"].hex()
                report_data_version = 2
            except Exception as tls_e:
                _state["ready"] = False
                _state["reason"] = (
                    f"tls derivation failed: {tls_e!r}"
                )
                return

        if report_data_version == 2 and tls_bundle:
            report_data = _build_report_data_v2(tls_bundle["fingerprint"])
        else:
            report_data = _build_report_data_v1()

        quote_resp = dstack.get_quote(report_data)
        info = dstack.info()
        tcb = info.tcb_info

        quote_hex = (
            quote_resp.quote
            if isinstance(quote_resp.quote, str)
            else quote_resp.quote.hex()
        )

        _state["attestation"] = {
            "tdx_quote_hex": quote_hex,
            "event_log_json": getattr(quote_resp, "event_log", "") or "",
            "measurements": {
                "mrtd": tcb.mrtd,
                "rtmr0": tcb.rtmr0,
                "rtmr1": tcb.rtmr1,
                "rtmr2": tcb.rtmr2,
                "rtmr3": tcb.rtmr3,
                "mr_aggregated": tcb.mr_aggregated,
                "mr_config_id": _parse_mr_config_id(quote_hex),
            },
            "compose_hash": info.compose_hash,
            "app_id": info.app_id,
            "instance_id": info.instance_id,
            "hivemind_version": APP_VERSION,
            "report_data_version": report_data_version,
            "app_auth": _app_auth_metadata(),
            "tls": {
                "enabled": tls_bundle is not None,
                "cert_fingerprint_sha256_hex": cert_fingerprint_hex,
                "cert_pem": (
                    tls_bundle["cert_pem"].decode("ascii")
                    if tls_bundle
                    else ""
                ),
            },
        }
        # Stash cert/key for the server to consume — server.py reads
        # these and passes them to uvicorn.
        if tls_bundle is not None:
            _state["tls_cert_pem"] = tls_bundle["cert_pem"]
            _state["tls_key_pem"] = tls_bundle["key_pem"]
        _state["booted_at"] = time.time()
        _state["ready"] = True
        _state["reason"] = None
    except Exception as e:
        _state["ready"] = False
        _state["reason"] = f"bootstrap failed: {e!r}"


def get_bundle() -> dict[str, Any]:
    """Return the cached attestation bundle for ``GET /v1/attestation``."""
    if not _state["ready"]:
        return {
            "ready": False,
            "reason": _state["reason"] or "not_bootstrapped",
            "hivemind_version": APP_VERSION,
        }
    return {
        "ready": True,
        "booted_at": _state["booted_at"],
        "attestation": _state["attestation"],
    }


def get_tls_material() -> tuple[bytes, bytes] | None:
    """Return ``(cert_pem, key_pem)`` if TLS-in-enclave is active.

    Called by ``server.py`` during lifespan startup to decide whether
    to hand uvicorn an SSL context.
    """
    cert = _state.get("tls_cert_pem")
    key = _state.get("tls_key_pem")
    if cert and key:
        return cert, key
    return None

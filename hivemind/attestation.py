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

The binding is intentionally thin — we don't yet have a per-boot
enclave pubkey (that ships with client-side row encryption), so there
is nothing to bind except the release identity. The authoritative
compose-hash binding lives in ``measurements.mr_config_id``, which
dstack itself writes into the quote: ``0x01 || sha256(app_compose) || 0x00*15``.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from .version import APP_VERSION

_REPORT_DATA_VERSION_TAG = b"hivemind-core-v1"

_state: dict[str, Any] = {
    "ready": False,
    "reason": None,
    "attestation": None,
    "booted_at": None,
}


def _build_report_data() -> bytes:
    binding = hashlib.sha256(
        _REPORT_DATA_VERSION_TAG + APP_VERSION.encode("utf-8")
    ).digest()
    version_byte = b"\x01"
    return binding + version_byte + b"\x00" * 31


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


def bootstrap() -> None:
    """Fetch the quote + measurement registers once at startup.

    Safe to call outside a TEE — sets ``ready=false`` with a reason
    rather than raising, so the rest of the app boots normally.
    """
    try:
        from dstack_sdk import DstackClient  # type: ignore[import-not-found]
    except ImportError as e:
        _state["ready"] = False
        _state["reason"] = f"dstack_sdk not installed: {e}"
        return

    try:
        dstack = DstackClient()
        report_data = _build_report_data()
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
            "report_data_version": 1,
        }
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

"""Client-side DCAP / TDX quote verification.

Feedling's second binding (quote verification) for Hivemind, done in
the CLI. Uses the vendored ``dcap-qvl`` Rust crate (Phala-Network,
v0.4.0) built as a PyO3 wheel.

What this buys us:

* The CLI trusts the cryptographically verified ``mr_config_id``
  from the quote, NOT the ``compose_hash`` the server sends in the
  bundle. A compromised or lying server can't forge the register —
  it's signed by the Intel PCK chain rooted at the Intel SGX Root CA.
* TCB status is surfaced to the user: ``UpToDate``,
  ``OutOfDate``, ``Revoked``, etc. The CLI can refuse to connect
  on ``Revoked``.

The module is optional: if ``dcap_qvl`` isn't installed, calls return
a ``DcapResult(status="unavailable", ...)`` and callers can fall
back to the unverified bundle. We never fail-open on DCAP — if the
user explicitly enables strict DCAP via ``HIVEMIND_REQUIRE_DCAP=1``
and the wheel isn't present, the CLI aborts.

Collateral source defaults to Phala's PCCS mirror
(``https://pccs.phala.network``), overridable via
``HIVEMIND_PCCS_URL``. The mirror caches Intel's PCS responses so we
don't pay the Intel API's rate limit on every CLI invocation.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any


@dataclass
class DcapResult:
    """Outcome of a DCAP verification attempt.

    ``status`` values:
      - ``verified``    → quote validated against Intel SGX Root CA;
                          verified_compose_hash is authoritative.
      - ``tcb_issue``   → quote validated but TCB status is not UpToDate
                          (OutOfDate, SWHardeningNeeded, etc).
                          Caller may choose to warn or abort.
      - ``revoked``     → quote validated but PCK cert chain is revoked.
                          Caller SHOULD abort.
      - ``invalid``     → cryptographic check failed. Caller MUST abort.
      - ``unavailable`` → dcap_qvl wheel not installed.
      - ``network``     → could not fetch collateral (offline etc).
    """

    status: str
    verified_compose_hash: str = ""
    verified_app_id: str = ""
    tcb_status: str = ""
    advisory_ids: tuple[str, ...] = ()
    reason: str = ""


def available() -> bool:
    try:
        import dcap_qvl  # noqa: F401
    except ImportError:
        return False
    return True


def _compose_hash_from_mr_config_id(mr_config_id_hex: str) -> str:
    """Extract the hex compose_hash from a verified TD10 mr_config_id.

    dstack encodes it as ``0x01 || sha256(app_compose) || zeros[14]``
    (48 bytes total = 96 hex chars). The sha256 sits at offset 1..33.
    Pre-dstack or simulator quotes (all zeros) return "".
    """
    h = mr_config_id_hex.lower().removeprefix("0x")
    if len(h) != 96:
        return ""
    if h[:2] != "01":
        return ""
    return h[2:66]


def _app_id_from_rt_mr3(rt_mr3_hex: str) -> str:
    """Best-effort app-id extraction (first 20 bytes of rt_mr3).

    The exact mapping is dstack-specific; we surface what we can and
    leave final comparison to the caller (the bundle also carries
    app_id over server-authenticated JSON).
    """
    h = rt_mr3_hex.lower().removeprefix("0x")
    if len(h) < 40:
        return ""
    return h[:40]


def verify_quote(quote_hex: str, *, pccs_url: str = "") -> DcapResult:
    """Verify a TDX quote and return the authoritative compose_hash.

    Synchronous wrapper around ``dcap_qvl.get_collateral_and_verify``.
    Runs the crate's async collateral fetch in a fresh event loop.
    """
    try:
        import dcap_qvl as dq
    except ImportError:
        return DcapResult(
            status="unavailable",
            reason="dcap_qvl wheel not installed",
        )

    try:
        quote_bytes = bytes.fromhex(quote_hex.removeprefix("0x"))
    except ValueError as e:
        return DcapResult(status="invalid", reason=f"bad quote hex: {e}")

    url = (pccs_url or os.environ.get("HIVEMIND_PCCS_URL", "")).strip()
    url = url or dq.PHALA_PCCS_URL

    async def _run() -> Any:
        collateral = await dq.get_collateral(url, quote_bytes)
        import time
        return dq.verify(quote_bytes, collateral, int(time.time()))

    try:
        report = asyncio.run(_run())
    except RuntimeError as e:
        msg = str(e)
        if "event loop is already running" in msg.lower():
            # Unlikely in the CLI (we call this from sync click), but be
            # defensive: fall back to running in a worker thread.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                report = pool.submit(lambda: asyncio.run(_run())).result()
        else:
            return DcapResult(status="network", reason=f"runtime: {e}")
    except Exception as e:
        return DcapResult(status="network", reason=f"collateral fetch: {e}")

    # Parse the verified report JSON (PyO3 type doesn't expose TD10 directly).
    import json as _json
    try:
        payload = _json.loads(report.to_json())
    except Exception as e:
        return DcapResult(status="invalid", reason=f"report json: {e}")

    tcb = payload.get("status", "")
    advisory = tuple(payload.get("advisory_ids", []) or ())
    td10 = (payload.get("report") or {}).get("TD10") or {}
    verified_hash = _compose_hash_from_mr_config_id(td10.get("mr_config_id", ""))
    verified_app_id = _app_id_from_rt_mr3(td10.get("rt_mr3", ""))

    if tcb == "Revoked":
        status = "revoked"
    elif tcb == "UpToDate":
        status = "verified"
    elif tcb:
        status = "tcb_issue"
    else:
        status = "invalid"

    return DcapResult(
        status=status,
        verified_compose_hash=verified_hash,
        verified_app_id=verified_app_id,
        tcb_status=tcb,
        advisory_ids=advisory,
    )


def verify_report_data_v2(
    report_data_hex: str,
    *,
    observed_fingerprint: bytes,
    hivemind_version: str,
) -> bool:
    """Reconstruct the expected report_data v2 and compare.

    Layout we expect (mirrors feedling's build_report_data):
      sha256(b"hivemind-core-v2" || hivemind_version || fingerprint)  [32]
      || 0x02                                                          [ 1]
      || 0x00                                                          [ 1]
      || 0x00 * 30                                                     [30]

    Returns True iff the first 32 bytes — the cryptographic binding —
    match the reconstruction for the given fingerprint. The tail is
    not secret (same as feedling: version byte + flag byte +
    reserved zeros) so we only assert the version byte here.
    """
    import hashlib

    if len(observed_fingerprint) != 32:
        return False
    h = report_data_hex.lower().removeprefix("0x")
    if len(h) != 128:
        return False
    try:
        rd = bytes.fromhex(h)
    except ValueError:
        return False
    if rd[32] != 0x02:
        return False
    expected = hashlib.sha256(
        b"hivemind-core-v2"
        + hivemind_version.encode("utf-8")
        + observed_fingerprint
    ).digest()
    return rd[:32] == expected


def extract_report_data_hex(quote_hex: str) -> str:
    """Pull the 64-byte ``report_data`` out of a TDX quote.

    TDX quote: header (48 bytes) + TD report; report_data sits at
    offset ``body + 520`` for TD 1.0 reports. Returns ``""`` on parse
    failure.
    """
    try:
        qbytes = bytes.fromhex(quote_hex.removeprefix("0x"))
    except ValueError:
        return ""
    # TDX quote body starts at offset 48; report_data is 64 bytes at body+520
    off = 48 + 520
    if len(qbytes) < off + 64:
        return ""
    return qbytes[off : off + 64].hex()


__all__ = [
    "DcapResult",
    "available",
    "extract_report_data_hex",
    "verify_quote",
    "verify_report_data_v2",
]

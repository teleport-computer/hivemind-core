"""Remote attestation, DCAP, TLS-pin verification, and on-chain governance gate."""

import hashlib as _hashlib
import json as _json
import os
import ssl as _ssl
from urllib.parse import urlparse as _urlparse

import click
import httpx

from .. import dcap as _dcap
from .. import onchain as _onchain
from .. import reproduce as _reproduce
from .. import trust as _trust
from ._http import _api_error, _hget, _pin_service_cert


# ── Remote attestation / compose-hash consent ──
#
# Every command that hits the service calls ``_require_trust`` before
# the first request. It fetches ``GET /v1/attestation`` and compares
# the CVM's current ``compose_hash`` against the locally-cached
# approval. Three environment-variable escape hatches for CI:
#
#   HIVEMIND_TRUST_ALL=1          auto-approve any change
#   HIVEMIND_TRUST_HASH=0x...     abort unless hash matches (strict CI)
#   HIVEMIND_NO_TRUST_CHECK=1     skip the check entirely (dev)
#
# If the attestation bundle includes an ``app_auth`` block with a
# non-empty contract address, the CLI queries the on-chain registry
# first. An approved hash auto-accepts silently; a revoked hash
# hard-aborts with no y/N prompt. Unknown/unreachable contracts fall
# back to the local TOFU / change-prompt flow (fail-closed on the
# local store — never fail-open on the RPC).


def _fetch_attestation(service: str) -> tuple[dict, bytes | None]:
    """Fetch ``/v1/attestation`` and capture the peer cert fingerprint.

    Returns ``(bundle, observed_fingerprint_or_None)``. The fingerprint
    is ``sha256(cert.DER)`` of the cert the server presented during the
    TLS handshake — non-None only when the service URL is ``https://``.

    Self-signed certs are tolerated (verify_mode=CERT_NONE) because
    feedling's trust model pins the cert via attestation, not via the
    public-CA chain — the CLI re-verifies the observed fingerprint
    against the value cryptographically bound into the TDX quote's
    REPORT_DATA v2 immediately after. A MITM that served its own cert
    would be caught in that check.
    """
    url = f"{service.rstrip('/')}/v1/attestation"
    parsed = _urlparse(url)
    if parsed.scheme == "https":
        return _fetch_attestation_https(parsed)
    # Plain http — no TLS, no fingerprint to pin.
    try:
        r = _hget(url, timeout=5)
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        return ({"ready": False, "reason": f"fetch_failed: {e!r}"}, None)
    if r.status_code != 200:
        return (
            {
                "ready": False,
                "reason": f"http_{r.status_code}: {_api_error(r)}",
            },
            None,
        )
    try:
        return (r.json(), None)
    except ValueError:
        return ({"ready": False, "reason": "invalid_json"}, None)


def _fetch_attestation_https(parsed) -> tuple[dict, bytes | None]:
    """Fetch /v1/attestation over HTTPS, capturing peer cert DER.

    Uses http.client directly so we can reach into the underlying
    ssl socket and call ``getpeercert(binary_form=True)`` before the
    connection closes. httpx doesn't expose the raw cert reliably.
    """
    import http.client as _httpc

    host = parsed.hostname or ""
    port = parsed.port or 443
    path = parsed.path or "/v1/attestation"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    try:
        conn = _httpc.HTTPSConnection(host, port, timeout=5, context=ctx)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        fp: bytes | None = None
        try:
            der = conn.sock.getpeercert(binary_form=True)  # type: ignore[union-attr]
        except Exception:
            der = None
        if der:
            fp = _hashlib.sha256(der).digest()
        conn.close()
    except (OSError, _ssl.SSLError) as e:
        return ({"ready": False, "reason": f"fetch_failed: {e!r}"}, None)

    if resp.status != 200:
        return (
            {"ready": False, "reason": f"http_{resp.status}"},
            fp,
        )
    try:
        return (_json.loads(body.decode("utf-8")), fp)
    except (ValueError, UnicodeDecodeError):
        return ({"ready": False, "reason": "invalid_json"}, fp)


def _fetch_cert_fingerprint(url: str) -> bytes | None:
    """Open a TLS connection to ``url`` and return ``sha256(cert.DER)``.

    No HTTP request is sent — we only need the peer cert from the
    handshake. Used for Tier-3 cross-pinning when the user's service
    URL is fronted by dstack-ingress (LE cert, fingerprint ≠ REPORT_DATA)
    but the raw `-<port>s.<gateway>` URL still exposes the enclave cert.

    Returns None on any error — caller decides whether that's fatal.
    """
    parsed = _urlparse(url)
    if parsed.scheme != "https":
        return None
    host = parsed.hostname or ""
    port = parsed.port or 443
    if not host:
        return None
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    try:
        import socket as _sock

        with _sock.create_connection((host, port), timeout=8) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, _ssl.SSLError):
        return None
    if not der:
        return None
    return _hashlib.sha256(der).digest()


def _dcap_augment(bundle: dict) -> dict:
    """Cryptographically verify the quote and pin the compose_hash.

    If verification succeeds, returns a new bundle whose
    ``attestation.compose_hash`` is the hash extracted from the
    verified ``mr_config_id`` register — overriding whatever the server
    claimed. If verification can't run, the bundle passes through
    unchanged (unless HIVEMIND_REQUIRE_DCAP=1, which hard-aborts).
    """
    if os.environ.get("HIVEMIND_DISABLE_DCAP"):
        return bundle
    if not bundle.get("ready"):
        return bundle

    require = bool(os.environ.get("HIVEMIND_REQUIRE_DCAP"))

    if not _dcap.available():
        if require:
            click.echo(
                "Error: HIVEMIND_REQUIRE_DCAP=1 but the dcap_qvl wheel "
                "is not installed. Build it from "
                "/Users/sxysun/Desktop/suapp/feedling-mcp-v1/ios/vendor/"
                "dcap-qvl/python-bindings (maturin build --release) and "
                "`uv pip install` the wheel.",
                err=True,
            )
            raise SystemExit(4)
        return bundle

    att = bundle.get("attestation") or {}
    quote_hex = att.get("tdx_quote_hex") or ""
    if not quote_hex:
        return bundle

    result = _dcap.verify_quote(quote_hex)

    if result.status == "verified":
        # Replace the server-claimed hash with the cryptographically
        # verified one and annotate the bundle for downstream UX.
        new_att = {
            **att,
            "compose_hash": result.verified_compose_hash
            or att.get("compose_hash", ""),
            "dcap": {
                "status": result.status,
                "tcb_status": result.tcb_status,
                "advisory_ids": list(result.advisory_ids),
            },
        }
        return {**bundle, "attestation": new_att}

    if result.status in {"revoked", "invalid"}:
        click.echo(
            f"\nError: TDX quote verification FAILED ({result.status}).\n"
            f"  Reason: {result.reason or result.tcb_status}\n"
            "The CVM's attestation cannot be trusted. Aborting.",
            err=True,
        )
        raise SystemExit(4)

    if result.status == "tcb_issue":
        click.echo(
            f"! TCB warning: status={result.tcb_status}; "
            f"advisories={list(result.advisory_ids)}. "
            "Quote cryptographically valid but platform TCB is not "
            "UpToDate. Continuing.",
            err=True,
        )
        new_att = {
            **att,
            "compose_hash": result.verified_compose_hash
            or att.get("compose_hash", ""),
            "dcap": {
                "status": result.status,
                "tcb_status": result.tcb_status,
                "advisory_ids": list(result.advisory_ids),
            },
        }
        return {**bundle, "attestation": new_att}

    # network / unavailable → warn and fall back
    if require:
        click.echo(
            f"Error: HIVEMIND_REQUIRE_DCAP=1 but DCAP failed "
            f"({result.status}): {result.reason}",
            err=True,
        )
        raise SystemExit(4)
    click.echo(
        f"! DCAP skipped ({result.status}): {result.reason or '(no detail)'}. "
        "Falling back to server-claimed compose hash.",
        err=True,
    )
    return bundle


def _consult_app_auth(bundle: dict, compose_hash: str) -> bool | None:
    """Ask the on-chain registry whether `compose_hash` is approved.

    Returns True  → approved  (auto-accept, no prompt).
            False → revoked   (hard-abort).
            None  → unknown / not configured / RPC unreachable
                    (fall back to local TOFU / change-prompt flow).

    The contract address and RPC URL are carried in the attestation
    bundle itself — the CLI trusts *where to look*, not *what the
    answer is*. The answer comes from the chain.

    Override via env:
      HIVEMIND_APP_AUTH_RPC   — override the RPC URL (mirrored in bundle).
      HIVEMIND_DISABLE_APP_AUTH=1 — skip the check entirely.
    """
    if os.environ.get("HIVEMIND_DISABLE_APP_AUTH"):
        return None
    if not compose_hash:
        return None
    att = (bundle.get("attestation") or {}).get("app_auth") or {}
    contract = att.get("contract") or ""
    if not contract:
        return None
    rpc_url = (
        os.environ.get("HIVEMIND_APP_AUTH_RPC", "").strip()
        or att.get("rpc_url")
        or ""
    )
    if not rpc_url:
        return None
    return _onchain.is_app_allowed(rpc_url, contract, compose_hash)


def _release_metadata_for(
    bundle: dict, compose_hash: str
) -> dict | None:
    """Read `(approved, git_commit, compose_uri)` for ``compose_hash``.

    Same env / bundle plumbing as ``_consult_app_auth``; returns the
    decoded tuple from the on-chain `releases(bytes32)` getter, or
    ``None`` on any failure (no contract, RPC down, decode error,
    user disabled). Cheap separate call rather than hoisting into
    `_consult_app_auth` so the existing boolean-only code path stays
    untouched and the metadata fetch is best-effort UX sugar.
    """
    if os.environ.get("HIVEMIND_DISABLE_APP_AUTH"):
        return None
    if not compose_hash:
        return None
    att = (bundle.get("attestation") or {}).get("app_auth") or {}
    contract = att.get("contract") or ""
    if not contract:
        return None
    rpc_url = (
        os.environ.get("HIVEMIND_APP_AUTH_RPC", "").strip()
        or att.get("rpc_url")
        or ""
    )
    if not rpc_url:
        return None
    return _onchain.release_metadata(rpc_url, contract, compose_hash)


def _verify_tls_pin(
    bundle: dict,
    observed_fp: bytes | None,
    service: str | None = None,
) -> None:
    """Verify the live TLS fingerprint matches what's bound into the quote.

    This is feedling's first binding translated for hivemind: the
    enclave's deterministic TLS cert has ``sha256(cert.DER)`` folded
    into REPORT_DATA v2 during quote generation. The CLI extracts the
    raw report_data from the verified quote, reconstructs the
    binding from (app_version, observed_fingerprint), and aborts on
    mismatch — that's the signal that something between us and the
    enclave is serving its own cert.

    No-op when:
      - bundle is ``report_data_version=1`` (TLS-in-enclave off),
      - we didn't observe a fingerprint (plain http://),
      - dcap wheel isn't installed (can't parse quote).

    Toggle ``HIVEMIND_DISABLE_TLS_PIN=1`` to skip (dev only). Strict
    mode via ``HIVEMIND_REQUIRE_TLS_PIN=1`` aborts if we can't run
    the check (mirror of ``HIVEMIND_REQUIRE_DCAP``).
    """
    if os.environ.get("HIVEMIND_DISABLE_TLS_PIN"):
        return
    if not bundle.get("ready"):
        return
    att = bundle.get("attestation") or {}
    if att.get("report_data_version") != 2:
        return
    strict = bool(os.environ.get("HIVEMIND_REQUIRE_TLS_PIN"))
    if observed_fp is None:
        if strict:
            click.echo(
                "Error: HIVEMIND_REQUIRE_TLS_PIN=1 but service URL is "
                "not https:// — no live cert to pin against.",
                err=True,
            )
            raise SystemExit(4)
        return
    quote_hex = att.get("tdx_quote_hex") or ""
    if not quote_hex:
        return
    rd_hex = _dcap.extract_report_data_hex(quote_hex)
    if not rd_hex:
        if strict:
            click.echo(
                "Error: HIVEMIND_REQUIRE_TLS_PIN=1 but report_data could "
                "not be extracted from the quote.",
                err=True,
            )
            raise SystemExit(4)
        return
    hv = att.get("hivemind_version", "")
    if _dcap.verify_report_data_v2(
        rd_hex,
        observed_fingerprint=observed_fp,
        hivemind_version=hv,
    ):
        # Sanity check against the bundle's declared fingerprint too.
        declared = (att.get("tls") or {}).get(
            "cert_fingerprint_sha256_hex", ""
        ).lower()
        if declared and declared != observed_fp.hex():
            click.echo(
                f"\nError: bundle's declared cert fingerprint "
                f"({declared[:16]}…) disagrees with observed "
                f"({observed_fp.hex()[:16]}…) despite quote-binding "
                "match. This is an inconsistent server, not MITM, but "
                "it's suspicious enough to abort.",
                err=True,
            )
            raise SystemExit(4)
        # Pin: save the enclave's PEM and default every subsequent
        # httpx call to verify against it.
        cert_pem = (att.get("tls") or {}).get("cert_pem", "")
        if cert_pem:
            _pin_service_cert(observed_fp, cert_pem, service=service)
        return

    # User's URL fingerprint doesn't match REPORT_DATA. This is normally
    # MITM, BUT it's also expected when the service is fronted by
    # dstack-ingress (friendly URL → LE cert at the wire, enclave cert
    # one hop deeper at the raw passthrough URL). Try the fallback:
    # fetch the cert from the bundle-declared `pinning_url` and check
    # THAT one against REPORT_DATA. If that matches, Tier 3 is verified
    # via the alternate surface; the friendly URL relies on standard LE
    # cert validation (which httpx already enforces). Mismatch on both
    # → genuine MITM, abort.
    pinning_url = (att.get("tls") or {}).get("pinning_url", "") or ""
    if pinning_url and service and pinning_url.rstrip("/") != service.rstrip("/"):
        alt_fp = _fetch_cert_fingerprint(pinning_url)
        if alt_fp is not None and _dcap.verify_report_data_v2(
            rd_hex,
            observed_fingerprint=alt_fp,
            hivemind_version=hv,
        ):
            declared = (att.get("tls") or {}).get(
                "cert_fingerprint_sha256_hex", ""
            ).lower()
            if declared and declared != alt_fp.hex():
                click.echo(
                    f"\nError: bundle's declared cert fingerprint "
                    f"({declared[:16]}…) disagrees with the cert observed "
                    f"on the pinning surface {pinning_url} "
                    f"({alt_fp.hex()[:16]}…).",
                    err=True,
                )
                raise SystemExit(4)
            # Show the cert fingerprint (the actual thing being pinned)
            # instead of the raw `-8100s.` Phala-gateway URL. The URL is
            # an implementation detail of where the enclave cert lives;
            # the fingerprint is what's bound into the TDX quote and
            # what an auditor compares. Full URL still available via
            # `hivemind trust attest --raw` for anyone who wants it.
            click.echo(
                f"  ✓ Live remote attestation verified — your connection "
                f"terminates inside\n"
                f"    a TDX enclave bound to this quote "
                f"(cert sha256: {alt_fp.hex()[:16]}…).",
                err=True,
            )
            cert_pem = (att.get("tls") or {}).get("cert_pem", "")
            if cert_pem:
                # Pin only against the raw URL (where the enclave cert
                # lives). Friendly URL keeps normal LE validation.
                _pin_service_cert(alt_fp, cert_pem, service=pinning_url)
            return

    # Neither the user's URL nor the pinning_url presented a cert
    # bound into the quote — that's genuine MITM territory.
    click.echo(
        "\nError: TLS cert fingerprint does not match the value bound "
        "into the TDX quote's REPORT_DATA.\n"
        f"  Observed sha256(cert.DER): {observed_fp.hex()}\n"
        + (
            f"  Also tried pinning surface {pinning_url} — no match either.\n"
            if pinning_url else ""
        )
        + "  This is either a man-in-the-middle between your CLI and the "
        "enclave, or the server is running an old image whose quote was "
        "generated for a different cert.",
        err=True,
    )
    raise SystemExit(4)


def _require_trust(config: dict) -> None:
    """Gate on the remote compose_hash matching the local approval.

    Prompts the user on first-use (TOFU) or on hash change. Aborts
    with exit code 4 if the user declines. No-op when the remote is
    not TEE-backed (local dev); prints an amber warning on stderr.
    """
    if os.environ.get("HIVEMIND_NO_TRUST_CHECK"):
        click.echo(
            "! Trust check skipped (HIVEMIND_NO_TRUST_CHECK set).",
            err=True,
        )
        return

    service = config["service"]
    # Tests in tests/test_cli_trust.py monkey-patch
    # ``hivemind.cli._fetch_attestation``. Look up via the parent module
    # at call time so the patch takes effect.
    from . import _fetch_attestation as _fetch
    bundle, observed_fp = _fetch(service)

    # ── DCAP quote verification (feedling's second binding) ──
    # When the vendored dcap_qvl wheel is installed, we cryptographically
    # verify the TDX quote against the Intel SGX Root CA before trusting
    # any claim in the bundle. On success we replace the server-claimed
    # compose_hash with the hash extracted from the verified
    # mr_config_id — the server can't forge that.
    #
    # Toggles:
    #   HIVEMIND_REQUIRE_DCAP=1  → abort if DCAP can't run or fails.
    #   HIVEMIND_DISABLE_DCAP=1  → skip DCAP entirely (dev).
    bundle = _dcap_augment(bundle)

    # ── TLS-cert-fingerprint pinning (feedling's first binding) ──
    # When the bundle is report_data_version=2 and we observed a cert
    # fingerprint on the wire, reconstruct the expected REPORT_DATA
    # first-32-byte binding from (app_version, observed_fp) and compare
    # against the quote. Mismatch → MITM. Layout matches feedling's
    # build_report_data exactly, so the verifier is a direct translation.
    _verify_tls_pin(bundle, observed_fp, service=service)

    decision = _trust.evaluate(service, bundle)

    # ── On-chain governance gate (feedling's third binding) ──
    # Runs before the local trust store because the chain is the
    # authoritative source: an approved hash short-circuits every
    # prompt; a revoked hash hard-aborts with no escape hatch.
    if decision.status in {"trusted", "tofu", "changed"}:
        onchain_verdict = _consult_app_auth(bundle, decision.current_hash)
        if onchain_verdict is True:
            # Silent auto-accept on subsequent connects (status=trusted).
            # On first-trust (tofu) or hash-change, surface the source
            # pointer once so the user can eyeball where the running
            # build came from. _trust.record_approval flips the local
            # state to "trusted" so this only fires once per service per
            # compose_hash — same cadence as the existing TOFU prompt.
            first_or_changed = decision.status != "trusted"
            if first_or_changed:
                _trust.record_approval(
                    service, decision.current_hash, decision.app_id
                )
                meta = _release_metadata_for(bundle, decision.current_hash)
                if meta and meta.get("compose_uri"):
                    src = _reproduce.short_source(
                        meta.get("git_commit", ""),
                        meta.get("compose_uri", ""),
                    )
                    click.echo(
                        f"  ✓ Source: {src}\n"
                        f"    (run `hivemind trust attest --reproduce` "
                        f"to walk the chain back to source).",
                        err=True,
                    )
            return
        if onchain_verdict is False:
            att = (bundle.get("attestation") or {}).get("app_auth") or {}
            contract = att.get("contract", "")
            chain_id = att.get("chain_id", _onchain.ETH_SEPOLIA_CHAIN_ID)
            link = _onchain.explorer_link(contract, chain_id) if contract else ""
            click.echo(
                f"\nError: Remote compose hash is NOT approved on-chain.\n"
                f"  Hash:     {decision.current_hash}\n"
                f"  Contract: {contract}"
                + (f"\n  View:     {link}" if link else "")
                + "\n\nThe contract owner must call addComposeHash() "
                "before the CLI will connect.",
                err=True,
            )
            raise SystemExit(4)
        # onchain_verdict is None → fall through to local flow.

    if decision.status == "trusted":
        return

    if decision.status == "degraded":
        click.echo(
            f"! Remote attestation unavailable ({decision.reason}). "
            f"Treating as dev mode.",
            err=True,
        )
        return

    pinned_hash = os.environ.get("HIVEMIND_TRUST_HASH", "").strip()
    if pinned_hash:
        if decision.current_hash == pinned_hash:
            _trust.record_approval(
                service, decision.current_hash, decision.app_id
            )
            return
        click.echo(
            f"Error: Remote compose_hash {decision.current_hash} does not "
            f"match HIVEMIND_TRUST_HASH={pinned_hash}.",
            err=True,
        )
        raise SystemExit(4)

    if os.environ.get("HIVEMIND_TRUST_ALL"):
        _trust.record_approval(
            service, decision.current_hash, decision.app_id
        )
        return

    # Interactive prompt.
    skip_hint = (
        "  Skip this prompt next time:\n"
        "    --yes                            auto-approve (TLS pin + on-chain revoke still enforced)\n"
        "    --dangerously-skip-attestations  disable all verification (DEV / localhost only)"
    )
    if decision.status == "tofu":
        click.echo(
            f"\nFirst connection to {service}.\n"
            f"  App ID:        {decision.app_id or '(unknown)'}\n"
            f"  Compose hash:  {decision.current_hash}\n\n"
            f"{skip_hint}\n",
            err=True,
        )
        prompt = "Trust this compose hash and continue? [y/N]: "
    else:  # changed
        click.echo(
            f"\n! Remote compose hash changed since last connection.\n"
            f"  Service:   {service}\n"
            f"  App ID:    {decision.app_id or '(unknown)'}\n"
            f"  Old hash:  {decision.approved_hash}\n"
            f"  New hash:  {decision.current_hash}\n\n"
            f"{skip_hint}\n",
            err=True,
        )
        prompt = "Approve the new hash and continue? [y/N]: "

    try:
        answer = (
            click.prompt(prompt, default="N", show_default=False)
            .strip()
            .lower()
        )
    except (click.Abort, EOFError):
        click.echo(
            "Aborted — no input available. Re-run with --yes (auto-approve) "
            "or --dangerously-skip-attestations (full skip, dev only) for "
            "non-interactive use.\n"
            "Env-var equivalents: HIVEMIND_TRUST_ALL=1, "
            "HIVEMIND_TRUST_HASH=<hex>, HIVEMIND_NO_TRUST_CHECK=1.",
            err=True,
        )
        raise SystemExit(4)
    if answer not in {"y", "yes"}:
        click.echo("Aborted — compose hash not approved.", err=True)
        raise SystemExit(4)
    _trust.record_approval(service, decision.current_hash, decision.app_id)

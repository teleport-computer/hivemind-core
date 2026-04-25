"""Hivemind CLI — conditional recall for the privacy-quality frontier.

Remote client for a hivemind-core service deployed in a TEE.

Usage:
  hivemind init [--service <url>] [--api-key <key>]
  hivemind load <file> [--table <name>] [--format sql|csv|jsonl]
  hivemind scope "<english rules>" | --from-file <path>
  hivemind share
  hivemind query "<question>"
  hivemind run <path> [--prompt "..."] [--json] [--fetch]
  hivemind runs [<run_id>]
  hivemind agents
"""

import hashlib as _hashlib
import io
import json as _json
import os
import socket as _socket
import ssl as _ssl
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import urlparse as _urlparse

import click
import httpx
import yaml

from . import dcap as _dcap
from . import onchain as _onchain
from . import trust as _trust

# Machine-global profile root. Holds named tenant identities, a side-by-
# side `trust.json`, and pinned enclave-TLS PEMs. Multi-profile support
# means you can keep separate identities (admin vs. watch-history tenant
# vs. alice tenant) on one laptop without `cd`-ing between dirs.
_HIVEMIND_HOME = Path.home() / ".hivemind"
_PROFILES_DIR = _HIVEMIND_HOME / "profiles"
_ACTIVE_POINTER = _HIVEMIND_HOME / "active"
_DEFAULT_PROFILE = "default"

# Legacy CWD-scoped config we auto-migrate from (one-shot).
_LEGACY_CONFIG_DIR = ".hivemind"
_LEGACY_CONFIG_FILE = "config.yaml"

_REPO_ROOT = Path(__file__).parent.parent
_AGENTS_DIR = _REPO_ROOT / "agents"
_DEFAULT_SERVICE = "http://localhost:8100"

_DOCKERFILE_MD_AGENT = """\
FROM hivemind-agent-base:latest
COPY _bridge.py .
COPY agent.py .
COPY prompt.md .
CMD ["python", "/app/agent.py"]
"""


# ── Pinned httpx wrappers ──
#
# When the enclave terminates TLS (``HIVEMIND_ENCLAVE_TLS=1``) the
# server serves a self-signed cert derived from dstack-KMS. Default
# ``httpx.get/post/...`` would reject it with a CA-chain error. After
# ``_require_trust`` has cryptographically verified that the live
# cert fingerprint matches what's bound into the TDX quote, we save
# the verified PEM and pin subsequent calls *to that host* against it.
# That turns the one-shot quote check into a continuous transport-level
# binding for the rest of the process.
#
# Pins are keyed by host (scheme://host[:port]) so that a deployment
# fronted by both a friendly URL (LE cert via dstack-ingress) and a
# raw -8100s pinning URL (self-signed enclave cert) coexist correctly:
# the friendly URL keeps verifying against system CAs; only the raw
# URL uses the pinned enclave PEM. Without per-host scoping a request
# to the friendly URL would try to validate an LE cert against the
# enclave PEM and fail with CERTIFICATE_VERIFY_FAILED.

_SERVICE_VERIFY_BY_HOST: dict[str, str] = {}


def _host_key(url: str) -> str:
    """``scheme://host[:port]`` for ``url``. Empty when not parseable."""
    try:
        p = _urlparse(url)
    except Exception:
        return ""
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}".lower()


def _verify_for_url(url: str) -> str | bool:
    """Pin path for ``url``'s host, or ``True`` (default system CAs)."""
    pin = _SERVICE_VERIFY_BY_HOST.get(_host_key(url))
    return pin if pin else True


def _hget(url: str, **kw):
    kw.setdefault("verify", _verify_for_url(url))
    return httpx.get(url, **kw)


def _hpost(url: str, **kw):
    kw.setdefault("verify", _verify_for_url(url))
    return httpx.post(url, **kw)


def _hput(url: str, **kw):
    kw.setdefault("verify", _verify_for_url(url))
    return httpx.put(url, **kw)


def _hdelete(url: str, **kw):
    kw.setdefault("verify", _verify_for_url(url))
    return httpx.delete(url, **kw)


def _pin_path_for_fingerprint(fingerprint_hex: str) -> Path:
    """Path of the saved enclave PEM for ``fingerprint_hex``.

    Filename is keyed by the first 16 hex chars of sha256(cert.DER).
    That's plenty unique for a CLI's set of trusted enclaves and gives
    us one stable location per cert without leaking the full hash into
    the filesystem.
    """
    return Path.home() / ".hivemind" / f"enclave-tls-{fingerprint_hex[:16]}.pem"


def _pin_service_cert(
    observed_fp: bytes,
    cert_pem: str,
    service: str | None = None,
) -> None:
    """Persist the verified enclave cert and wire it as the default verify.

    Called after ``_verify_tls_pin`` has cryptographically confirmed
    the cert. Subsequent service requests through ``_hget/_hpost/...``
    will use this cert as their CA — so a mid-session MITM that serves
    a different cert fails the TLS handshake rather than returning
    plausible garbage.

    When ``service`` is provided we also stash the fingerprint in the
    trust store. Future CLI invocations for that URL can then warm up
    the pin from disk via ``_warm_pin_from_trust`` without redoing the
    quote verification. Without this stash, every fresh shell would
    re-fall-back to system CAs and break against -8100s. URLs.

    The pin is registered against ``service``'s host only. Other hosts
    (e.g. a friendly LE-fronted URL for the same deployment) keep
    using system CAs.
    """
    if not cert_pem:
        return
    pin_dir = Path.home() / ".hivemind"
    pin_dir.mkdir(parents=True, exist_ok=True)
    fp_hex = observed_fp.hex()
    pin_path = _pin_path_for_fingerprint(fp_hex)
    try:
        pin_path.write_text(cert_pem, encoding="utf-8")
    except OSError:
        return
    if service:
        host = _host_key(service)
        if host:
            _SERVICE_VERIFY_BY_HOST[host] = str(pin_path)
        try:
            _trust.record_cert_fingerprint(service, fp_hex)
        except Exception:
            # Trust store write failures shouldn't break the live
            # request — we already have the pin in-memory for this run.
            pass


def _warm_pin_from_trust(service: str) -> None:
    """Register the previously-pinned cert for ``service``'s host.

    The trust store tracks the verified cert fingerprint per service
    URL (set by ``_pin_service_cert``). When the matching PEM exists
    on disk we wire it under ``service``'s host so admin / init /
    everyday commands can talk to a -8100s. URL without first running
    ``_require_trust`` (admin commands have no tenant config to feed
    into ``_require_trust``).

    No-ops when the trust store has no fingerprint for the URL or the
    PEM file is missing — requests fall through to system CAs and (for
    HTTPS) fail loudly if untrusted, which is the correct behavior
    when nothing has been pinned yet.
    """
    host = _host_key(service)
    if not host or host in _SERVICE_VERIFY_BY_HOST:
        return
    entry = _trust.get_approved(service)
    if not entry:
        return
    fp_hex = entry.get("cert_fingerprint_sha256_hex") or ""
    if not fp_hex:
        return
    pin_path = _pin_path_for_fingerprint(fp_hex)
    if pin_path.exists():
        _SERVICE_VERIFY_BY_HOST[host] = str(pin_path)


# ── Config helpers ──


def _profile_name() -> str:
    """Active profile name. Resolution order:

    1. ``HIVEMIND_PROFILE`` env var (set by ``--profile NAME``)
    2. The persistent pointer file at ``~/.hivemind/active``
       (written by ``hivemind profile use NAME``)
    3. ``"default"`` — the legacy fallback

    Step 2 is what lets plain ``hivemind <cmd>`` work after a cluster
    migration leaves the literal "default" profile pointing at dead
    infra: the operator picks a real profile once via ``profile use``
    and forgets about it.
    """
    env = os.environ.get("HIVEMIND_PROFILE", "").strip()
    if env:
        return env
    try:
        if _ACTIVE_POINTER.exists():
            name = _ACTIVE_POINTER.read_text().strip()
            if name:
                return name
    except OSError:
        pass
    return _DEFAULT_PROFILE


def _set_active_profile(name: str) -> None:
    _HIVEMIND_HOME.mkdir(parents=True, exist_ok=True)
    _ACTIVE_POINTER.write_text(name + "\n")


def _clear_active_profile_if(name: str) -> None:
    """Remove the active pointer iff it currently points at ``name``."""
    try:
        if (
            _ACTIVE_POINTER.exists()
            and _ACTIVE_POINTER.read_text().strip() == name
        ):
            _ACTIVE_POINTER.unlink()
    except OSError:
        pass


def _config_path(profile: str | None = None) -> Path:
    name = profile or _profile_name()
    return _PROFILES_DIR / f"{name}.yaml"


def _legacy_config_path() -> Path:
    return Path(_LEGACY_CONFIG_DIR) / _LEGACY_CONFIG_FILE


def _maybe_migrate_legacy(target: Path) -> bool:
    """One-shot migration from `./.hivemind/config.yaml` → global default profile.

    Triggered when someone upgrades and runs a non-init command in the
    same directory they used the old layout from. Returns True if a
    migration happened.
    """
    legacy = _legacy_config_path()
    if not legacy.exists() or target.exists():
        return False
    if _profile_name() != _DEFAULT_PROFILE:
        # User explicitly asked for a non-default profile; don't silently
        # claim someone else's name.
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(legacy.read_bytes())
    click.echo(
        f"Migrated legacy {legacy} → {target} (default profile). "
        f"You can delete {_LEGACY_CONFIG_DIR}/ when ready.",
        err=True,
    )
    return True


def _load_config(*, check_trust: bool = True) -> dict:
    """Load the active profile's config or exit with error.

    When ``check_trust`` is True (default) this also runs the remote
    compose-hash consent check — prompting the user on first-use or on
    hash change. Pass ``check_trust=False`` from local-only commands
    (e.g. ``hivemind trust`` subcommands that inspect the store without
    contacting the CVM).
    """
    p = _config_path()
    _maybe_migrate_legacy(p)
    if not p.exists():
        profile = _profile_name()
        existing = (
            sorted(x.stem for x in _PROFILES_DIR.glob("*.yaml"))
            if _PROFILES_DIR.exists()
            else []
        )
        if profile == _DEFAULT_PROFILE and existing:
            hint = (
                f"Profile '{profile}' doesn't exist. Pick one of "
                f"{existing} via 'hivemind profile use NAME', "
                f"or pass --profile NAME on each command."
            )
        elif profile == _DEFAULT_PROFILE:
            hint = f"Run 'hivemind init --api-key …' to create profile '{profile}'."
        else:
            hint = f"Run 'hivemind --profile {profile} init --api-key …' to create it."
        click.echo(
            f"Error: profile '{profile}' not found at {p}. {hint}",
            err=True,
        )
        raise SystemExit(1)
    try:
        with open(p) as f:
            config = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        click.echo(f"Error: Corrupt config file {p}: {e}", err=True)
        raise SystemExit(1)
    if not config.get("service"):
        click.echo(
            f"Error: Config {p} missing 'service' URL. Run 'hivemind init' again.",
            err=True,
        )
        raise SystemExit(1)
    # Restore any pinned enclave cert for this URL before any HTTP call
    # so the trust-check fetch + downstream commands see the right CA
    # for self-signed Tier-3 certs. Cheap on every invocation: a single
    # JSON read + one stat() call.
    _warm_pin_from_trust(config["service"])
    if check_trust:
        _require_trust(config)
    return config


def _save_config(config: dict) -> None:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    # First-init UX: if no profile is currently marked as active, mark
    # this one. Subsequent inits don't change the pointer — switch
    # explicitly via `hivemind profile use NAME`. This makes a
    # one-tenant laptop work without ever passing --profile.
    if not _ACTIVE_POINTER.exists():
        _set_active_profile(_profile_name())


def _headers(config: dict) -> dict:
    h: dict[str, str] = {}
    if config.get("api_key"):
        h["Authorization"] = f"Bearer {config['api_key']}"
    return h


def _make_tarball(files: dict[str, str]) -> bytes:
    """Create a gzipped tarball from {filename: content} dict."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _tarball_from_dir(path: Path) -> bytes:
    """Pack a directory into a gzipped tarball (files at top level)."""
    if not path.exists():
        raise click.ClickException(f"Path not found: {path}")
    if not path.is_dir():
        raise click.ClickException(f"Not a directory: {path}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in sorted(path.rglob("*")):
            if f.is_file():
                if any(
                    part in {"__pycache__", ".git", ".venv", "node_modules"}
                    for part in f.parts
                ):
                    continue
                tar.add(f, arcname=str(f.relative_to(path)))
    data = buf.getvalue()
    if len(data) == 0:
        raise click.ClickException(f"No files found in {path}")
    return data


def _api_error(resp: httpx.Response) -> str:
    """Extract server detail field for nicer error messages."""
    try:
        j = resp.json()
        if isinstance(j, dict):
            return str(j.get("detail") or j.get("error") or resp.text)
    except Exception:
        pass
    return resp.text or f"HTTP {resp.status_code}"


def _http_get(url: str, headers: dict, timeout: float = 30) -> dict:
    try:
        r = _hget(url, headers=headers, timeout=timeout)
    except httpx.ConnectError:
        click.echo(f"Error: Cannot reach {url}", err=True)
        raise SystemExit(2)
    except httpx.TimeoutException:
        click.echo(f"Error: Timed out fetching {url}", err=True)
        raise SystemExit(2)
    if r.status_code >= 400:
        click.echo(f"Error {r.status_code}: {_api_error(r)}", err=True)
        raise SystemExit(3)
    return r.json()


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
            click.echo(
                f"  Tier-3 TLS pin: verified via {pinning_url}",
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
    bundle, observed_fp = _fetch_attestation(service)

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
            # Silent auto-accept. Record in local store so subsequent
            # runs print the "(on-chain: approved)" context if desired.
            if decision.status != "trusted":
                _trust.record_approval(
                    service, decision.current_hash, decision.app_id
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


# ── CLI ──


@click.group()
@click.option(
    "-y",
    "--yes",
    "auto_yes",
    is_flag=True,
    help="Auto-answer 'yes' to the compose-hash approval prompt. "
    "TLS pinning and the on-chain revoke kill-switch still apply, so a "
    "tampered or revoked hash still hard-aborts. Use in CI / scripts.",
)
@click.option(
    "--dangerously-skip-attestations",
    "skip_attestations",
    is_flag=True,
    help="Disable ALL attestation verification — no TLS pin, no on-chain "
    "check, no compose-hash prompt. Only meaningful against a local "
    "dev server (no TEE). Never use against a Phala CVM.",
)
@click.option(
    "--profile",
    "profile",
    default="",
    envvar="HIVEMIND_PROFILE",
    metavar="NAME",
    help="Named identity to use. Each profile is an independent "
    "service+api_key pair stored at ~/.hivemind/profiles/<NAME>.yaml. "
    "Defaults to 'default'. Example: hivemind --profile alice query '...'",
)
def cli(auto_yes: bool, skip_attestations: bool, profile: str) -> None:
    """Hivemind — conditional recall for the privacy-quality frontier."""
    # Set the same env vars the trust layer already reads, so we don't
    # have to thread a context object into every subcommand. Flags win
    # over the absence of an env var; if the env var is already set,
    # leave it alone (most permissive of {flag, env} wins).
    if auto_yes:
        os.environ["HIVEMIND_TRUST_ALL"] = "1"
    if skip_attestations:
        os.environ["HIVEMIND_NO_TRUST_CHECK"] = "1"
    if profile:
        os.environ["HIVEMIND_PROFILE"] = profile


@cli.command()
@click.option(
    "--service",
    default=_DEFAULT_SERVICE,
    show_default=True,
    help="Hivemind service URL",
)
@click.option("--api-key", default="", help="API key for authentication")
def init(service: str, api_key: str):
    """Connect to a hivemind service and save config."""
    service = service.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    # If the operator has already run ``hivemind trust approve`` for this
    # URL, that flow saved the enclave's pinned cert. Wire it as the
    # default verify so the health check below doesn't blow up on the
    # self-signed -8100s. cert.
    _warm_pin_from_trust(service)

    # Probe /v1/health (tenant key). If the key turns out to be the
    # admin key (HIVEMIND_ADMIN_KEY), /v1/health 401s — try the
    # admin-only /v1/admin/tenants probe before giving up. This lets the
    # operator set up an admin profile the same way as a tenant profile.
    health: dict = {}
    role = "tenant"
    try:
        resp = _hget(f"{service}/v1/health", headers=headers, timeout=10)
        if resp.status_code == 401 and api_key:
            ar = _hget(
                f"{service}/v1/admin/tenants", headers=headers, timeout=10
            )
            if ar.status_code < 400:
                role = "admin"
                health = {
                    "table_count": "(admin)",
                    "version": "(admin)",
                }
            else:
                click.echo(
                    f"Error: 401 from {service} — key authorizes neither "
                    "a tenant nor admin role.",
                    err=True,
                )
                raise SystemExit(1)
        else:
            resp.raise_for_status()
            health = resp.json()
    except httpx.ConnectError:
        click.echo(f"Error: Cannot reach {service}", err=True)
        raise SystemExit(1)
    except httpx.HTTPStatusError as e:
        click.echo(
            f"Error: {e.response.status_code} from {service}", err=True
        )
        raise SystemExit(1)
    except httpx.TimeoutException:
        click.echo(
            f"Error: Connection timed out reaching {service}", err=True
        )
        raise SystemExit(1)

    _save_config({"service": service, "api_key": api_key, "role": role})
    profile = _profile_name()
    click.echo(
        f"Initialized profile '{profile}' (role={role}) at {_config_path()} "
        f"— connected to {service}"
    )
    click.echo(f"  Tables: {health.get('table_count', '?')}")
    click.echo(f"  Version: {health.get('version', '?')}")
    if profile == _DEFAULT_PROFILE:
        click.echo(
            "  Tip: pass --profile NAME to keep separate identities "
            "(admin / tenant_a / tenant_b) on the same laptop."
        )


@cli.command()
@click.argument("rules", required=False)
@click.option(
    "--from-file",
    "from_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read rules from a file (use instead of RULES argument)",
)
@click.option(
    "--private-prompt",
    "private_prompt",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Upload the file as the entire scope prompt (no public template). "
    "Uses agents/private-default-scope; prompt content stays TEE-resident.",
)
def scope(
    rules: str | None,
    from_file: Path | None,
    private_prompt: Path | None,
):
    """Upload a scope agent with English privacy rules.

    RULES is a natural-language description of your access policy.
    It gets fused into the default scope agent prompt and uploaded
    as a tarball to the service.

    Example:

      hivemind scope "Share patient outcomes for research.
        Allow aggregate statistics and survival curves.
        Never expose individual records or exact dates of birth.
        Suppress groups smaller than 10."

      hivemind scope --from-file policy.md
      hivemind scope --private-prompt my-secret-rules.md
    """
    config = _load_config()
    if sum(bool(x) for x in (rules, from_file, private_prompt)) != 1:
        click.echo(
            "Error: provide exactly one of RULES / --from-file / --private-prompt.",
            err=True,
        )
        raise SystemExit(1)

    if private_prompt is not None:
        agent_dir = _AGENTS_DIR / "private-default-scope"
        prompt_text = private_prompt.read_text()
        description = "Private scope (prompt content TEE-resident)"
    else:
        if from_file is not None:
            rules = from_file.read_text()
        if not rules or not rules.strip():
            click.echo(
                "Error: Provide rules as an argument or via --from-file.",
                err=True,
            )
            raise SystemExit(1)
        rules = rules.strip()
        template_path = _AGENTS_DIR / "default-scope" / "scope-prompt.md"
        if not template_path.exists():
            click.echo(
                f"Error: Scope template not found: {template_path}", err=True
            )
            raise SystemExit(1)
        prompt_text = template_path.read_text().replace(
            "{scenario_description}", rules
        )
        agent_dir = _AGENTS_DIR / "default-scope"
        description = f"Scope: {rules[:200]}"

    agent_py = (agent_dir / "agent.py").read_text()
    bridge_py = (agent_dir / "_bridge.py").read_text()

    tarball = _make_tarball(
        {
            "Dockerfile": _DOCKERFILE_MD_AGENT,
            "_bridge.py": bridge_py,
            "agent.py": agent_py,
            "prompt.md": prompt_text,
        }
    )

    click.echo("Uploading scope agent...")
    service = config["service"]
    headers = _headers(config)

    try:
        resp = _hpost(
            f"{service}/v1/agents/upload",
            files={
                "archive": ("scope-agent.tar.gz", tarball, "application/gzip")
            },
            data={
                "name": "private-scope-agent" if private_prompt else "scope-agent",
                "agent_type": "scope",
                "description": description,
            },
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.ConnectError:
        click.echo(f"Error: Cannot reach {service}", err=True)
        raise SystemExit(1)
    except httpx.HTTPStatusError as e:
        click.echo(
            f"Error: Upload failed ({e.response.status_code}): {_api_error(e.response)}",
            err=True,
        )
        raise SystemExit(1)
    except httpx.TimeoutException:
        click.echo("Error: Upload timed out. Try again.", err=True)
        raise SystemExit(1)

    result = resp.json()
    agent_id = result["agent_id"]
    run_id = result.get("run_id")

    if run_id:
        click.echo(f"Building image (run: {run_id})...")
        for _ in range(60):
            time.sleep(2)
            try:
                sr = _hget(
                    f"{service}/v1/agent-runs/{run_id}",
                    headers=headers,
                    timeout=10,
                )
                status = sr.json()
                s = status.get("status", "")
                if s == "completed":
                    click.echo("Build complete.")
                    break
                if s == "failed":
                    click.echo(
                        f"Error: Build failed: {status.get('error', '?')}",
                        err=True,
                    )
                    raise SystemExit(1)
            except httpx.RequestError:
                pass
        else:
            click.echo(
                "Warning: Build still in progress. Check status manually.",
                err=True,
            )

    config["scope_agent_id"] = agent_id
    _save_config(config)
    click.echo(f"Scope agent: {agent_id}")


def _split_sql_statements(sql: str) -> list[str]:
    """Naive SQL splitter: handles ';' terminators, single-quoted strings,
    $$-delimited bodies, and -- line comments. Good enough for pg_dump-style
    output."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_single = False
    in_dollar = False
    dollar_tag = ""
    while i < n:
        c = sql[i]
        if not in_single and not in_dollar and c == "-" and i + 1 < n and sql[i + 1] == "-":
            nl = sql.find("\n", i)
            if nl < 0:
                break
            i = nl + 1
            continue
        if not in_dollar and c == "'":
            in_single = not in_single
            buf.append(c)
            i += 1
            continue
        if not in_single and c == "$":
            end = sql.find("$", i + 1)
            if end > i:
                tag = sql[i : end + 1]
                if in_dollar and tag == dollar_tag:
                    buf.append(tag)
                    i = end + 1
                    in_dollar = False
                    dollar_tag = ""
                    continue
                if not in_dollar:
                    buf.append(tag)
                    i = end + 1
                    in_dollar = True
                    dollar_tag = tag
                    continue
        if c == ";" and not in_single and not in_dollar:
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


@cli.command("load")
@click.argument(
    "file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--table",
    "table",
    default=None,
    help="Target table (required for CSV/JSONL; ignored for SQL).",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["auto", "sql", "csv", "jsonl"]),
    default="auto",
    show_default=True,
    help="File format. 'auto' infers from the extension.",
)
@click.option(
    "--batch",
    "batch",
    type=int,
    default=500,
    show_default=True,
    help="Rows per INSERT batch (CSV/JSONL).",
)
@click.option(
    "--delimiter",
    "delim",
    default=",",
    show_default=True,
    help="CSV delimiter.",
)
def load_cmd(
    file: Path,
    table: str | None,
    fmt: str,
    batch: int,
    delim: str,
):
    """Load a dataset into the service's Postgres via POST /v1/store.

    Formats:

      SQL    — splits statements and executes each one.
               `hivemind load dump.sql`

      CSV    — parameterized INSERTs into --table. First row is header.
               `hivemind load users.csv --table users`

      JSONL  — one JSON object per line → parameterized INSERTs.
               `hivemind load events.jsonl --table events`

    All rows are batched and sent through the normal auth-checked API, so
    this works against local Postgres and remote SQL-proxy-backed deploys
    alike.
    """
    import csv

    config = _load_config()
    service = config["service"]
    headers = {**_headers(config), "Content-Type": "application/json"}
    store_url = f"{service}/v1/store"

    if fmt == "auto":
        ext = file.suffix.lower()
        fmt = {".sql": "sql", ".csv": "csv", ".jsonl": "jsonl", ".ndjson": "jsonl"}.get(ext, "")
        if not fmt:
            raise click.ClickException(
                f"Cannot infer format from extension: {file.suffix}. "
                "Pass --format sql|csv|jsonl."
            )

    if fmt in ("csv", "jsonl") and not table:
        raise click.ClickException("--table is required for CSV/JSONL.")

    def _post(sql: str, params: list):
        try:
            r = _hpost(
                store_url,
                headers=headers,
                json={"sql": sql, "params": params},
                timeout=120,
            )
        except httpx.ConnectError:
            raise click.ClickException(f"Cannot reach {service}")
        if r.status_code >= 400:
            raise click.ClickException(f"{r.status_code}: {_api_error(r)}")

    if fmt == "sql":
        text = file.read_text()
        stmts = _split_sql_statements(text)
        click.echo(f"Loading {len(stmts)} SQL statements from {file} → {service}")
        with click.progressbar(stmts, label="exec") as bar:
            for stmt in bar:
                _post(stmt, [])
        click.echo(f"Done: {len(stmts)} statements.")
        return

    if fmt == "csv":
        with file.open(newline="") as f:
            reader = csv.reader(f, delimiter=delim)
            try:
                cols = next(reader)
            except StopIteration:
                raise click.ClickException(f"{file}: empty file")
            rows = list(reader)
        click.echo(
            f"Loading {len(rows)} rows from {file} into {table} "
            f"(columns: {', '.join(cols)}) → {service}"
        )
        _batch_insert(_post, table, cols, rows, batch)
        click.echo(f"Done: {len(rows)} rows.")
        return

    if fmt == "jsonl":
        rows_dicts: list[dict] = []
        with file.open() as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except _json.JSONDecodeError as e:
                    raise click.ClickException(f"{file}:{lineno}: {e}")
                if not isinstance(obj, dict):
                    raise click.ClickException(
                        f"{file}:{lineno}: expected object, got {type(obj).__name__}"
                    )
                rows_dicts.append(obj)
        if not rows_dicts:
            click.echo(f"{file}: no rows to load.")
            return
        cols = sorted({k for r in rows_dicts for k in r.keys()})
        rows = [[r.get(c) for c in cols] for r in rows_dicts]
        click.echo(
            f"Loading {len(rows)} rows from {file} into {table} "
            f"(columns: {', '.join(cols)}) → {service}"
        )
        _batch_insert(_post, table, cols, rows, batch)
        click.echo(f"Done: {len(rows)} rows.")
        return


def _batch_insert(
    post_fn,
    table: str,
    cols: list[str],
    rows: list[list],
    batch_size: int,
) -> None:
    """Multi-row INSERT: `INSERT INTO t (c1,c2) VALUES (%s,%s),(%s,%s),...`."""
    if not rows:
        return
    col_list = ", ".join(f'"{c}"' for c in cols)
    row_tpl = "(" + ", ".join(["%s"] * len(cols)) + ")"
    total = len(rows)
    with click.progressbar(length=total, label="insert") as bar:
        for start in range(0, total, batch_size):
            chunk = rows[start : start + batch_size]
            placeholders = ", ".join([row_tpl] * len(chunk))
            sql = f'INSERT INTO "{table}" ({col_list}) VALUES {placeholders}'
            params: list = []
            for r in chunk:
                params.extend(r)
            post_fn(sql, params)
            bar.update(len(chunk))


@cli.command()
def share():
    """Show the query endpoint and connection details."""
    config = _load_config()
    scope_id = config.get("scope_agent_id")

    if not scope_id:
        click.echo(
            "Error: No scope agent registered. Run 'hivemind scope' first.",
            err=True,
        )
        raise SystemExit(1)

    service = config["service"]
    click.echo(f"Endpoint:    {service}/v1/query")
    click.echo(f"Scope agent: {scope_id}")
    if config.get("api_key"):
        click.echo(f"API key:     {config['api_key']}")
    click.echo()
    click.echo("Example:")
    click.echo('  hivemind query "What tables are available?"')


@cli.command("query")
@click.argument("question")
@click.option("--endpoint", default=None, help="Override service URL")
@click.option(
    "--async", "use_async", is_flag=True, help="Use async submit+poll"
)
@click.option(
    "--agent",
    "query_agent",
    default=None,
    help="Query agent ID (persists to profile as the default).",
)
def query_cmd(
    question: str,
    endpoint: str | None,
    use_async: bool,
    query_agent: str | None,
):
    """Send a natural-language query to the hivemind service."""
    config = _load_config()
    service = endpoint or config["service"]
    headers = _headers(config)

    if query_agent:
        config["query_agent_id"] = query_agent
        _save_config(config)

    payload: dict = {"query": question}
    if config.get("query_agent_id"):
        payload["query_agent_id"] = config["query_agent_id"]
    if config.get("scope_agent_id"):
        payload["scope_agent_id"] = config["scope_agent_id"]

    if use_async:
        _query_async(service, headers, payload)
    else:
        _query_sync(service, headers, payload)


def _query_sync(service: str, headers: dict, payload: dict) -> None:
    try:
        resp = _hpost(
            f"{service}/v1/query",
            json=payload,
            headers=headers,
            timeout=120,
        )
        resp.raise_for_status()
    except httpx.ConnectError:
        click.echo(f"Error: Cannot reach {service}", err=True)
        raise SystemExit(1)
    except httpx.HTTPStatusError as e:
        click.echo(
            f"Error: {e.response.status_code}: {_api_error(e.response)}",
            err=True,
        )
        raise SystemExit(1)
    except httpx.TimeoutException:
        click.echo(
            "Error: Query timed out. Try --async for long-running queries.",
            err=True,
        )
        raise SystemExit(1)

    result = resp.json()
    click.echo(result.get("output", "No output"))
    if result.get("mediated"):
        click.echo("\n(mediated)", err=True)


def _query_async(service: str, headers: dict, payload: dict) -> None:
    try:
        resp = _hpost(
            f"{service}/v1/query/submit",
            json=payload,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    run_id = resp.json().get("run_id")
    click.echo(f"Submitted (run: {run_id}). Polling...")

    for _ in range(120):
        time.sleep(2)
        try:
            sr = _hget(
                f"{service}/v1/query/runs/{run_id}",
                headers=headers,
                timeout=10,
            )
            data = sr.json()
            status = data.get("status", "")
            if status == "completed":
                result = data.get("result", {})
                click.echo(result.get("output", "No output"))
                if result.get("mediated"):
                    click.echo("\n(mediated)", err=True)
                return
            if status == "failed":
                click.echo(f"Error: {data.get('error', '?')}", err=True)
                raise SystemExit(1)
        except httpx.RequestError:
            pass

    click.echo("Error: Query did not complete within timeout.", err=True)
    raise SystemExit(1)


# ── Approach A: run / runs / agents ──


def _artifact_url(service: str, run_id: str, filename: str) -> str:
    return f"{service}/v1/query/runs/{run_id}/artifacts/{filename}"


@cli.command("run")
@click.argument(
    "path",
    type=click.Path(exists=True, file_okay=True, dir_okay=True, path_type=Path),
)
@click.option("--prompt", default="", help="Prompt to pass to the agent")
@click.option(
    "--name",
    default=None,
    help="Agent name (defaults to directory or file basename)",
)
@click.option("--scope-agent", "scope_agent", default=None, help="Scope agent ID")
@click.option(
    "--mediator-agent", "mediator_agent", default=None, help="Mediator agent ID"
)
@click.option(
    "--timeout",
    type=int,
    default=600,
    show_default=True,
    help="Poll timeout in seconds",
)
@click.option(
    "--memory-mb",
    type=int,
    default=256,
    show_default=True,
    help="Container memory limit",
)
@click.option(
    "--max-llm-calls",
    type=int,
    default=20,
    show_default=True,
    help="Maximum LLM calls the agent may make",
)
@click.option(
    "--max-tokens",
    type=int,
    default=100_000,
    show_default=True,
    help="Total token budget",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
@click.option(
    "--fetch",
    is_flag=True,
    help="Download artifacts into ./hivemind-artifacts/<run_id>/",
)
def run_cmd(
    path: Path,
    prompt: str,
    name: str | None,
    scope_agent: str | None,
    mediator_agent: str | None,
    timeout: int,
    memory_mb: int,
    max_llm_calls: int,
    max_tokens: int,
    as_json: bool,
    fetch: bool,
):
    """Upload, run, and collect a query agent in one command.

    PATH can be either a directory containing a Dockerfile + agent source, or
    an existing .tar.gz archive. The CLI packages, uploads, polls the run to
    completion, and prints the agent's output and artifact URLs.

    Example:

      hivemind run ./my-agent --prompt "How many documents?"
      hivemind run agent.tar.gz --json --fetch
    """
    config = _load_config()
    service = config["service"]
    headers = _headers(config)

    if path.is_dir():
        archive_bytes = _tarball_from_dir(path)
        archive_name = f"{path.name}.tar.gz"
        agent_name = name or path.name
    elif path.suffix in {".gz", ".tgz"} or path.name.endswith(".tar.gz"):
        archive_bytes = path.read_bytes()
        archive_name = path.name
        agent_name = name or path.stem.replace(".tar", "")
    else:
        raise click.ClickException(
            f"Unsupported path type: {path}. Pass a directory or .tar.gz."
        )

    scope_id = scope_agent or config.get("scope_agent_id")
    mediator_id = mediator_agent or config.get("mediator_agent_id")

    form_data: dict[str, str] = {
        "name": agent_name,
        "prompt": prompt,
        "description": f"hivemind run {path.name}",
        "memory_mb": str(memory_mb),
        "max_llm_calls": str(max_llm_calls),
        "max_tokens": str(max_tokens),
        "timeout_seconds": str(min(timeout, 3600)),
    }
    if scope_id:
        form_data["scope_agent_id"] = scope_id
    if mediator_id:
        form_data["mediator_agent_id"] = mediator_id

    if not as_json:
        click.echo(f"Packing {path} → {archive_name} ({len(archive_bytes)} bytes)")
        click.echo(f"Uploading to {service}...")

    try:
        resp = _hpost(
            f"{service}/v1/query-agents/submit",
            files={
                "archive": (archive_name, archive_bytes, "application/gzip"),
            },
            data=form_data,
            headers=headers,
            timeout=60,
        )
    except httpx.ConnectError:
        click.echo(f"Error: Cannot reach {service}", err=True)
        raise SystemExit(2)
    except httpx.TimeoutException:
        click.echo("Error: Upload timed out.", err=True)
        raise SystemExit(2)

    if resp.status_code >= 400:
        click.echo(
            f"Error: Submit failed ({resp.status_code}): {_api_error(resp)}",
            err=True,
        )
        raise SystemExit(3)

    submission = resp.json()
    run_id = submission["run_id"]
    agent_id = submission.get("agent_id")

    if not as_json:
        click.echo(f"Submitted: run_id={run_id} agent_id={agent_id}")
        click.echo(f"Polling /v1/agent-runs/{run_id}...")

    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        try:
            sr = _hget(
                f"{service}/v1/agent-runs/{run_id}",
                headers=headers,
                timeout=15,
            )
            if sr.status_code == 404:
                time.sleep(2)
                continue
            data = sr.json()
        except httpx.RequestError:
            time.sleep(2)
            continue

        status = data.get("status", "")
        if status != last_status and not as_json:
            click.echo(f"  status: {status}")
            last_status = status

        if status == "completed":
            _emit_run_result(service, data, run_id, as_json=as_json, fetch=fetch)
            return
        if status == "failed":
            err = data.get("error") or data.get("result", {}).get("error") or "?"
            if as_json:
                click.echo(_json.dumps({"status": "failed", "error": err, "run_id": run_id}))
            else:
                click.echo(f"Error: run failed: {err}", err=True)
            raise SystemExit(4)

        time.sleep(3)

    if as_json:
        click.echo(_json.dumps({"status": "timeout", "run_id": run_id}))
    else:
        click.echo(
            f"Error: timed out after {timeout}s. Check `hivemind runs {run_id}`.",
            err=True,
        )
    raise SystemExit(5)


def _emit_run_result(
    service: str,
    data: dict,
    run_id: str,
    *,
    as_json: bool,
    fetch: bool,
) -> None:
    # Server returns these as top-level columns from the runs table
    # (see hivemind/sandbox/run_store.py). The legacy ``result.output``
    # nesting never existed in this code path; reading it always
    # returned None and printed "(empty)" even when the agent succeeded.
    output = data.get("output") or ""
    index_output = data.get("index_output") or ""
    mediated = data.get("mediated")  # reserved for future mediator runs
    artifacts = data.get("artifacts", []) or []

    artifact_urls = [
        {
            "filename": a["filename"],
            "size": a.get("size"),
            "content_type": a.get("content_type"),
            "url": _artifact_url(service, run_id, a["filename"]),
        }
        for a in artifacts
    ]

    fetched: list[dict] = []
    if fetch and artifacts:
        out_dir = Path("hivemind-artifacts") / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        config = _load_config()
        headers = _headers(config)
        for a in artifacts:
            fname = a["filename"]
            try:
                r = _hget(
                    _artifact_url(service, run_id, fname),
                    headers=headers,
                    timeout=60,
                )
                r.raise_for_status()
                dest = out_dir / fname
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(r.content)
                fetched.append({"filename": fname, "path": str(dest)})
            except httpx.HTTPError as e:
                if not as_json:
                    click.echo(
                        f"  warn: failed to fetch {fname}: {e}", err=True
                    )

    if as_json:
        click.echo(
            _json.dumps(
                {
                    "status": "completed",
                    "run_id": run_id,
                    "output": output,
                    "mediated": mediated,
                    "artifacts": artifact_urls,
                    "fetched": fetched,
                },
                indent=2,
            )
        )
        return

    click.echo("")
    click.echo("── Output ──")
    click.echo(output or "(empty)")
    if index_output:
        click.echo("")
        click.echo("── Index agent output ──")
        click.echo(index_output)
    if mediated:
        click.echo(f"\n(mediated: {mediated})", err=True)
    if artifact_urls:
        click.echo("\n── Artifacts ──")
        for a in artifact_urls:
            size = f" {a['size']}B" if a.get("size") else ""
            click.echo(f"  {a['filename']}{size}  →  {a['url']}")
    if fetched:
        click.echo("\nFetched to ./hivemind-artifacts/{}/".format(run_id))
        for f in fetched:
            click.echo(f"  {f['path']}")


@cli.command("runs")
@click.argument("run_id", required=False)
@click.option(
    "--limit",
    type=int,
    default=20,
    show_default=True,
    help="Max rows when listing",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def runs_cmd(run_id: str | None, limit: int, as_json: bool):
    """List recent agent runs, or show details for one run."""
    config = _load_config()
    service = config["service"]
    headers = _headers(config)

    if run_id:
        data = _http_get(
            f"{service}/v1/agent-runs/{run_id}", headers=headers, timeout=15
        )
        if as_json:
            click.echo(_json.dumps(data, indent=2, default=str))
            return

        click.echo(f"Run:      {data.get('run_id')}")
        click.echo(f"Agent:    {data.get('agent_id')}")
        click.echo(f"Status:   {data.get('status')}")
        click.echo(f"Created:  {data.get('created_at')}")
        click.echo(f"Updated:  {data.get('updated_at')}")

        # Stages are flattened into <stage>_started_at / <stage>_ended_at
        # columns by run_store, not nested under a ``stages`` key.
        stage_names = ("build", "scope", "query", "mediator", "index")
        stage_lines = []
        for name in stage_names:
            started = data.get(f"{name}_started_at")
            ended = data.get(f"{name}_ended_at")
            if started is None and ended is None:
                continue
            if started and ended:
                dur = f"{ended - started:.1f}s"
            elif started:
                dur = "(running)"
            else:
                dur = "(unknown)"
            stage_lines.append(f"  {name}: {dur}")
        if stage_lines:
            click.echo("Stages:")
            for line in stage_lines:
                click.echo(line)

        # ``output`` and ``index_output`` are top-level columns on the
        # run row — reading ``data["result"]["output"]`` (the prior
        # shape) always returned None and silently dropped the agent's
        # answer.
        output = data.get("output")
        if output:
            click.echo("")
            click.echo("── Output ──")
            click.echo(output)
        index_output = data.get("index_output")
        if index_output:
            click.echo("")
            click.echo("── Index agent output ──")
            click.echo(index_output)

        artifacts = data.get("artifacts") or []
        if artifacts:
            click.echo("")
            click.echo("── Artifacts ──")
            for a in artifacts:
                size = f" {a.get('size')}B" if a.get("size") else ""
                click.echo(
                    f"  {a['filename']}{size}  →  {_artifact_url(service, run_id, a['filename'])}"
                )

        err = data.get("error")
        if err:
            click.echo(f"\nError: {err}", err=True)
        return

    # List recent
    data = _http_get(
        f"{service}/v1/agent-runs?limit={limit}", headers=headers, timeout=15
    )
    if as_json:
        click.echo(_json.dumps(data, indent=2, default=str))
        return

    if not data:
        click.echo("(no runs)")
        return

    click.echo(f"{'RUN_ID':<14} {'AGENT_ID':<14} {'STATUS':<10} CREATED")
    for row in data:
        click.echo(
            f"{row.get('run_id','?'):<14} "
            f"{row.get('agent_id','?'):<14} "
            f"{str(row.get('status','?')):<10} "
            f"{row.get('created_at','')}"
        )


@cli.command("agents")
@click.option(
    "--type",
    "agent_type",
    default=None,
    help="Filter by agent type (query/scope/mediator/index)",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def agents_cmd(agent_type: str | None, as_json: bool):
    """List agents registered with the service."""
    config = _load_config()
    service = config["service"]
    headers = _headers(config)

    url = f"{service}/v1/agents"
    if agent_type:
        url += f"?type={agent_type}"
    data = _http_get(url, headers=headers, timeout=15)

    if as_json:
        click.echo(_json.dumps(data, indent=2, default=str))
        return

    if not data:
        click.echo("(no agents)")
        return

    click.echo(
        f"{'AGENT_ID':<14} {'TYPE':<10} {'NAME':<24} DESCRIPTION"
    )
    for a in data:
        desc = (a.get("description") or "").replace("\n", " ")
        if len(desc) > 60:
            desc = desc[:57] + "..."
        click.echo(
            f"{a.get('agent_id','?'):<14} "
            f"{str(a.get('agent_type','?')):<10} "
            f"{str(a.get('name',''))[:24]:<24} "
            f"{desc}"
        )


@cli.command("agent-rm")
@click.argument("agent_id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def agent_rm_cmd(agent_id: str, as_json: bool):
    """Delete a single agent by ID.

    Use ``hivemind agents`` to list. Bulk cleanup of agents whose
    Docker images are missing is ``hivemind admin sweep-broken-agents``.
    """
    config = _load_config()
    service = config["service"]
    headers = _headers(config)
    try:
        r = _hdelete(
            f"{service}/v1/agents/{agent_id}",
            headers=headers,
            timeout=30,
        )
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if r.status_code == 404:
        click.echo(f"Error: agent {agent_id} not found", err=True)
        raise SystemExit(3)
    if r.status_code >= 400:
        click.echo(f"Error {r.status_code}: {_api_error(r)}", err=True)
        raise SystemExit(3)
    if as_json:
        click.echo(_json.dumps(r.json(), indent=2))
    else:
        click.echo(f"deleted {agent_id}")


@cli.command("schema")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def schema_cmd(as_json: bool):
    """Print the tenant's table schema (column names + types per table)."""
    config = _load_config()
    service = config["service"]
    data = _http_get(
        f"{service}/v1/admin/schema", headers=_headers(config), timeout=30
    )
    schema = data.get("schema") or []
    if as_json:
        click.echo(_json.dumps(schema, indent=2, default=str))
        return
    if not schema:
        click.echo("(no tables)")
        return
    by_table: dict[str, list] = {}
    for row in schema:
        by_table.setdefault(row.get("table_name", "?"), []).append(row)
    for table, cols in sorted(by_table.items()):
        click.echo(f"{table}")
        for col in cols:
            name = col.get("column_name") or col.get("name") or "?"
            typ = col.get("data_type") or col.get("type") or "?"
            nullable = "" if col.get("is_nullable") == "YES" else " NOT NULL"
            click.echo(f"  {name:<28} {typ}{nullable}")


@cli.command("attestation")
@click.option(
    "--service",
    default=None,
    help="Service URL (defaults to active profile, or HIVEMIND_SERVICE).",
)
@click.option(
    "--raw", is_flag=True, help="Emit the full /v1/attestation JSON payload."
)
def attestation_cmd(service: str | None, raw: bool):
    """Fetch and print the CVM's attestation bundle (TDX quote + binding).

    No auth — this endpoint is public so out-of-band verifiers can pin
    the cert + replay the quote against Intel/dstack KMS.
    """
    if service:
        url = service.rstrip("/")
    else:
        try:
            url = _load_config(check_trust=False)["service"]
        except SystemExit:
            url = _DEFAULT_SERVICE
    _warm_pin_from_trust(url)
    try:
        r = _hget(f"{url}/v1/attestation", timeout=15)
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if r.status_code >= 400:
        click.echo(f"Error {r.status_code}: {_api_error(r)}", err=True)
        raise SystemExit(3)
    bundle = r.json()
    if raw:
        click.echo(_json.dumps(bundle, indent=2, default=str))
        return
    if not bundle.get("ready"):
        click.echo(f"ready:    false")
        click.echo(f"reason:   {bundle.get('reason', '?')}")
        click.echo(f"version:  {bundle.get('hivemind_version', '?')}")
        return
    att = bundle.get("attestation") or {}
    click.echo(f"ready:        true")
    click.echo(f"booted_at:    {bundle.get('booted_at', '?')}")
    click.echo(f"app_id:       {att.get('app_id', '?')}")
    click.echo(f"compose_hash: {att.get('compose_hash', '?')}")
    click.echo(f"version:      {att.get('hivemind_version', '?')}")
    if app_auth := att.get("app_auth"):
        click.echo(f"app_auth:")
        click.echo(f"  contract:   {app_auth.get('contract', '?')}")
        click.echo(f"  chain_id:   {app_auth.get('chain_id', '?')}")
    if tls := att.get("tls"):
        if tls.get("enabled"):
            click.echo(f"tls:")
            click.echo(
                f"  fingerprint: {tls.get('cert_fingerprint_sha256_hex', '?')}"
            )
            if pin := tls.get("pinning_url"):
                click.echo(f"  pin URL:     {pin}")
    click.echo("(use --raw for full JSON including the TDX quote)")


@cli.command("index")
@click.argument("data", required=False)
@click.option(
    "--from-file",
    "from_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read DATA from a file.",
)
@click.option(
    "--metadata",
    "metadata_json",
    default=None,
    help='JSON object passed through to the index agent (e.g. \'{"source":"x"}\').',
)
@click.option(
    "--agent",
    "index_agent",
    default=None,
    help="Index agent ID (overrides the profile default).",
)
@click.option(
    "--max-tokens",
    type=int,
    default=None,
    help="Per-call token budget for the index agent.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def index_cmd(
    data: str | None,
    from_file: Path | None,
    metadata_json: str | None,
    index_agent: str | None,
    max_tokens: int | None,
    as_json: bool,
):
    """Run the index agent against DATA and store the resulting summary.

    The index agent receives the input plus tenant tools, writes
    summaries / extractions back into Postgres, and returns the
    canonical index_text.
    """
    if (data is None) == (from_file is None):
        click.echo(
            "Error: provide exactly one of DATA argument or --from-file.",
            err=True,
        )
        raise SystemExit(1)
    if from_file is not None:
        data = from_file.read_text()
    metadata: dict = {}
    if metadata_json:
        try:
            metadata = _json.loads(metadata_json)
            if not isinstance(metadata, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, _json.JSONDecodeError) as e:
            click.echo(f"Error: --metadata: {e}", err=True)
            raise SystemExit(1)

    config = _load_config()
    service = config["service"]
    headers = {**_headers(config), "Content-Type": "application/json"}
    payload: dict = {"data": data, "metadata": metadata}
    if index_agent:
        payload["index_agent_id"] = index_agent
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    try:
        r = _hpost(
            f"{service}/v1/index", headers=headers, json=payload, timeout=300
        )
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if r.status_code >= 400:
        click.echo(f"Error {r.status_code}: {_api_error(r)}", err=True)
        raise SystemExit(3)
    body = r.json()
    if as_json:
        click.echo(_json.dumps(body, indent=2, default=str))
        return
    click.echo(body.get("index_text", ""))


@cli.command("rotate-key")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
@click.confirmation_option(
    prompt=(
        "Rotate this tenant's API key? The current key will stop working "
        "immediately. Continue?"
    )
)
def rotate_key(as_json: bool):
    """Rotate this tenant's API key and update local config.

    Designed as the mandatory first action for a new tenant: the admin
    who created the tenant briefly saw the plaintext key. Rotating
    immediately cuts them out of the trust loop — from here on, only
    the TEE holds anything that maps to this tenant's data.
    """
    config = _load_config()
    service = config["service"]
    headers = _headers(config)
    if "Authorization" not in headers:
        click.echo("Error: no api_key in config. Run 'hivemind init'.", err=True)
        raise SystemExit(1)

    try:
        resp = _hpost(
            f"{service}/v1/tenant/rotate-key", headers=headers, timeout=30,
        )
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(3)

    data = resp.json()
    new_key = data["api_key"]
    config["api_key"] = new_key
    _save_config(config)

    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    click.echo(f"Tenant: {data['tenant_id']}")
    click.echo(
        f"New API key (saved to profile '{_profile_name()}' "
        f"at {_config_path()}):"
    )
    click.echo(f"  {new_key}")
    click.echo("")
    click.echo(
        "Previous key is now revoked. Anyone who held the old key "
        "(including the admin who minted it) can no longer reach your data."
    )


# ── Profiles ──
#
# Each profile is a separate identity: service URL + tenant API key,
# stored at ``~/.hivemind/profiles/<name>.yaml``. Trust pins
# (``trust.json``, ``enclave-tls-*.pem``) live alongside, so all
# profiles share the same set of approved enclaves.


@cli.group("profile")
def profile_cli():
    """Manage named identities (admin / tenant_a / tenant_b / …)."""
    pass


@profile_cli.command("list")
def profile_list():
    """List all profiles in ~/.hivemind/profiles/."""
    active = _profile_name()
    if not _PROFILES_DIR.exists():
        click.echo(f"No profiles yet. Run 'hivemind init …' to create '{active}'.")
        return
    rows: list[tuple[str, str, str, str]] = []
    for p in sorted(_PROFILES_DIR.glob("*.yaml")):
        name = p.stem
        try:
            cfg = yaml.safe_load(p.read_text()) or {}
        except yaml.YAMLError:
            cfg = {}
        service = str(cfg.get("service") or "?")
        has_key = "yes" if cfg.get("api_key") else "no"
        role = str(cfg.get("role") or "tenant")
        rows.append((name, service, has_key, role))
    if not rows:
        click.echo(f"No profiles in {_PROFILES_DIR}.")
        return
    click.echo(
        f"{'ACTIVE':<7} {'NAME':<24} {'ROLE':<8} {'API_KEY':<8} SERVICE"
    )
    for name, service, has_key, role in rows:
        marker = "*" if name == active else " "
        click.echo(
            f"  {marker:<5} {name:<24} {role:<8} {has_key:<8} {service}"
        )


@profile_cli.command("show")
@click.argument("name", required=False)
def profile_show(name: str | None):
    """Print the YAML for the active profile (or NAME)."""
    p = _config_path(name) if name else _config_path()
    if not p.exists():
        click.echo(f"Error: profile not found at {p}", err=True)
        raise SystemExit(1)
    click.echo(f"# {p}")
    click.echo(p.read_text(), nl=False)


@profile_cli.command("delete")
@click.argument("name")
@click.confirmation_option(
    prompt="Delete this profile's local config? "
    "(does NOT revoke the API key on the server)"
)
def profile_delete(name: str):
    """Delete a profile's local config file."""
    p = _config_path(name)
    if not p.exists():
        click.echo(f"Error: profile not found at {p}", err=True)
        raise SystemExit(1)
    p.unlink()
    pointer_was_set = (
        _ACTIVE_POINTER.exists()
        and _ACTIVE_POINTER.read_text().strip() == name
    )
    _clear_active_profile_if(name)
    click.echo(f"Deleted {p}")
    if pointer_was_set:
        click.echo(
            f"Active-profile pointer cleared (it was pointing at '{name}'). "
            "Run 'hivemind profile use NAME' to set a new one."
        )
    click.echo(
        "Note: the API key on the server is still valid until you "
        "rotate it via 'hivemind rotate-key' or delete the tenant."
    )


@profile_cli.command("use")
@click.argument("name")
def profile_use(name: str):
    """Make NAME the persistent active profile.

    Plain ``hivemind <cmd>`` (no --profile flag, no HIVEMIND_PROFILE env)
    will use this profile from now on. Per-command overrides via
    ``--profile`` and ``HIVEMIND_PROFILE`` still win.
    """
    p = _config_path(name)
    if not p.exists():
        click.echo(
            f"Error: profile '{name}' not found at {p}. "
            f"Run 'hivemind --profile {name} init …' first.",
            err=True,
        )
        raise SystemExit(1)
    _set_active_profile(name)
    click.echo(f"Active profile is now '{name}' (pointer: {_ACTIVE_POINTER}).")


@profile_cli.command("path")
@click.argument("name", required=False)
def profile_path(name: str | None):
    """Print the filesystem path of the active profile (or NAME)."""
    click.echo(str(_config_path(name) if name else _config_path()))


# ── Admin: tenant management ──


def _admin_headers(admin_key: str) -> dict:
    return {
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }


def _resolve_admin_key(admin_key: str) -> str:
    """Resolve the admin bearer.

    Order of preference:
    1. ``--admin-key`` flag (Click also fills this from
       ``HIVEMIND_ADMIN_KEY`` env via the option's ``envvar=``).
    2. The active profile's ``api_key`` IF it was registered with
       ``role: admin`` (set by ``hivemind init`` when /v1/health 401s
       and /v1/admin/tenants accepts the key).
    Otherwise, abort. We never silently use a tenant key for admin
    operations.
    """
    if admin_key:
        return admin_key
    try:
        cfg = _load_config(check_trust=False)
    except SystemExit:
        cfg = {}
    if cfg.get("role") == "admin" and cfg.get("api_key"):
        return cfg["api_key"]
    click.echo(
        "Error: admin key required. Pass --admin-key, set "
        "HIVEMIND_ADMIN_KEY, or 'hivemind --profile <admin> init "
        "--api-key <admin-key>' to wire up an admin profile.",
        err=True,
    )
    raise SystemExit(2)


def _resolve_admin_service(service: str | None) -> str:
    if service:
        url = service.rstrip("/")
    else:
        # Fall back to the regular init config (admins often manage locally).
        try:
            url = _load_config()["service"]
        except SystemExit:
            url = _DEFAULT_SERVICE
    # Admin commands don't go through ``_require_trust``, so pin the
    # enclave cert from the trust store here. Without this, every
    # ``hivemind admin *`` against an -8100s. URL fails the self-signed
    # handshake and exits before reaching the server.
    _warm_pin_from_trust(url)
    return url


@cli.group("admin")
def admin_cli():
    """Multi-tenant admin: provision, list, delete tenants.

    Requires the server's HIVEMIND_ADMIN_KEY. Pass it with --admin-key
    or set HIVEMIND_ADMIN_KEY in your shell environment.
    """
    pass


@admin_cli.command("create-tenant")
@click.argument("name")
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    default="",
    help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
    "the active profile's api_key when role=admin.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
def admin_create_tenant(name: str, service: str | None, admin_key: str, as_json: bool):
    """Provision a new tenant. Prints the one-time API key."""
    admin_key = _resolve_admin_key(admin_key)
    url = _resolve_admin_service(service)
    try:
        resp = _hpost(
            f"{url}/v1/admin/tenants",
            headers=_admin_headers(admin_key),
            json={"name": name},
            timeout=60,
        )
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(3)
    data = resp.json()
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    click.echo(f"Tenant:   {data['tenant_id']}  ({data['name']})")
    click.echo(f"Database: {data['db_name']}")
    click.echo("")
    click.echo("API key (store it now — we won't show it again):")
    click.echo(f"  {data['api_key']}")


@admin_cli.command("register-existing")
@click.argument("name")
@click.argument("db_name")
@click.option(
    "--api-key",
    default=None,
    help="Reuse this key (defaults to a newly minted one)",
)
@click.option(
    "--tenant-id",
    default=None,
    help="Pin the control-plane tenant_id (use when db_name=tenant_<tenant_id>)",
)
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    default="",
    help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
    "the active profile's api_key when role=admin.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
def admin_register_existing(
    name: str,
    db_name: str,
    api_key: str | None,
    tenant_id: str | None,
    service: str | None,
    admin_key: str,
    as_json: bool,
):
    """Adopt a pre-populated Postgres database as a tenant."""
    admin_key = _resolve_admin_key(admin_key)
    url = _resolve_admin_service(service)
    body: dict[str, str] = {"name": name, "db_name": db_name}
    if api_key:
        body["api_key"] = api_key
    if tenant_id:
        body["tenant_id"] = tenant_id
    try:
        resp = _hpost(
            f"{url}/v1/admin/tenants/register",
            headers=_admin_headers(admin_key),
            json=body,
            timeout=30,
        )
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(3)
    data = resp.json()
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    click.echo(f"Tenant:   {data['tenant_id']}  ({data['name']})")
    click.echo(f"Database: {data['db_name']}")
    click.echo("API key:")
    click.echo(f"  {data['api_key']}")


@admin_cli.command("list-tenants")
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    default="",
    help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
    "the active profile's api_key when role=admin.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
def admin_list_tenants(service: str | None, admin_key: str, as_json: bool):
    """List all tenants."""
    admin_key = _resolve_admin_key(admin_key)
    url = _resolve_admin_service(service)
    try:
        resp = _hget(
            f"{url}/v1/admin/tenants",
            headers={"Authorization": f"Bearer {admin_key}"},
            timeout=15,
        )
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(3)
    tenants = resp.json().get("tenants", [])
    if as_json:
        click.echo(_json.dumps(tenants, indent=2, default=str))
        return
    if not tenants:
        click.echo("(no tenants)")
        return
    click.echo(f"{'TENANT_ID':<16} {'NAME':<24} {'DB':<28} SUSPENDED")
    for t in tenants:
        click.echo(
            f"{t['id']:<16} "
            f"{str(t.get('name', ''))[:24]:<24} "
            f"{str(t.get('db_name', ''))[:28]:<28} "
            f"{t.get('suspended', False)}"
        )


@admin_cli.command("delete-tenant")
@click.argument("tenant_id")
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    default="",
    help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
    "the active profile's api_key when role=admin.",
)
@click.confirmation_option(
    prompt="Drop the tenant DB and all its data? This cannot be undone."
)
def admin_delete_tenant(tenant_id: str, service: str | None, admin_key: str):
    """Delete a tenant: drops its Postgres DB and revokes its key."""
    admin_key = _resolve_admin_key(admin_key)
    url = _resolve_admin_service(service)
    try:
        resp = _hdelete(
            f"{url}/v1/admin/tenants/{tenant_id}",
            headers={"Authorization": f"Bearer {admin_key}"},
            timeout=60,
        )
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(3)
    click.echo(f"Deleted tenant {tenant_id}.")


@admin_cli.command("rename-database")
@click.argument("old_name")
@click.argument("new_name")
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    default="",
    help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
    "the active profile's api_key when role=admin.",
)
@click.confirmation_option(
    prompt=(
        "Rename the target database on the Postgres cluster? "
        "The database must not have any open connections. Continue?"
    )
)
def admin_rename_database(
    old_name: str, new_name: str, service: str | None, admin_key: str,
):
    """ALTER DATABASE on the cluster. One-shot migration helper.

    Does NOT update control-plane rows — follow up with
    'hivemind admin register-existing <name> <new_name> --tenant-id <t_...>'
    to adopt the renamed DB as a tenant.
    """
    admin_key = _resolve_admin_key(admin_key)
    url = _resolve_admin_service(service)
    try:
        resp = _hpost(
            f"{url}/v1/admin/rename-database",
            headers=_admin_headers(admin_key),
            json={"old_name": old_name, "new_name": new_name},
            timeout=60,
        )
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(3)
    click.echo(f"Renamed: {old_name} → {new_name}")


@admin_cli.command("migrate-to-roles")
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    default="",
    help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
    "the active profile's api_key when role=admin.",
)
@click.option(
    "--as-json/--no-as-json", default=False, help="Emit JSON output",
)
def admin_migrate_to_roles(service: str | None, admin_key: str, as_json: bool):
    """Retrofit per-tenant Postgres roles onto pre-existing tenant DBs.

    Idempotent: creates the role if missing, ALTERs the password to the
    current derivation, transfers DB + public-schema ownership, and
    REVOKEs CONNECT from PUBLIC. Required once after upgrading to the
    Layer-1 build of the SQL proxy; tenants provisioned after the
    upgrade already have roles.
    """
    import json as _json

    admin_key = _resolve_admin_key(admin_key)
    url = _resolve_admin_service(service)
    try:
        resp = _hpost(
            f"{url}/v1/admin/migrate-to-roles",
            headers=_admin_headers(admin_key),
            timeout=180,
        )
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(3)

    results = resp.json().get("results", [])
    if as_json:
        click.echo(_json.dumps(results, indent=2, default=str))
        return

    if not results:
        click.echo("(no tenant DBs found)")
        return
    migrated = sum(1 for r in results if r.get("migrated"))
    skipped = sum(1 for r in results if r.get("skipped"))
    errored = sum(1 for r in results if r.get("error"))
    click.echo(
        f"Processed {len(results)} databases: "
        f"{migrated} migrated, {skipped} skipped, {errored} errored"
    )
    for r in results:
        if r.get("migrated"):
            click.echo(f"  OK   {r['db_name']} → role {r['role']}")
        elif r.get("skipped"):
            click.echo(f"  SKIP {r['db_name']} ({r['skipped']})")
        elif r.get("error"):
            click.echo(f"  ERR  {r['db_name']}: {r['error']}", err=True)


@admin_cli.command("sweep-broken-agents")
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    default="",
    help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
    "the active profile's api_key when role=admin.",
)
@click.option(
    "--dry-run/--no-dry-run", default=True,
    help="Default: dry-run (just list orphans). Pass --no-dry-run to delete.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
def admin_sweep_broken_agents(
    service: str | None, admin_key: str, dry_run: bool, as_json: bool,
):
    """Find (and optionally delete) agents whose Docker image is missing.

    Use after a CVM redeploy to clean up stale agents whose images were
    in the old daemon's cache. Default is dry-run; pass --no-dry-run to
    actually delete.
    """
    import json as _json

    admin_key = _resolve_admin_key(admin_key)
    url = _resolve_admin_service(service)
    try:
        resp = _hpost(
            f"{url}/v1/admin/agents/sweep-broken",
            headers=_admin_headers(admin_key),
            params={"dry_run": "true" if dry_run else "false"},
            timeout=120,
        )
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(3)

    data = resp.json()
    if as_json:
        click.echo(_json.dumps(data, indent=2, default=str))
        return

    orphans = data.get("orphans", [])
    if not orphans:
        click.echo("No broken agents found.")
        return

    verb = "would delete" if data.get("dry_run") else "deleted"
    click.echo(
        f"Found {data['count']} orphan agent(s) — {verb} "
        f"{data.get('deleted', 0)}:"
    )
    click.echo(
        f"  {'TENANT_ID':<16} {'AGENT_ID':<14} {'TYPE':<10} {'NAME':<24} IMAGE"
    )
    for o in orphans:
        click.echo(
            f"  {o.get('tenant_id',''):<16} "
            f"{o.get('agent_id',''):<14} "
            f"{o.get('agent_type',''):<10} "
            f"{(o.get('name') or '')[:24]:<24} "
            f"{o.get('image','')}"
        )
    if data.get("dry_run"):
        click.echo("\nRe-run with --no-dry-run to actually delete.")


# ── Admin: on-chain HivemindAppAuth ──
#
# Thin wrappers around `cast send` so the contract owner can approve
# or revoke compose hashes without leaving the hivemind CLI. The
# private key never leaves the operator's machine. Reads
# (`view-release`) go through httpx — no private key required.


@admin_cli.command("approve-hash")
@click.argument("compose_hash")
@click.option(
    "--contract",
    envvar="HIVEMIND_APP_AUTH_CONTRACT",
    required=True,
    help="HivemindAppAuth contract address (or HIVEMIND_APP_AUTH_CONTRACT)",
)
@click.option(
    "--rpc-url",
    envvar="ETH_SEPOLIA_RPC_URL",
    default="https://ethereum-sepolia-rpc.publicnode.com",
    show_default=True,
    help="EVM JSON-RPC URL (or ETH_SEPOLIA_RPC_URL)",
)
@click.option(
    "--private-key",
    envvar="PRIVATE_KEY",
    required=True,
    help="Contract owner's EOA key (or PRIVATE_KEY env var)",
)
@click.option(
    "--git-commit",
    default="uncommitted",
    show_default=True,
    help="Git commit sha bound to this compose hash",
)
@click.option(
    "--compose-yaml-uri",
    default="",
    help="Raw-URL pointer to the compose file (defaults to a github.com stub)",
)
def admin_approve_hash(
    compose_hash: str,
    contract: str,
    rpc_url: str,
    private_key: str,
    git_commit: str,
    compose_yaml_uri: str,
) -> None:
    """Approve a compose_hash on the HivemindAppAuth contract.

    After this lands, any CLI connecting to a CVM running this hash
    gets a silent auto-accept (no y/N prompt). Requires the contract
    owner's EOA private key.
    """
    import shutil
    import subprocess

    if shutil.which("cast") is None:
        click.echo(
            "Error: 'cast' (foundry) not on PATH. Install foundry or "
            "write the transaction with another tool.",
            err=True,
        )
        raise SystemExit(2)

    if not compose_yaml_uri:
        compose_yaml_uri = (
            f"https://github.com/account-link/hivemind-core/blob/"
            f"{git_commit}/deploy/phala/docker-compose.core.yaml"
        )

    if not compose_hash.startswith("0x"):
        compose_hash = "0x" + compose_hash

    cmd = [
        "cast", "send",
        contract,
        "addComposeHash(bytes32,string,string)",
        compose_hash,
        git_commit,
        compose_yaml_uri,
        "--rpc-url", rpc_url,
        "--private-key", private_key,
    ]
    click.echo(f"Approving {compose_hash} on {contract}...")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        click.echo(f"Error: cast send failed.\n{res.stderr}", err=True)
        raise SystemExit(3)
    # Extract tx hash from cast output
    for line in res.stdout.splitlines():
        if line.lower().startswith("transactionhash"):
            click.echo(line.strip())
            break
    click.echo("Approved.")


@admin_cli.command("revoke-hash")
@click.argument("compose_hash")
@click.option(
    "--contract",
    envvar="HIVEMIND_APP_AUTH_CONTRACT",
    required=True,
    help="HivemindAppAuth contract address",
)
@click.option(
    "--rpc-url",
    envvar="ETH_SEPOLIA_RPC_URL",
    default="https://ethereum-sepolia-rpc.publicnode.com",
    show_default=True,
)
@click.option(
    "--private-key",
    envvar="PRIVATE_KEY",
    required=True,
)
@click.confirmation_option(
    prompt=(
        "Revoke this compose hash on-chain? All CLI users will be "
        "hard-rejected when they try to connect. Continue?"
    )
)
def admin_revoke_hash(
    compose_hash: str,
    contract: str,
    rpc_url: str,
    private_key: str,
) -> None:
    """Revoke a compose_hash on the HivemindAppAuth contract."""
    import shutil
    import subprocess

    if shutil.which("cast") is None:
        click.echo("Error: 'cast' (foundry) not on PATH.", err=True)
        raise SystemExit(2)

    if not compose_hash.startswith("0x"):
        compose_hash = "0x" + compose_hash

    cmd = [
        "cast", "send",
        contract,
        "revoke(bytes32)",
        compose_hash,
        "--rpc-url", rpc_url,
        "--private-key", private_key,
    ]
    click.echo(f"Revoking {compose_hash} on {contract}...")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        click.echo(f"Error: cast send failed.\n{res.stderr}", err=True)
        raise SystemExit(3)
    for line in res.stdout.splitlines():
        if line.lower().startswith("transactionhash"):
            click.echo(line.strip())
            break
    click.echo("Revoked.")


@admin_cli.command("list-hashes")
@click.option(
    "--contract",
    envvar="HIVEMIND_APP_AUTH_CONTRACT",
    required=True,
    help="HivemindAppAuth contract address",
)
@click.option(
    "--rpc-url",
    envvar="ETH_SEPOLIA_RPC_URL",
    default="https://ethereum-sepolia-rpc.publicnode.com",
    show_default=True,
)
def admin_list_hashes(contract: str, rpc_url: str) -> None:
    """Print every compose_hash the on-chain registry has ever seen."""
    import shutil
    import subprocess

    if shutil.which("cast") is None:
        click.echo("Error: 'cast' (foundry) not on PATH.", err=True)
        raise SystemExit(2)

    count_res = subprocess.run(
        ["cast", "call", contract, "releaseCount()(uint256)",
         "--rpc-url", rpc_url],
        capture_output=True, text=True,
    )
    if count_res.returncode != 0:
        click.echo(f"Error: {count_res.stderr}", err=True)
        raise SystemExit(3)
    try:
        count = int(count_res.stdout.strip().split()[0])
    except (ValueError, IndexError):
        click.echo(f"Error: unexpected output: {count_res.stdout!r}", err=True)
        raise SystemExit(3)

    if count == 0:
        click.echo("(no releases)")
        return

    click.echo(f"{'HASH':<68} {'STATUS':<10} COMMIT")
    for i in range(count):
        r = subprocess.run(
            ["cast", "call", contract,
             "getRelease(uint256)(bytes32,bool,uint64,uint64,string,string)",
             str(i), "--rpc-url", rpc_url],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            click.echo(f"  (error at index {i}: {r.stderr.strip()})", err=True)
            continue
        # cast prints one value per line for tuples.
        lines = [ln.strip() for ln in r.stdout.strip().splitlines() if ln.strip()]
        if len(lines) < 5:
            continue
        h, approved, _approved_at, _revoked_at, commit = lines[0], lines[1], lines[2], lines[3], lines[4]
        status = "approved" if approved == "true" else "revoked"
        click.echo(f"{h:<68} {status:<10} {commit}")


# ── Trust store inspection (``hivemind trust ...``) ──


@cli.group("trust")
def trust_group():
    """Manage the local compose-hash trust store (``~/.hivemind/trust.json``)."""
    pass


@trust_group.command("show")
@click.argument("service", required=False)
def trust_show(service: str | None):
    """Print the trust entry for SERVICE (or all services)."""
    store = _trust.load_trust()
    services = store.get("services", {})
    if service:
        entry = services.get(service.rstrip("/"))
        if entry is None:
            click.echo(f"No trust entry for {service}.", err=True)
            raise SystemExit(1)
        click.echo(_json.dumps(entry, indent=2, sort_keys=True))
        return
    if not services:
        click.echo("(trust store empty)")
        return
    for url, entry in sorted(services.items()):
        click.echo(f"{url}")
        click.echo(f"  app_id:        {entry.get('app_id', '')}")
        click.echo(
            f"  compose_hash:  {entry.get('approved_compose_hash', '')}"
        )
        click.echo(f"  approved_at:   {entry.get('approved_at', '')}")
        click.echo(f"  first_seen_at: {entry.get('first_seen_at', '')}")
        hist = entry.get("history", [])
        if hist:
            click.echo(f"  history: {len(hist)} prior hash(es)")


@trust_group.command("reset")
@click.argument("service", required=False)
@click.option("--all", "all_services", is_flag=True, help="Clear every service.")
def trust_reset(service: str | None, all_services: bool):
    """Clear the trust entry for SERVICE (or --all)."""
    if all_services and service:
        click.echo("Pass either SERVICE or --all, not both.", err=True)
        raise SystemExit(1)
    if not all_services and not service:
        click.echo(
            "Specify a SERVICE URL or --all.\n"
            "Run 'hivemind trust show' to see what's stored.",
            err=True,
        )
        raise SystemExit(1)
    n = _trust.clear(None if all_services else service)
    click.echo(f"Cleared {n} trust entr{'y' if n == 1 else 'ies'}.")


@trust_group.command("approve")
@click.argument("service", required=False)
def trust_approve(service: str | None):
    """Force-approve the current remote compose_hash for SERVICE.

    Equivalent to answering "y" to the next prompt, without waiting for
    it. Uses the configured service URL when SERVICE is omitted.
    """
    if service is None:
        config = _load_config(check_trust=False)
        service = config["service"]
    service = service.rstrip("/")
    bundle, observed_fp = _fetch_attestation(service)
    if not bundle.get("ready"):
        click.echo(
            f"Cannot approve — attestation unavailable: "
            f"{bundle.get('reason', 'unknown')}",
            err=True,
        )
        raise SystemExit(2)
    att = bundle.get("attestation") or {}
    compose_hash = att.get("compose_hash") or ""
    app_id = att.get("app_id") or ""
    if not compose_hash:
        click.echo("Cannot approve — bundle has no compose_hash.", err=True)
        raise SystemExit(2)
    _trust.record_approval(service, compose_hash, app_id)
    # If the enclave terminates TLS, verify the cert binding and stash
    # the PEM so subsequent CLI commands can pin against it without
    # re-running the full quote check. Without this, every command
    # against an -8100s. URL hits CERTIFICATE_VERIFY_FAILED on the
    # self-signed cert.
    _verify_tls_pin(bundle, observed_fp, service=service)
    click.echo(f"Approved {compose_hash} for {service}.")


if __name__ == "__main__":
    cli()

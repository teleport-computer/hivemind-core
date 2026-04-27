"""Pinned httpx wrappers and tarball helpers for the CLI."""

import io
import tarfile
from pathlib import Path
from urllib.parse import urlparse as _urlparse

import click
import httpx

from .. import trust as _trust

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

"""``trust`` command group: show / reset / approve / attest."""

import json as _json

import click
import httpx

from .. import trust as _trust
from ._config import _load_config
from ._http import _api_error, _hget, _warm_pin_from_trust
from ._shared import _DEFAULT_SERVICE, _run_reproduce
from ._trust import (
    _fetch_attestation,
    _release_metadata_for,
    _verify_tls_pin,
)


# ── Trust store inspection (``hivemind trust ...``) ──


@click.group("trust")
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


@trust_group.command("attest")
@click.option(
    "--service",
    default=None,
    help="Service URL (defaults to active profile, or HIVEMIND_SERVICE).",
)
@click.option(
    "--raw", is_flag=True, help="Emit the full /v1/attestation JSON payload."
)
@click.option(
    "--reproduce",
    is_flag=True,
    help=(
        "Walk the chain of trust from compose_hash → live app_compose "
        "→ on-chain-registered git_sha → repo YAML, and print which "
        "links held."
    ),
)
def trust_attest(service: str | None, raw: bool, reproduce: bool):
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
    if reproduce:
        _run_reproduce(bundle)
        return
    if not bundle.get("ready"):
        click.echo("ready:    false")
        click.echo(f"reason:   {bundle.get('reason', '?')}")
        click.echo(f"version:  {bundle.get('hivemind_version', '?')}")
        return
    att = bundle.get("attestation") or {}
    click.echo("ready:        true")
    click.echo(f"booted_at:    {bundle.get('booted_at', '?')}")
    click.echo(f"app_id:       {att.get('app_id', '?')}")
    click.echo(f"compose_hash: {att.get('compose_hash', '?')}")
    click.echo(f"version:      {att.get('hivemind_version', '?')}")
    if app_auth := att.get("app_auth"):
        click.echo("app_auth:")
        click.echo(f"  contract:   {app_auth.get('contract', '?')}")
        click.echo(f"  chain_id:   {app_auth.get('chain_id', '?')}")
    meta = _release_metadata_for(bundle, att.get("compose_hash") or "")
    if meta and meta.get("compose_uri"):
        click.echo("source:")
        click.echo(f"  git_commit: {meta.get('git_commit', '?')}")
        click.echo(f"  compose:    {meta.get('compose_uri', '?')}")
    if tls := att.get("tls"):
        if tls.get("enabled"):
            click.echo("tls:")
            click.echo(
                f"  fingerprint: {tls.get('cert_fingerprint_sha256_hex', '?')}"
            )
            if pin := tls.get("pinning_url"):
                click.echo(f"  pin URL:     {pin}")
    click.echo(
        "(use --raw for full JSON, --reproduce to verify the chain "
        "back to source)"
    )

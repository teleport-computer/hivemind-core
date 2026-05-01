"""The root ``cli`` Click group."""

import os

import click

from ..version import APP_VERSION


@click.group()
@click.version_option(version=APP_VERSION, prog_name="hivemind")
@click.option(
    "-y",
    "--yes",
    "auto_yes",
    is_flag=True,
    help="Auto-answer 'yes' to the compose-hash approval prompt. "
    "TLS pinning and the on-chain revoke kill-switch still apply, so a "
    "tampered or revoked hash still hard-aborts. Does not accept room "
    "manifests; run `hivemind room accept` first in CI / scripts.",
)
@click.option(
    "--dangerously-skip-attestations",
    "skip_attestations",
    is_flag=True,
    help="Disable ALL attestation verification — no TLS pin, no on-chain "
    "check, no compose-hash prompt. This is an explicit tenant/operator "
    "risk-acceptance bypass for cases where you do not want client-side "
    "attestation. Also bypasses first-use room manifest acceptance.",
)
@click.option(
    "--allow-degraded-attestation",
    "allow_degraded_attestation",
    is_flag=True,
    help="Permit remote services whose CVM attestation cannot be fully "
    "verified. Intended for debugging only; production HTTPS services "
    "fail closed by default.",
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
def cli(
    auto_yes: bool,
    skip_attestations: bool,
    allow_degraded_attestation: bool,
    profile: str,
) -> None:
    """Hivemind — conditional recall for the privacy-quality frontier."""
    # Set the same env vars the trust layer already reads, so we don't
    # have to thread a context object into every subcommand. Flags win
    # over the absence of an env var; if the env var is already set,
    # leave it alone (most permissive of {flag, env} wins).
    if auto_yes:
        os.environ["HIVEMIND_TRUST_ALL"] = "1"
    if skip_attestations:
        os.environ["HIVEMIND_NO_TRUST_CHECK"] = "1"
    if allow_degraded_attestation:
        os.environ["HIVEMIND_ALLOW_DEGRADED_ATTESTATION"] = "1"
    if profile:
        os.environ["HIVEMIND_PROFILE"] = profile

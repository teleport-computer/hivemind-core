"""``admin`` command group: tenants, hashes, sweep."""

import json as _json
import shlex
from decimal import Decimal

import click
import httpx

from ._http import _api_error, _hdelete, _hget, _hpost
from ._shared import (
    _admin_headers,
    _resolve_admin_key,
    _resolve_admin_service,
)


# ── Admin: tenant management ──


@click.group("admin")
def admin_cli():
    """Multi-tenant admin: tenants, on-chain hash governance, sweeps.

    Requires the server's HIVEMIND_ADMIN_KEY. Pass it with --admin-key
    or set HIVEMIND_ADMIN_KEY in your shell environment.
    """
    pass


@admin_cli.group("tenants")
def admin_tenants():
    """Provision, list, delete, rename tenants; migrate to per-tenant roles."""
    pass


@admin_cli.group("hashes")
def admin_hashes():
    """On-chain compose-hash governance (approve / revoke / list)."""
    pass


@admin_cli.group("billing")
def admin_billing():
    """Tenant credits, usage ledger, and model prices."""
    pass


def _micro_usd(value) -> str:
    dec = Decimal(int(value or 0)) / Decimal(1_000_000)
    return f"${dec.quantize(Decimal('0.000001'))}"


def _tenant_init_command(*, service: str, profile: str, api_key: str) -> str:
    parts = [
        "hivemind",
        "-y",
        "--profile",
        profile,
        "init",
        "--service",
        service,
        "--api-key",
        api_key,
    ]
    return " ".join(shlex.quote(part) for part in parts)


@admin_tenants.command("create")
@click.argument("name")
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    default="",
    help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
    "the active profile's api_key when role=admin.",
)
@click.option(
    "--allow-duplicate-name",
    is_flag=True,
    help="Create even if another tenant already has this display name.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
def admin_create_tenant(
    name: str,
    service: str | None,
    admin_key: str,
    allow_duplicate_name: bool,
    as_json: bool,
):
    """Provision a new tenant. Prints the key and tenant setup command."""
    admin_key = _resolve_admin_key(admin_key)
    url = _resolve_admin_service(service)
    payload = {"name": name}
    if allow_duplicate_name:
        payload["allow_duplicate_name"] = True
    try:
        resp = _hpost(
            f"{url}/v1/admin/tenants",
            headers=_admin_headers(admin_key),
            json=payload,
            timeout=60,
        )
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(3)
    data = resp.json()
    tenant_setup = _tenant_init_command(
        service=url,
        profile=str(data.get("name") or name),
        api_key=str(data["api_key"]),
    )
    data["tenant_setup_command"] = tenant_setup
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    click.echo(f"Tenant:   {data['tenant_id']}  ({data['name']})")
    click.echo(f"Database: {data['db_name']}")
    click.echo("")
    click.echo("API key (store it now — we won't show it again):")
    click.echo(f"  {data['api_key']}")
    click.echo("")
    click.echo("Send this one-liner to the tenant:")
    click.echo(f"  {tenant_setup}")


@admin_tenants.command("list")
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


@admin_billing.command("balance")
@click.argument("tenant_id")
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    default="",
    help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
    "the active profile's api_key when role=admin.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
def admin_billing_balance(
    tenant_id: str,
    service: str | None,
    admin_key: str,
    as_json: bool,
):
    """Show tenant billing balance and recent ledger entries."""
    admin_key = _resolve_admin_key(admin_key)
    url = _resolve_admin_service(service)
    try:
        resp = _hget(
            f"{url}/v1/admin/billing/{tenant_id}",
            headers=_admin_headers(admin_key),
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
        click.echo(_json.dumps(data, indent=2, default=str))
        return
    click.echo(f"Tenant:  {data['tenant_id']}")
    click.echo(f"Balance: {_micro_usd(data.get('balance_micro_usd'))}")
    ledger = data.get("ledger") or []
    if not ledger:
        return
    click.echo("")
    click.echo(f"{'WHEN':<12} {'KIND':<16} {'AMOUNT':>14} RUN")
    for row in ledger:
        click.echo(
            f"{str(row.get('created_at',''))[:12]:<12} "
            f"{str(row.get('kind',''))[:16]:<16} "
            f"{_micro_usd(row.get('amount_micro_usd')):>14} "
            f"{row.get('run_id') or ''}"
        )


@admin_billing.command("grant")
@click.argument("tenant_id")
@click.argument("amount_usd")
@click.option("--note", default="", help="Ledger note.")
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    default="",
    help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
    "the active profile's api_key when role=admin.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
def admin_billing_grant(
    tenant_id: str,
    amount_usd: str,
    note: str,
    service: str | None,
    admin_key: str,
    as_json: bool,
):
    """Grant tenant prepaid billing credit in USD."""
    admin_key = _resolve_admin_key(admin_key)
    url = _resolve_admin_service(service)
    try:
        resp = _hpost(
            f"{url}/v1/admin/billing/{tenant_id}/credits",
            headers=_admin_headers(admin_key),
            json={"amount_usd": amount_usd, "note": note},
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
        click.echo(_json.dumps(data, indent=2, default=str))
        return
    click.echo(
        f"Granted {_micro_usd(data.get('amount_micro_usd'))} "
        f"to {tenant_id}; balance {_micro_usd(data.get('balance_micro_usd'))}"
    )


@admin_billing.command("prices")
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    default="",
    help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
    "the active profile's api_key when role=admin.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
def admin_billing_prices(
    service: str | None,
    admin_key: str,
    as_json: bool,
):
    """List model price snapshots used for run billing."""
    admin_key = _resolve_admin_key(admin_key)
    url = _resolve_admin_service(service)
    try:
        resp = _hget(
            f"{url}/v1/admin/billing/prices",
            headers=_admin_headers(admin_key),
            timeout=30,
        )
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(3)
    prices = resp.json().get("prices", [])
    if as_json:
        click.echo(_json.dumps(prices, indent=2, default=str))
        return
    click.echo(f"{'PROVIDER':<12} {'MODEL':<36} {'PROMPT/M':>12} {'OUT/M':>12}")
    for p in prices:
        click.echo(
            f"{p.get('provider',''):<12} "
            f"{str(p.get('model',''))[:36]:<36} "
            f"{_micro_usd(p.get('prompt_microusd_per_mtok')):>12} "
            f"{_micro_usd(p.get('completion_microusd_per_mtok')):>12}"
        )


@admin_billing.command("set-price")
@click.argument("provider")
@click.argument("model")
@click.option(
    "--prompt-usd-per-million",
    required=True,
    help="Input-token price in USD per 1M tokens.",
)
@click.option(
    "--completion-usd-per-million",
    required=True,
    help="Output-token price in USD per 1M tokens.",
)
@click.option("--source", default="admin", show_default=True)
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    default="",
    help="Admin bearer token. Defaults to HIVEMIND_ADMIN_KEY or "
    "the active profile's api_key when role=admin.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
def admin_billing_set_price(
    provider: str,
    model: str,
    prompt_usd_per_million: str,
    completion_usd_per_million: str,
    source: str,
    service: str | None,
    admin_key: str,
    as_json: bool,
):
    """Create or update one model price snapshot."""
    admin_key = _resolve_admin_key(admin_key)
    url = _resolve_admin_service(service)
    payload = {
        "provider": provider,
        "model": model,
        "prompt_usd_per_million": prompt_usd_per_million,
        "completion_usd_per_million": completion_usd_per_million,
        "source": source,
    }
    try:
        resp = _hpost(
            f"{url}/v1/admin/billing/prices",
            headers=_admin_headers(admin_key),
            json=payload,
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
        click.echo(_json.dumps(data, indent=2, default=str))
        return
    click.echo(f"Set price for {data['provider']}/{data['model']}")


@admin_tenants.command("delete")
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


@admin_tenants.command("rename")
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

    Does NOT update control-plane rows — the tenant row's db_name still
    points at <old_name>. Use this only for low-level cluster moves.
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


@admin_tenants.command("migrate-roles")
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


@admin_cli.command("sweep")
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


@admin_hashes.command("approve")
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
@click.option(
    "--replace",
    is_flag=True,
    help="If already approved, revoke then re-add with the supplied metadata.",
)
def admin_approve_hash(
    compose_hash: str,
    contract: str,
    rpc_url: str,
    private_key: str,
    git_commit: str,
    compose_yaml_uri: str,
    replace: bool,
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

    if replace:
        check = subprocess.run(
            [
                "cast", "call",
                "--rpc-url", rpc_url,
                contract,
                "isAppAllowed(bytes32)(bool)",
                compose_hash,
            ],
            capture_output=True,
            text=True,
        )
        if check.returncode == 0 and check.stdout.strip() == "true":
            click.echo(f"Revoking existing approval for {compose_hash}...")
            revoke = subprocess.run(
                [
                    "cast", "send",
                    contract,
                    "revoke(bytes32)",
                    compose_hash,
                    "--rpc-url", rpc_url,
                    "--private-key", private_key,
                ],
                capture_output=True,
                text=True,
            )
            if revoke.returncode != 0:
                click.echo(
                    f"Error: cast revoke failed.\n{revoke.stderr}",
                    err=True,
                )
                raise SystemExit(3)

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


@admin_hashes.command("revoke")
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


@admin_hashes.command("list")
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

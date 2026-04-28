"""``tokens`` command group: issue / list / revoke."""

import json as _json

import click
import httpx

from ._config import _headers, _load_config
from ._http import _api_error, _hdelete, _hget, _hpost


# ── Capability tokens ──
#
# Owner-side commands to issue / list / revoke delegated query tokens.
#   * ``query`` (``hmq_…``) — recipient may submit prompts via /v1/query
#     and upload their own query agent via /v1/query-agents/submit. Every
#     such call is forced through the scope agent the owner pins here.
#
# Plaintext is shown ONCE at issue. Loss == revoke + reissue.


@click.group("tokens")
def tokens_cli():
    """Issue / list / revoke delegated capability tokens."""
    pass


@tokens_cli.command("issue")
@click.option(
    "--label",
    default="",
    help="Free-form label shown in `tokens list` for your own bookkeeping",
)
@click.option(
    "--scope-agent",
    "scope_agent",
    default=None,
    help="Pin every prompt through this scope agent id",
)
@click.option(
    "--can-upload-query-agent",
    "can_upload_query_agent",
    is_flag=True,
    default=False,
    help=(
        "Allow this token to upload its own query agent code via "
        "/v1/query-agents/submit. Off by default — opt-in cedes "
        "execution surface to the recipient. The scope agent still "
        "policies all SQL the recipient's code emits."
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def tokens_issue(
    label: str,
    scope_agent: str | None,
    can_upload_query_agent: bool,
    as_json: bool,
):
    """Mint a new query capability token (hmq_).

    The plaintext is printed exactly once — copy it now or revoke + reissue.
    Only the hash is stored on the core CVM.
    """
    config = _load_config()
    service = config["service"]
    headers = _headers(config)
    if "Authorization" not in headers:
        click.echo("Error: no api_key in config. Run 'hivemind init'.", err=True)
        raise SystemExit(1)

    if not scope_agent:
        scope_agent = config.get("scope_agent_id")
    if not scope_agent:
        click.echo(
            "Error: --scope-agent is required (no scope_agent_id in active "
            "profile either)",
            err=True,
        )
        raise SystemExit(2)

    body = {
        "kind": "query",
        "label": label,
        "constraints": {
            "scope_agent_id": scope_agent,
            "can_upload_query_agent": bool(can_upload_query_agent),
        },
    }
    try:
        resp = _hpost(
            f"{service}/v1/tokens", headers=headers, json=body, timeout=30,
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
    click.echo(f"kind:        {data['kind']}")
    click.echo(f"token_id:    {data['token_id']}")
    if data.get("label"):
        click.echo(f"label:       {data['label']}")
    click.echo(f"constraints: {_json.dumps(data['constraints'])}")
    click.echo("")
    click.echo("token (copy now — shown only once):")
    click.echo(f"  {data['token']}")


@tokens_cli.command("list")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def tokens_list(as_json: bool):
    """List delegated tokens for this tenant. Plaintext is never shown."""
    config = _load_config()
    service = config["service"]
    headers = _headers(config)
    if "Authorization" not in headers:
        click.echo("Error: no api_key in config. Run 'hivemind init'.", err=True)
        raise SystemExit(1)
    try:
        resp = _hget(f"{service}/v1/tokens", headers=headers, timeout=30)
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(3)
    rows = resp.json().get("tokens", [])
    if as_json:
        click.echo(_json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("(no capability tokens yet — `hivemind tokens issue …`)")
        return
    click.echo(
        f"{'TOKEN_ID':<14} {'KIND':<6} {'STATUS':<8} {'LABEL':<24} CONSTRAINTS"
    )
    for r in rows:
        status = "revoked" if r.get("revoked_at") else "active"
        label = (r.get("label") or "")[:24]
        cons = _json.dumps(r.get("constraints") or {})
        click.echo(
            f"{r['token_id']:<14} {r['kind']:<6} {status:<8} "
            f"{label:<24} {cons}"
        )


@tokens_cli.command("audit")
@click.argument("token_id")
@click.option(
    "--limit",
    type=int,
    default=50,
    show_default=True,
    help="Max rows to show.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def tokens_audit(token_id: str, limit: int, as_json: bool):
    """Show what a recipient hmq_ token has actually done.

    Lists every run initiated by the given token (12-hex prefix from
    ``hivemind tokens list``). Owner-only.
    """
    config = _load_config()
    service = config["service"]
    headers = _headers(config)
    if "Authorization" not in headers:
        click.echo("Error: no api_key in config. Run 'hivemind init'.", err=True)
        raise SystemExit(1)
    tid = (token_id or "").strip().lower()
    if len(tid) < 6:
        click.echo("Error: token_id must be at least 6 hex chars.", err=True)
        raise SystemExit(2)
    from urllib.parse import quote as _quote
    url = (
        f"{service}/v1/agent-runs?limit={limit}&token_id={_quote(tid)}"
    )
    try:
        resp = _hget(url, headers=headers, timeout=30)
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(3)
    rows = resp.json()
    if as_json:
        click.echo(_json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        click.echo(f"(no runs initiated by token {tid})")
        return
    click.echo(f"Runs initiated by token {tid}:")
    click.echo(
        f"{'RUN_ID':<14} {'AGENT_ID':<14} {'STATUS':<10} CREATED"
    )
    for r in rows:
        click.echo(
            f"{r.get('run_id','?'):<14} "
            f"{r.get('agent_id','?'):<14} "
            f"{str(r.get('status','?')):<10} "
            f"{r.get('created_at','')}"
        )


@tokens_cli.command("revoke")
@click.argument("token_id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def tokens_revoke(token_id: str, as_json: bool):
    """Revoke a token by its short id (from `tokens list`).

    Soft-revoke: the row stays for audit (``revoked_at`` set). Future
    requests with that token are 401'd by ``resolve_any``.
    """
    config = _load_config()
    service = config["service"]
    headers = _headers(config)
    if "Authorization" not in headers:
        click.echo("Error: no api_key in config. Run 'hivemind init'.", err=True)
        raise SystemExit(1)
    try:
        resp = _hdelete(
            f"{service}/v1/tokens/{token_id}", headers=headers, timeout=30,
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
    click.echo(f"revoked: {data['token_id']}")

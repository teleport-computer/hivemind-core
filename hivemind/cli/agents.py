"""``agents`` command group: list / get / delete.

Mirrors the website's /app/agents page: list all room-agents on the active
profile's tenant, inspect one, or delete one. Delete refuses if any non-
revoked room references the agent (scope_agent_id, fixed_query_agent_id,
fixed_mediator_agent_id), since the server has no concept of "is this
agent attached to a room" — it would happily 404 every subsequent run
that depended on the deleted agent. We do the reference check client-
side so the failure mode is obvious.
"""

import click

from . import _hdelete, _hget
from ._config import _headers, _load_config
from ._http import _api_error


@click.group("agents")
def agents_cli():
    """List, inspect, and delete room agents on the active tenant."""
    pass


@agents_cli.command("list")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit raw JSON on stdout instead of the formatted table.",
)
def agents_list(as_json: bool):
    """List room-agents registered on the active tenant."""
    config = _load_config()
    service = config["service"].rstrip("/")
    headers = _headers(config)

    resp = _hget(f"{service}/v1/room-agents", headers=headers, timeout=30)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(1)
    body = resp.json()
    agents = body if isinstance(body, list) else (body.get("agents") or [])

    if as_json:
        import json as _json
        click.echo(_json.dumps(agents, indent=2))
        return

    if not agents:
        click.echo("No agents registered. Upload one via 'hmctl room create'.")
        return

    rooms = _list_rooms(service, headers)
    usage = _index_usage(rooms)

    click.echo(
        f"{'NAME':<28} {'TYPE':<10} {'HARNESS':<11} "
        f"{'INSPECTION':<12} {'IN ROOMS':<10} AGENT_ID"
    )
    for a in agents:
        name = (a.get("name") or "—")[:28]
        atype = a.get("agent_type") or "—"
        harness = a.get("harness") or "claude_code"
        inspection = a.get("inspection_mode") or "full"
        agent_id = a.get("agent_id") or ""
        n = len(usage.get(agent_id, []))
        in_rooms = "—" if n == 0 else str(n)
        click.echo(
            f"{name:<28} {atype:<10} {harness:<11} "
            f"{inspection:<12} {in_rooms:<10} {agent_id}"
        )


@agents_cli.command("get")
@click.argument("agent_id")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit raw JSON on stdout instead of formatted output.",
)
def agents_get(agent_id: str, as_json: bool):
    """Show full record for one agent (manifest + file list)."""
    config = _load_config()
    service = config["service"].rstrip("/")
    headers = _headers(config)

    resp = _hget(
        f"{service}/v1/room-agents/{agent_id}", headers=headers, timeout=30
    )
    if resp.status_code == 404:
        click.echo(f"Agent {agent_id!r} not found", err=True)
        raise SystemExit(1)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(1)
    agent = resp.json()

    files_resp = _hget(
        f"{service}/v1/room-agents/{agent_id}/files",
        headers=headers,
        timeout=30,
    )
    files = []
    if files_resp.status_code == 200:
        files = (files_resp.json() or {}).get("files", [])

    if as_json:
        import json as _json
        click.echo(_json.dumps({**agent, "files": files}, indent=2))
        return

    click.echo(f"agent_id:        {agent.get('agent_id')}")
    click.echo(f"name:            {agent.get('name')}")
    click.echo(f"type:            {agent.get('agent_type') or '—'}")
    click.echo(f"image:           {agent.get('image') or '—'}")
    click.echo(f"harness:         {agent.get('harness') or 'claude_code'}")
    click.echo(f"inspection_mode: {agent.get('inspection_mode') or 'full'}")
    if files:
        click.echo("files:")
        for f in files:
            size = f.get("size")
            suffix = f" ({size}B)" if size is not None else ""
            click.echo(f"  - {f.get('path')}{suffix}")


@agents_cli.command("delete")
@click.argument("agent_id")
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    help="Skip the confirmation prompt.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Delete even if rooms still reference this agent. Use with care: "
    "subsequent runs in those rooms will 404 on the missing agent.",
)
def agents_delete(agent_id: str, yes: bool, force: bool):
    """Delete an agent. Refuses by default if rooms reference it."""
    config = _load_config()
    service = config["service"].rstrip("/")
    headers = _headers(config)

    rooms = _list_rooms(service, headers)
    refs = _refs_to_agent(rooms, agent_id)
    if refs and not force:
        click.echo(
            f"Refusing to delete: {len(refs)} active room"
            f"{'' if len(refs) == 1 else 's'} reference this agent.",
            err=True,
        )
        for ref in refs:
            click.echo(
                f"  - {ref['room_id']} ({ref['name']}) as {ref['role']}",
                err=True,
            )
        click.echo(
            "Revoke or rebuild those rooms first, or pass --force to "
            "delete anyway (subsequent runs in those rooms will fail).",
            err=True,
        )
        raise SystemExit(1)

    if not yes:
        prompt = f"Delete agent {agent_id}?"
        if refs and force:
            prompt = (
                f"Force-delete agent {agent_id} despite {len(refs)} "
                f"active reference{'s' if len(refs) != 1 else ''}?"
            )
        click.confirm(prompt, abort=True)

    resp = _hdelete(
        f"{service}/v1/room-agents/{agent_id}", headers=headers, timeout=30
    )
    if resp.status_code == 404:
        click.echo(f"Agent {agent_id!r} not found", err=True)
        raise SystemExit(1)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(1)
    click.echo(f"Deleted agent {agent_id}.")


# ── Internals ──


def _list_rooms(service: str, headers: dict) -> list[dict]:
    """Best-effort fetch of the rooms list. Empty on any error so the caller
    can still operate when /v1/rooms is unreachable (e.g., minimal smoke
    deploys); the user just doesn't get the cross-reference safety net.
    """
    try:
        resp = _hget(f"{service}/v1/rooms", headers=headers, timeout=30)
        if resp.status_code >= 400:
            return []
        body = resp.json()
        return body.get("rooms") if isinstance(body, dict) else (body or [])
    except Exception:
        return []


def _index_usage(rooms: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rooms:
        if r.get("revoked_at"):
            continue
        for field, role in (
            ("scope_agent_id", "scope"),
            ("fixed_query_agent_id", "query"),
            ("fixed_mediator_agent_id", "mediator"),
        ):
            agent_id = r.get(field)
            if not agent_id:
                continue
            out.setdefault(agent_id, []).append(
                {
                    "room_id": r.get("room_id"),
                    "name": r.get("name", ""),
                    "role": role,
                }
            )
    return out


def _refs_to_agent(rooms: list[dict], agent_id: str) -> list[dict]:
    return _index_usage(rooms).get(agent_id, [])

"""``agents`` command group: list / rm / upload / attest."""

import json as _json
import time
from pathlib import Path

import click
import httpx

from ._config import (
    _headers,
    _load_config,
    _profile_name,
    _save_config,
)
from ._http import (
    _api_error,
    _hdelete,
    _hget,
    _hpost,
    _http_get,
    _tarball_from_dir,
)


@click.group("agents")
def agents_cli():
    """List / upload / inspect / remove agents on the service."""
    pass


@agents_cli.command("list")
@click.option(
    "--type",
    "agent_type",
    default=None,
    help="Filter by agent type (query/scope/mediator/index)",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def agents_list(agent_type: str | None, as_json: bool):
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


@agents_cli.command("rm")
@click.argument("agent_id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def agents_rm(agent_id: str, as_json: bool):
    """Delete a single agent by ID.

    Use ``hivemind agents list`` to list. Bulk cleanup of agents whose
    Docker images are missing is ``hivemind admin sweep``.
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


@agents_cli.command("upload")
@click.argument(
    "path",
    type=click.Path(
        exists=True, file_okay=True, dir_okay=True, path_type=Path
    ),
)
@click.option(
    "--type",
    "agent_type",
    type=click.Choice(["scope", "query", "index", "mediator"]),
    required=True,
    help="Agent role.",
)
@click.option(
    "--name",
    default=None,
    help="Agent name (defaults to the directory or archive basename).",
)
@click.option(
    "--description", default="", help="Free-form description for the listing."
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def agents_upload(
    path: Path,
    agent_type: str,
    name: str | None,
    description: str,
    as_json: bool,
):
    """Upload an arbitrary directory or .tar.gz to /v1/agents/upload.

    Bring-your-own agent — use this when the default scope template
    isn't enough (custom Dockerfile, extra runtime deps, alternative
    LLM glue). Polls the build run, then prints the new agent_id. For
    ``--type scope`` the new id is also saved to the active profile so
    ``hivemind query`` / ``hivemind share`` pick it up automatically.
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
            f"Unsupported path: {path}. Pass a directory or .tar.gz."
        )

    if not as_json:
        click.echo(
            f"Uploading {archive_name} ({len(archive_bytes)} bytes) "
            f"as {agent_type}…"
        )

    try:
        resp = _hpost(
            f"{service}/v1/agents/upload",
            files={
                "archive": (
                    archive_name, archive_bytes, "application/gzip",
                )
            },
            data={
                "name": agent_name,
                "agent_type": agent_type,
                "description": description
                or f"hivemind agents upload {path.name}",
            },
            headers=headers,
            timeout=60,
        )
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(
            f"Error: Upload failed ({resp.status_code}): "
            f"{_api_error(resp)}",
            err=True,
        )
        raise SystemExit(3)

    result = resp.json()
    agent_id = result["agent_id"]
    run_id = result.get("run_id")

    if run_id:
        if not as_json:
            click.echo(f"Building image (run: {run_id})…")
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
                    if not as_json:
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
            if not as_json:
                click.echo(
                    "Warning: Build still in progress. Check with "
                    f"'hivemind runs {run_id}'.",
                    err=True,
                )

    if agent_type == "scope":
        config["scope_agent_id"] = agent_id
        _save_config(config)

    if as_json:
        click.echo(
            _json.dumps(
                {"agent_id": agent_id, "run_id": run_id, "type": agent_type},
                indent=2,
            )
        )
        return
    click.echo(f"agent_id: {agent_id}")
    if agent_type == "scope":
        click.echo(
            f"(saved as scope_agent_id in profile '{_profile_name()}')"
        )


# ── Agent attestation ──
#
# Pin + publish a verifiable record of an uploaded agent: saved config,
# stable sha256 over source files, resolved Docker image digest, live
# CVM attestation bundle. Owner passes ``AGENT_ID``; ``hmq_`` token
# holders omit it — the server resolves the bound scope_agent_id from
# the token (via the /v1/scope-attest alias), so a recipient can audit
# the policy they're about to query without needing to know its id.


@agents_cli.command("attest")
@click.argument("agent_id", required=False)
@click.option(
    "--show-file",
    "show_file",
    default=None,
    help="Print the contents of one extracted file in addition to the digest.",
)
@click.option(
    "--list-files",
    is_flag=True,
    help="List every extracted file path + size.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def agents_attest(
    agent_id: str | None,
    show_file: str | None,
    list_files: bool,
    as_json: bool,
):
    """Pin and publish an attestation for any registered agent.

    Owner usage::

        hivemind agents attest <agent_id>

    Token-holder usage (no id needed — token pins the scope agent)::

        HIVEMIND_PROFILE=recipient hivemind agents attest

    Returns the agent config, a stable digest over its source files,
    the resolved Docker image digest, and the live attestation bundle
    (compose_hash, app_id, quote, TLS pubkey). Cross-check compose_hash
    against the NotarizedAppAuth contract; re-derive the source-files
    digest by re-fetching the files.
    """
    config = _load_config()
    service = config["service"]
    headers = _headers(config)
    if "Authorization" not in headers:
        click.echo(
            "Error: no api_key in config. Run 'hivemind init'.", err=True
        )
        raise SystemExit(1)

    if agent_id:
        url = f"{service}/v1/agents/{agent_id}/attest"
    else:
        url = f"{service}/v1/scope-attest"
    try:
        r = _hget(url, headers=headers, timeout=30)
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if r.status_code >= 400:
        click.echo(f"Error {r.status_code}: {_api_error(r)}", err=True)
        raise SystemExit(3)
    data = r.json()
    resolved_id = data.get("agent_id") or data.get("scope_agent_id") or ""

    files_listing: list[dict] | None = None
    if list_files:
        try:
            rl = _hget(
                f"{service}/v1/agents/{resolved_id}/files",
                headers=headers,
                timeout=30,
            )
        except httpx.RequestError as e:
            click.echo(f"Error: {e}", err=True)
            raise SystemExit(2)
        if rl.status_code >= 400:
            click.echo(
                f"Error {rl.status_code}: {_api_error(rl)}", err=True
            )
            raise SystemExit(3)
        files_listing = rl.json().get("files", [])

    file_body: str | None = None
    if show_file:
        try:
            rf = _hget(
                f"{service}/v1/agents/{resolved_id}/files/{show_file}",
                headers=headers,
                timeout=30,
            )
        except httpx.RequestError as e:
            click.echo(f"Error: {e}", err=True)
            raise SystemExit(2)
        if rf.status_code >= 400:
            click.echo(
                f"Error {rf.status_code}: {_api_error(rf)}", err=True
            )
            raise SystemExit(3)
        file_body = rf.text

    if as_json:
        out = dict(data)
        if files_listing is not None:
            out["files"] = files_listing
        if file_body is not None:
            out["file"] = {"path": show_file, "content": file_body}
        click.echo(_json.dumps(out, indent=2))
        return

    agent = data.get("agent") or {}
    img = data.get("image_digest") or {}
    click.echo(f"agent_id:            {resolved_id}")
    click.echo(f"name:                {agent.get('name', '')}")
    click.echo(f"image:               {agent.get('image', '')}")
    if img.get("id"):
        click.echo(f"image.id:            {img['id']}")
    for d in img.get("repo_digests") or []:
        click.echo(f"image.repo_digest:   {d}")
    click.echo(f"files_count:         {data['files_count']}")
    click.echo(f"files_digest_sha256: {data['files_digest_sha256']}")
    att = data.get("attestation") or {}
    inner = att.get("attestation") or {}
    if inner:
        click.echo("attestation:")
        click.echo(f"  compose_hash: {inner.get('compose_hash', '')}")
        click.echo(f"  app_id:       {inner.get('app_id', '')}")
    if files_listing is not None:
        click.echo("")
        click.echo("files:")
        for f in files_listing:
            click.echo(f"  {f['size_bytes']:>10}  {f['path']}")
    if file_body is not None:
        click.echo("")
        click.echo(f"── {show_file} ──")
        click.echo(file_body)

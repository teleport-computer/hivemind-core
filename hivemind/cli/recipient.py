"""Recipient-side commands: ask, query, run, runs."""

import json as _json
import time
from pathlib import Path

import click
import httpx

from ._config import _headers, _load_config, _save_config
from ._http import (
    _api_error,
    _http_get,
    _tarball_from_dir,
)
from ._shared import (
    _artifact_url,
    _emit_run_result,
    _parse_hmq_uri,
    _query_async,
    _query_sync,
)


# Test-patchable HTTP trampolines. See owner.py for rationale.
def _hget(*a, **kw):
    from . import _hget as _f
    return _f(*a, **kw)


def _hpost(*a, **kw):
    from . import _hpost as _f
    return _f(*a, **kw)


@click.command()
@click.argument("uri")
@click.argument("question")
@click.option(
    "--sync",
    "force_sync",
    is_flag=True,
    help=(
        "Force synchronous /v1/query (60s gateway timeout). Default is "
        "async submit+poll, which is the only mode that survives the "
        "Phala gateway's nginx 60s read timeout for slow agent runs."
    ),
)
@click.option(
    "--max-tokens",
    type=int,
    default=None,
    help="Per-call cap on total bridge LLM tokens.",
)
@click.option(
    "--max-llm-calls",
    type=int,
    default=None,
    help="Per-call cap on number of bridge LLM calls.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=int,
    default=None,
    help=(
        "Per-call container timeout in seconds (overrides the agent's "
        "configured timeout, still capped by global_timeout_seconds)."
    ),
)
@click.option(
    "--model",
    type=str,
    default=None,
    help=(
        "LLM model override for this call (e.g. moonshotai/kimi-k2.6, "
        "moonshotai/kimi-k2.5, anthropic/claude-haiku-4.5). Empty falls "
        "back to per-role server config, then to the global default."
    ),
)
@click.option(
    "--provider",
    type=str,
    default=None,
    help=(
        "LLM provider override for this call. 'openrouter' (default) or "
        "'tinfoil' (requires HIVEMIND_TINFOIL_API_KEY on the server). Lets "
        "a recipient flip provider per-question without re-deploying."
    ),
)
def ask(
    uri: str,
    question: str,
    force_sync: bool,
    max_tokens: int | None,
    max_llm_calls: int | None,
    timeout_seconds: int | None,
    model: str | None,
    provider: str | None,
):
    """Send a query through a hmq:// URI shared by an owner.

    Verifies the trust pins (compose_hash, files_digest) encoded in the
    URI against the live service before sending. No profile, no
    pre-registered config — just the URI plus a question.

    Example::

        hivemind ask 'hmq://hivemind.example/abc123?token=hmq_xyz&compose=...' \\
            "How many videos did I watch this month?"
    """
    parsed = _parse_hmq_uri(uri)
    service = parsed["service"]
    scope_id = parsed["scope_agent_id"]
    headers = {"Authorization": f"Bearer {parsed['token']}"}

    if parsed["compose_hash"]:
        try:
            r = _hget(f"{service}/v1/attestation", timeout=15)
        except httpx.RequestError as e:
            click.echo(f"Error fetching attestation: {e}", err=True)
            raise SystemExit(2)
        if r.status_code >= 400:
            click.echo(
                f"Error {r.status_code} fetching attestation: "
                f"{_api_error(r)}",
                err=True,
            )
            raise SystemExit(3)
        live_compose = (
            (r.json().get("attestation") or {}).get("compose_hash") or ""
        ).lower()
        if live_compose != parsed["compose_hash"]:
            click.echo(
                "Error: compose_hash pin mismatch.\n"
                f"  Expected (URI):  {parsed['compose_hash']}\n"
                f"  Live (service):  {live_compose}\n"
                "Aborting — service may have been redeployed since the "
                "URI was issued. Ask the owner for a fresh URI.",
                err=True,
            )
            raise SystemExit(4)

    if parsed["files_digest"]:
        try:
            r = _hget(
                f"{service}/v1/agents/{scope_id}/attest",
                headers=headers,
                timeout=30,
            )
        except httpx.RequestError as e:
            click.echo(f"Error fetching scope pins: {e}", err=True)
            raise SystemExit(2)
        if r.status_code >= 400:
            click.echo(
                f"Error {r.status_code} fetching scope pins: "
                f"{_api_error(r)}",
                err=True,
            )
            raise SystemExit(3)
        # The URI's ``files=`` is the *attested* digest (over public
        # files only, excluding private prompts/.env). Compare against
        # ``attested_files_digest_sha256`` when available; pre-Phase-1
        # servers only expose ``files_digest_sha256``, in which case
        # both digests are identical (no private files), so the fallback
        # is correct.
        body = r.json()
        live_files = (
            body.get("attested_files_digest_sha256")
            or body.get("files_digest_sha256")
            or ""
        ).lower()
        if live_files != parsed["files_digest"]:
            click.echo(
                "Error: files_digest pin mismatch.\n"
                f"  Expected (URI):  {parsed['files_digest']}\n"
                f"  Live (service):  {live_files}\n"
                "Aborting — scope agent source has changed since the URI "
                "was issued.",
                err=True,
            )
            raise SystemExit(4)

    payload: dict = {"query": question, "scope_agent_id": scope_id}
    qa_id = (parsed.get("query_agent_id") or "").strip()
    if qa_id:
        payload["query_agent_id"] = qa_id
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if max_llm_calls is not None:
        payload["max_llm_calls"] = max_llm_calls
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds
    if model:
        payload["model"] = model
    if provider:
        payload["provider"] = provider
    if force_sync:
        _query_sync(service, headers, payload)
    else:
        _query_async(service, headers, payload)


@click.command("query")
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
@click.option(
    "--max-tokens",
    type=int,
    default=None,
    help="Per-call cap on total bridge LLM tokens.",
)
@click.option(
    "--max-llm-calls",
    type=int,
    default=None,
    help="Per-call cap on number of bridge LLM calls.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=int,
    default=None,
    help=(
        "Per-call container timeout in seconds (overrides the agent's "
        "configured timeout, still capped by global_timeout_seconds)."
    ),
)
@click.option(
    "--model",
    type=str,
    default=None,
    help=(
        "LLM model override for this call (e.g. moonshotai/kimi-k2.6, "
        "moonshotai/kimi-k2.5, anthropic/claude-haiku-4.5). Empty falls "
        "back to per-role server config, then to the global default."
    ),
)
@click.option(
    "--provider",
    type=str,
    default=None,
    help=(
        "LLM provider override for this call. 'openrouter' (default) or "
        "'tinfoil' (requires HIVEMIND_TINFOIL_API_KEY on the server)."
    ),
)
def query_cmd(
    question: str,
    endpoint: str | None,
    use_async: bool,
    query_agent: str | None,
    max_tokens: int | None,
    max_llm_calls: int | None,
    timeout_seconds: int | None,
    model: str | None,
    provider: str | None,
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
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if max_llm_calls is not None:
        payload["max_llm_calls"] = max_llm_calls
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds
    if model:
        payload["model"] = model
    if provider:
        payload["provider"] = provider

    if use_async:
        _query_async(service, headers, payload)
    else:
        _query_sync(service, headers, payload)


@click.command("run")
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
@click.option(
    "--model",
    type=str,
    default=None,
    help="LLM model override for this run (per-call, no profile change).",
)
@click.option(
    "--provider",
    type=str,
    default=None,
    help=(
        "LLM provider override for this run. 'openrouter' (default) or "
        "'tinfoil' (requires HIVEMIND_TINFOIL_API_KEY on the server)."
    ),
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
    model: str | None,
    provider: str | None,
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
    if model:
        form_data["model"] = model
    if provider:
        form_data["provider"] = provider

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


@click.command("runs")
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

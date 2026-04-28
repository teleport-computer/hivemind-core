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
    _query_tracked,
)


# Test-patchable HTTP trampolines. See owner.py for rationale.
def _hget(*a, **kw):
    from . import _hget as _f
    return _f(*a, **kw)


def _hpost(*a, **kw):
    from . import _hpost as _f
    return _f(*a, **kw)


def _upload_query_agent_and_poll(
    *,
    service: str,
    headers: dict,
    archive_bytes: bytes,
    archive_name: str,
    agent_name: str,
    description: str,
    prompt: str,
    scope_id: str | None,
    mediator_id: str | None,
    memory_mb: int,
    max_llm_calls: int,
    max_tokens: int,
    timeout: int,
    model: str | None,
    provider: str | None,
    as_json: bool,
    fetch: bool,
    expected_pubkey_b64: str | None = None,
    expected_compose_hash: str | None = None,
    strict_attestation: bool = True,
) -> None:
    """Upload an archive to /v1/query-agents/submit and poll until done.

    Shared by ``hivemind run`` and ``hivemind ask --query-agent``. Keeps
    the same exit-code contract as ``run_cmd`` (2=connect, 3=submit-4xx,
    4=run failed, 5=timeout).
    """
    form_data: dict[str, str] = {
        "name": agent_name,
        "prompt": prompt,
        "description": description,
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
        click.echo(f"Uploading {archive_name} ({len(archive_bytes)} bytes) → {service}")

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
            _emit_run_result(
                service, data, run_id,
                as_json=as_json,
                fetch=fetch,
                expected_pubkey_b64=expected_pubkey_b64,
                expected_compose_hash=expected_compose_hash,
                strict_attestation=strict_attestation,
            )
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


def _archive_for_path(path: Path, name: str | None) -> tuple[bytes, str, str]:
    """Resolve a CLI-provided ``path`` (dir or .tar.gz) into (bytes, archive_name, agent_name)."""
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
    return archive_bytes, archive_name, agent_name


@click.command()
@click.argument("uri")
@click.argument("question")
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
@click.option(
    "--query-agent",
    "query_agent_path",
    type=click.Path(exists=True, file_okay=True, dir_okay=True, path_type=Path),
    default=None,
    help=(
        "Upload your own query agent (directory or .tar.gz) and run the "
        "question through it instead of the owner's default. Requires "
        "the URI's token to have can_upload_query_agent=true. The owner-"
        "pinned scope agent still policies all SQL the code emits."
    ),
)
@click.option(
    "--memory-mb",
    type=int,
    default=256,
    show_default=True,
    help="Container memory limit (only used with --query-agent).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
@click.option(
    "--fetch",
    is_flag=True,
    help=(
        "Download artifacts into ./hivemind-artifacts/<run_id>/ "
        "(only used with --query-agent)."
    ),
)
@click.option(
    "--no-strict-attestation",
    "no_strict_attestation",
    is_flag=True,
    help=(
        "Print run output even if the CVM-signed attestation envelope "
        "is missing or invalid. Default: strict — abort with exit 6. "
        "Only useful for debugging against pre-Phase-5 servers."
    ),
)
def ask(
    uri: str,
    question: str,
    max_tokens: int | None,
    max_llm_calls: int | None,
    timeout_seconds: int | None,
    model: str | None,
    provider: str | None,
    query_agent_path: Path | None,
    memory_mb: int,
    as_json: bool,
    fetch: bool,
    no_strict_attestation: bool,
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

    pin_signer = parsed.get("signer_pubkey", "") or ""
    if pin_signer:
        # Pin-rotation URI: fetch the signed envelope, verify against
        # the pubkey baked into the URI, then enforce live compose ∈
        # allowed_composes and live attested-files digest equality.
        # The pubkey is the trust anchor — as long as the owner hasn't
        # rotated their hmk_ (which rotates the keypair), this URI keeps
        # working across any redeploy the owner has blessed.
        from base64 import b64decode as _b64d
        from urllib.parse import unquote as _unquote

        try:
            expected_pub = _b64d(_unquote(pin_signer).encode("ascii"))
        except Exception:
            click.echo(
                "Error: URI signer= is not valid base64.", err=True
            )
            raise SystemExit(1)
        try:
            r = _hget(
                f"{service}/v1/tenants/compose-pin",
                headers=headers,
                timeout=15,
            )
        except httpx.RequestError as e:
            click.echo(f"Error fetching compose pin: {e}", err=True)
            raise SystemExit(2)
        if r.status_code == 404:
            click.echo(
                "Error: service has no compose pin published. Owner "
                "must run 'hivemind compose bless'.",
                err=True,
            )
            raise SystemExit(3)
        if r.status_code >= 400:
            click.echo(
                f"Error {r.status_code} fetching compose pin: "
                f"{_api_error(r)}",
                err=True,
            )
            raise SystemExit(3)
        envelope_dict = (r.json().get("envelope") or {})
        from hivemind.compose_pin import ComposePin

        try:
            pin = ComposePin.model_validate(envelope_dict)
        except Exception as e:
            click.echo(f"Error: pin envelope malformed: {e}", err=True)
            raise SystemExit(3)
        if not pin.verify(expected_pubkey=expected_pub):
            click.echo(
                "Error: compose-pin signature does not verify against "
                "URI signer pubkey.\n"
                "Either the URI was tampered with, the operator served "
                "a forged pin, or the owner rotated their hmk_ (in "
                "which case ask for a fresh URI).",
                err=True,
            )
            raise SystemExit(4)
        if pin.is_expired():
            click.echo(
                f"Error: compose pin expired at exp={pin.exp}. Ask "
                f"the owner to re-bless.",
                err=True,
            )
            raise SystemExit(4)
        if pin.scope_agent_id != scope_id:
            click.echo(
                "Error: compose pin is for a different scope agent.\n"
                f"  URI scope:  {scope_id}\n"
                f"  Pin scope:  {pin.scope_agent_id}",
                err=True,
            )
            raise SystemExit(4)

        # Live compose must be one of the blessed values.
        try:
            ar = _hget(f"{service}/v1/attestation", timeout=15)
        except httpx.RequestError as e:
            click.echo(f"Error fetching attestation: {e}", err=True)
            raise SystemExit(2)
        if ar.status_code >= 400:
            click.echo(
                f"Error {ar.status_code} fetching attestation: "
                f"{_api_error(ar)}",
                err=True,
            )
            raise SystemExit(3)
        live_compose = (
            (ar.json().get("attestation") or {}).get("compose_hash") or ""
        ).lower()
        allowed = {c.lower() for c in (pin.allowed_composes or [])}
        if live_compose not in allowed:
            click.echo(
                "Error: live compose_hash is not in the pin's "
                "allowed_composes.\n"
                f"  Live:     {live_compose}\n"
                f"  Allowed:  {sorted(allowed)}\n"
                "The owner has not blessed this redeploy. Ask them "
                "to update the pin.",
                err=True,
            )
            raise SystemExit(4)

        # Live attested-files digest must match the pin.
        try:
            fr = _hget(
                f"{service}/v1/agents/{scope_id}/attest",
                headers=headers,
                timeout=30,
            )
        except httpx.RequestError as e:
            click.echo(f"Error fetching scope pins: {e}", err=True)
            raise SystemExit(2)
        if fr.status_code >= 400:
            click.echo(
                f"Error {fr.status_code} fetching scope pins: "
                f"{_api_error(fr)}",
                err=True,
            )
            raise SystemExit(3)
        body = fr.json()
        live_files = (
            body.get("attested_files_digest_sha256")
            or body.get("files_digest_sha256")
            or ""
        ).lower()
        if live_files != pin.attested_files_digest.lower():
            click.echo(
                "Error: attested_files_digest mismatch (pin vs live).\n"
                f"  Pin:   {pin.attested_files_digest}\n"
                f"  Live:  {live_files}",
                err=True,
            )
            raise SystemExit(4)
    elif parsed["compose_hash"]:
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

    if parsed["files_digest"] and not pin_signer:
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

    if query_agent_path is not None:
        # Phase 4: B-uploadable query agent. All trust pins above
        # (compose / files / pin envelope) have already been verified
        # against the URI; routing here just swaps the prompts-only
        # /v1/query/run/submit path for an upload+run cycle. The server
        # gates this on the token's can_upload_query_agent constraint.
        archive_bytes, archive_name, agent_name = _archive_for_path(
            query_agent_path, None
        )
        # Phase 5: pull the live run-signer pubkey + compose_hash from
        # /v1/attestation so we can verify the signed run record against
        # the same enclave the URI authorised. Strict-by-default; the
        # earlier compose/pin checks already established trust in this
        # bundle, so reading it again is just cache-friendly plumbing.
        expected_pubkey: str | None = None
        live_compose_hash: str | None = None
        try:
            ar = _hget(f"{service}/v1/attestation", timeout=15)
            ar_body = ar.json() if ar.status_code < 400 else {}
            att = (ar_body.get("attestation") or {})
            expected_pubkey = att.get("run_signer_pubkey_b64") or None
            live_compose_hash = (att.get("compose_hash") or "").lower() or None
        except httpx.RequestError:
            pass
        # Default the run-side timeout to the gateway-friendly 600s
        # unless the caller set --timeout (which means per-call cap).
        run_timeout = timeout_seconds or 600
        _upload_query_agent_and_poll(
            service=service,
            headers=headers,
            archive_bytes=archive_bytes,
            archive_name=archive_name,
            agent_name=agent_name,
            description=f"hivemind ask --query-agent {query_agent_path.name}",
            prompt=question,
            scope_id=scope_id,
            mediator_id=None,
            memory_mb=memory_mb,
            max_llm_calls=max_llm_calls or 20,
            max_tokens=max_tokens or 100_000,
            timeout=run_timeout,
            model=model,
            provider=provider,
            as_json=as_json,
            fetch=fetch,
            expected_pubkey_b64=expected_pubkey,
            expected_compose_hash=live_compose_hash,
            strict_attestation=not no_strict_attestation,
        )
        return

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

    # Phase 5: pull the live signer pubkey + compose_hash from
    # /v1/attestation so _emit_run_result can verify the signed run
    # body matches the same enclave the URI's pins authorised.
    expected_pubkey: str | None = None
    live_compose_hash: str | None = None
    try:
        ar = _hget(f"{service}/v1/attestation", timeout=15)
        ar_body = ar.json() if ar.status_code < 400 else {}
        att = (ar_body.get("attestation") or {})
        expected_pubkey = att.get("run_signer_pubkey_b64") or None
        live_compose_hash = (att.get("compose_hash") or "").lower() or None
    except httpx.RequestError:
        pass

    _query_tracked(
        service, headers, payload,
        expected_pubkey_b64=expected_pubkey,
        expected_compose_hash=live_compose_hash,
        strict_attestation=not no_strict_attestation,
        as_json=as_json,
        poll_seconds=timeout_seconds or 600,
    )


@click.command("query")
@click.argument("question")
@click.option("--endpoint", default=None, help="Override service URL")
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
@click.option(
    "--no-strict-attestation",
    "no_strict_attestation",
    is_flag=True,
    help=(
        "Print run output even if the CVM-signed attestation envelope "
        "is missing or invalid. Default: strict — abort with exit 6."
    ),
)
def query_cmd(
    question: str,
    endpoint: str | None,
    query_agent: str | None,
    max_tokens: int | None,
    max_llm_calls: int | None,
    timeout_seconds: int | None,
    model: str | None,
    provider: str | None,
    no_strict_attestation: bool,
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

    expected_pubkey: str | None = None
    live_compose_hash: str | None = None
    try:
        ar = _hget(f"{service}/v1/attestation", timeout=15)
        ar_body = ar.json() if ar.status_code < 400 else {}
        att = (ar_body.get("attestation") or {})
        expected_pubkey = att.get("run_signer_pubkey_b64") or None
        live_compose_hash = (att.get("compose_hash") or "").lower() or None
    except httpx.RequestError:
        pass

    _query_tracked(
        service, headers, payload,
        expected_pubkey_b64=expected_pubkey,
        expected_compose_hash=live_compose_hash,
        strict_attestation=not no_strict_attestation,
        poll_seconds=timeout_seconds or 600,
    )


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
@click.option(
    "--no-strict-attestation",
    "no_strict_attestation",
    is_flag=True,
    help=(
        "Print run output even if the CVM-signed attestation envelope "
        "is missing or invalid. Default: strict — abort with exit 6."
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
    no_strict_attestation: bool,
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

    archive_bytes, archive_name, agent_name = _archive_for_path(path, name)
    scope_id = scope_agent or config.get("scope_agent_id")
    mediator_id = mediator_agent or config.get("mediator_agent_id")

    if not as_json:
        click.echo(f"Packing {path} → {archive_name}")

    # Phase 5: pull pubkey + compose_hash from /v1/attestation so we can
    # verify the run's signed attestation envelope. Best-effort fetch —
    # if the bundle is unavailable we'll fall through to verify-without-
    # expected-pub, which still catches missing/forged signatures.
    expected_pubkey: str | None = None
    live_compose_hash: str | None = None
    try:
        ar = _hget(f"{service}/v1/attestation", timeout=15)
        ar_body = ar.json() if ar.status_code < 400 else {}
        att = (ar_body.get("attestation") or {})
        expected_pubkey = att.get("run_signer_pubkey_b64") or None
        live_compose_hash = (att.get("compose_hash") or "").lower() or None
    except httpx.RequestError:
        pass

    _upload_query_agent_and_poll(
        service=service,
        headers=headers,
        archive_bytes=archive_bytes,
        archive_name=archive_name,
        agent_name=agent_name,
        description=f"hivemind run {path.name}",
        prompt=prompt,
        scope_id=scope_id,
        mediator_id=mediator_id,
        memory_mb=memory_mb,
        max_llm_calls=max_llm_calls,
        max_tokens=max_tokens,
        timeout=timeout,
        model=model,
        provider=provider,
        as_json=as_json,
        fetch=fetch,
        expected_pubkey_b64=expected_pubkey,
        expected_compose_hash=live_compose_hash,
        strict_attestation=not no_strict_attestation,
    )


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

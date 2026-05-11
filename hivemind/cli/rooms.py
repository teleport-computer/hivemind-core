"""Room CLI: compact create / inspect / ask UX."""

from __future__ import annotations

import json as _json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import click
import httpx
import yaml

from ._config import _config_path, _headers, _load_config, _profile_name
from ._http import _api_error, _tarball_from_dir
from ._shared import _emit_run_result, _query_tracked
from ._trust import _require_trust
from ..rooms import verify_room_envelope


def _hget(*a, **kw):
    from . import _hget as _f
    return _f(*a, **kw)


def _hpost(*a, **kw):
    from . import _hpost as _f
    return _f(*a, **kw)


def _hdelete(*a, **kw):
    from . import _hdelete as _f
    return _f(*a, **kw)


_ROOM_ATTEST_TIMEOUT_SECONDS = 90
_ROOM_ATTEST_RETRY_DELAYS_SECONDS = (0.5, 2.0)
_ROOM_PIPELINE_STAGE_COUNT = 3
_ROOM_RUN_POLL_GRACE_SECONDS = 120


def _get_room_attest(service: str, room_id: str, headers: dict):
    """Fetch the signed room manifest, retrying slow reads only.

    This preflight is an idempotent GET and can be slow on production CVMs
    under attestation/tenant-thaw load. Do not retry HTTP status failures here;
    callers need the exact response for sealed-room handling.
    """
    url = f"{service}/v1/rooms/{room_id}/attest"
    attempts = len(_ROOM_ATTEST_RETRY_DELAYS_SECONDS) + 1
    last_exc: httpx.TimeoutException | None = None
    for attempt in range(attempts):
        try:
            return _hget(url, headers=headers, timeout=_ROOM_ATTEST_TIMEOUT_SECONDS)
        except httpx.TimeoutException as exc:
            last_exc = exc
            if attempt == attempts - 1:
                break
            time.sleep(_ROOM_ATTEST_RETRY_DELAYS_SECONDS[attempt])
    raise click.ClickException(
        f"room attestation fetch timed out after {attempts} attempts: {last_exc}"
    )


def _room_run_poll_seconds(timeout: int) -> int:
    """Local polling window for a scope/query/mediator room pipeline."""
    stage_timeout = max(1, int(timeout))
    return stage_timeout * _ROOM_PIPELINE_STAGE_COUNT + _ROOM_RUN_POLL_GRACE_SECONDS


def _parse_room_ref(
    ref: str,
    config: dict | None = None,
) -> tuple[str, str, dict, str | None]:
    """Return ``(service, room_id, headers, owner_pubkey)``."""
    if ref.startswith("hmroom://"):
        parsed = urlparse(ref)
        room_id = parsed.path.strip("/")
        qs = parse_qs(parsed.query)
        service = unquote((qs.get("service") or [""])[0]).rstrip("/")
        token = unquote((qs.get("token") or [""])[0])
        owner_pubkey = unquote((qs.get("owner_pubkey") or [""])[0]) or None
        if not room_id or not service or not token:
            raise click.ClickException("invalid hmroom link")
        if owner_pubkey is None:
            raise click.ClickException("invalid hmroom link: missing owner_pubkey")
        return service, room_id, {"Authorization": f"Bearer {token}"}, owner_pubkey

    cfg = config or _load_config()
    return cfg["service"], ref.strip(), _headers(cfg), None


def _active_profile_api_key() -> str | None:
    path = _config_path()
    if not path.exists():
        return None
    try:
        with open(path) as f:
            config = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise click.ClickException(f"corrupt active profile {path}: {e}")
    key = str(config.get("api_key") or "").strip()
    return key if key.startswith("hmk_") else None


def _thaw_active_profile_tenant(service: str) -> bool:
    api_key = _active_profile_api_key()
    if not api_key:
        return False
    try:
        resp = _hget(
            f"{service.rstrip('/')}/v1/health",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
    except httpx.HTTPError:
        return False
    return resp.status_code < 400


def _room_sealed_hint(detail: str) -> str:
    return (
        f"{detail}\n\n"
        "This room belongs to another tenant and that owner tenant is still "
        "sealed after a server restart. Ask the room owner to run an hmk_ "
        "request once, for example `hmctl room inspect \"$ROOM\"` with "
        "the owner profile, then recreate the room and share the new invite."
    )


def _fetch_verified_room(
    service: str,
    room_id: str,
    headers: dict,
    *,
    owner_pubkey_b64: str | None,
) -> dict:
    if owner_pubkey_b64:
        # hmroom links do not load a local profile, so they would skip
        # the normal profile-based attestation gate unless we run it
        # explicitly before sending the room token.
        _require_trust({"service": service})
    resp = _get_room_attest(service, room_id, headers)
    if resp.status_code >= 400:
        detail = _api_error(resp)
        room_tenant_sealed = (
            owner_pubkey_b64
            and resp.status_code == 503
            and "Tenant is sealed" in detail
        )
        if room_tenant_sealed and _thaw_active_profile_tenant(service):
            resp = _get_room_attest(service, room_id, headers)
            detail = _api_error(resp) if resp.status_code >= 400 else ""
        if resp.status_code >= 400:
            if room_tenant_sealed and "Tenant is sealed" in detail:
                detail = _room_sealed_hint(detail)
            raise click.ClickException(f"{resp.status_code}: {detail}")
    data = resp.json()
    envelope = ((data.get("room") or {}).get("envelope") or {})
    ok, reason = verify_room_envelope(
        envelope,
        expected_pubkey_b64=owner_pubkey_b64,
    )
    if not ok:
        raise click.ClickException(f"room manifest verification failed: {reason}")
    return data


def _live_compose_from_attestation(data: dict) -> str:
    return (
        ((data.get("attestation") or {}).get("attestation") or {}).get(
            "compose_hash"
        )
        or ""
    ).lower()


def _enforce_room_trust(room_attest: dict) -> None:
    manifest = (room_attest.get("room") or {}).get("manifest") or {}
    trust = manifest.get("trust") or {}
    mode = (trust.get("mode") or "operator_updates").strip()
    if mode == "operator_updates":
        return
    live = _live_compose_from_attestation(room_attest)
    allowed = {str(c).lower() for c in (trust.get("allowed_composes") or [])}
    if not live or live not in allowed:
        raise click.ClickException(
            "live compose_hash is not allowed by the room manifest. "
            f"mode={mode} live={live or '(missing)'} allowed={sorted(allowed)}"
        )


def _room_acceptances_path() -> Path:
    from . import _HIVEMIND_HOME

    return _HIVEMIND_HOME / "accepted-rooms.json"


def _room_acceptance_key(
    *,
    profile: str,
    service: str,
    room_id: str,
    manifest_hash: str,
    owner_pubkey_b64: str | None,
) -> str:
    return "|".join(
        [
            profile,
            service.rstrip("/"),
            room_id,
            owner_pubkey_b64 or "",
            manifest_hash,
        ]
    )


def _load_room_acceptances() -> dict:
    path = _room_acceptances_path()
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = _json.load(f)
    except (_json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_room_acceptance(
    *,
    service: str,
    room_id: str,
    room_data: dict,
    owner_pubkey_b64: str | None,
) -> None:
    room = room_data.get("room") or {}
    manifest = room.get("manifest") or {}
    manifest_hash = str(room.get("manifest_hash") or "")
    if not manifest_hash:
        raise click.ClickException("room manifest response did not include manifest_hash")
    profile = _profile_name()
    key = _room_acceptance_key(
        profile=profile,
        service=service,
        room_id=room_id,
        manifest_hash=manifest_hash,
        owner_pubkey_b64=owner_pubkey_b64,
    )
    path = _room_acceptances_path()
    accepted = _load_room_acceptances()
    accepted[key] = {
        "profile": profile,
        "service": service.rstrip("/"),
        "room_id": room_id,
        "name": manifest.get("name") or "",
        "manifest_hash": manifest_hash,
        "owner_pubkey_b64": owner_pubkey_b64 or "",
        "accepted_at": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        _json.dump(accepted, f, indent=2, sort_keys=True)
        f.write("\n")


def _room_manifest_is_accepted(
    *,
    service: str,
    room_id: str,
    room_data: dict,
    owner_pubkey_b64: str | None,
) -> bool:
    manifest_hash = str(((room_data.get("room") or {}).get("manifest_hash")) or "")
    if not manifest_hash:
        return False
    key = _room_acceptance_key(
        profile=_profile_name(),
        service=service,
        room_id=room_id,
        manifest_hash=manifest_hash,
        owner_pubkey_b64=owner_pubkey_b64,
    )
    return key in _load_room_acceptances()


def _room_manifest_summary(room_data: dict) -> list[str]:
    room = room_data.get("room") or {}
    manifest = room.get("manifest") or {}
    scope = manifest.get("scope") or {}
    query = manifest.get("query") or {}
    mediator = manifest.get("mediator") or {}
    output = manifest.get("output") or {}
    egress = manifest.get("egress") or {}
    trust = manifest.get("trust") or {}
    providers = egress.get("llm_providers") or []
    mediator_agent = mediator.get("agent_id") or "disabled"
    return [
        f"Room:     {manifest.get('room_id') or room.get('room_id') or '(unknown)'}",
        f"Name:     {manifest.get('name') or '(unnamed)'}",
        f"Hash:     {room.get('manifest_hash') or '(missing)'}",
        f"Scope:    {scope.get('agent_id') or '(missing)'} ({scope.get('visibility') or 'unknown'})",
        f"Query:    {query.get('mode') or 'unknown'} {query.get('agent_id') or ''} ({query.get('visibility') or 'unknown'})",
        f"Mediator: {mediator_agent}",
        f"Output:   {output.get('visibility') or 'unknown'}",
        "LLM:      " + (", ".join(str(p) for p in providers) or "disabled"),
        f"Trust:    {trust.get('mode') or 'operator_updates'}",
    ]


def _echo_room_manifest_summary(room_data: dict) -> None:
    for line in _room_manifest_summary(room_data):
        click.echo(line)


def _require_room_manifest_acceptance(
    *,
    service: str,
    room_id: str,
    room_data: dict,
    owner_pubkey_b64: str | None,
) -> None:
    if owner_pubkey_b64 is None:
        return
    if os.environ.get("HIVEMIND_NO_TRUST_CHECK"):
        return
    if _room_manifest_is_accepted(
        service=service,
        room_id=room_id,
        room_data=room_data,
        owner_pubkey_b64=owner_pubkey_b64,
    ):
        return

    click.echo("This room manifest has not been accepted for this profile.")
    click.echo("Review the signed room rules before sending prompts or data:")
    click.echo("  hmctl room inspect \"$ROOM\" --json | jq '.room.manifest'")
    click.echo("")
    _echo_room_manifest_summary(room_data)
    click.echo("")
    if not click.confirm(
        f"Accept this room manifest for profile '{_profile_name()}'?",
        default=False,
    ):
        raise click.ClickException(
            "room manifest not accepted. Run "
            "`hmctl room inspect \"$ROOM\" --json | jq '.room.manifest'` "
            "and `hmctl room accept \"$ROOM\"` before asking, or use "
            "--dangerously-skip-attestations only as an explicit risk "
            "acceptance bypass."
        )
    _save_room_acceptance(
        service=service,
        room_id=room_id,
        room_data=room_data,
        owner_pubkey_b64=owner_pubkey_b64,
    )
    click.echo(
        "Accepted room manifest "
        f"{(room_data.get('room') or {}).get('manifest_hash') or '(missing)'}."
    )


def _parse_meta(pairs: tuple[str, ...]) -> dict:
    out: dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            raise click.ClickException("--meta values must be key=value")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise click.ClickException("--meta key cannot be empty")
        out[key] = value
    return out


def _inspection_mode_from_visibility(value: str) -> str:
    return "sealed" if value == "sealed" else "full"


def _archive_for_path(path: Path, name: str | None = None) -> tuple[bytes, str, str]:
    if path.is_dir():
        return _tarball_from_dir(path), f"{path.name}.tar.gz", name or path.name
    if path.suffix in {".gz", ".tgz"} or path.name.endswith(".tar.gz"):
        agent_name = name or path.name.replace(".tar.gz", "").replace(".tgz", "")
        return path.read_bytes(), path.name, agent_name
    raise click.ClickException(f"Unsupported agent path: {path}. Use a directory or .tar.gz.")


def _maybe_upload_room_agent(
    *,
    service: str,
    headers: dict,
    ref: str,
    agent_type: str,
    visibility: str,
    private_paths: tuple[str, ...] = (),
    as_json: bool = False,
    timeout_seconds: int | None = None,
) -> str:
    path = Path(ref)
    if not path.exists():
        return ref.strip()

    archive_bytes, archive_name, agent_name = _archive_for_path(path)
    if not as_json:
        click.echo(f"Uploading {agent_type} agent {archive_name}...")
    upload_data: dict[str, str] = {
        "name": agent_name,
        "agent_type": agent_type,
        "description": f"hmctl room {agent_type} agent {path.name}",
        "private_paths": _json.dumps(list(private_paths)),
        "inspection_mode": _inspection_mode_from_visibility(visibility),
    }
    if timeout_seconds is not None:
        upload_data["timeout_seconds"] = str(timeout_seconds)
    resp = _hpost(
        f"{service}/v1/room-agents",
        files={"archive": (archive_name, archive_bytes, "application/gzip")},
        data=upload_data,
        headers=headers,
        timeout=60,
    )
    if resp.status_code >= 400:
        raise click.ClickException(f"{resp.status_code}: {_api_error(resp)}")
    data = resp.json()
    run_id = data.get("run_id")
    if run_id:
        deadline = time.time() + 180
        while time.time() < deadline:
            status_resp = _hget(f"{service}/v1/runs/{run_id}", headers=headers, timeout=10)
            if status_resp.status_code == 404:
                time.sleep(2)
                continue
            status = status_resp.json()
            state = status.get("status")
            if state == "completed":
                break
            if state == "failed":
                raise click.ClickException(f"agent build failed: {status.get('error', '?')}")
            time.sleep(2)
        else:
            raise click.ClickException(f"agent build timed out: run {run_id}")
    return data["agent_id"]


def _upload_room_query_agent_and_poll(
    *,
    service: str,
    headers: dict,
    room_id: str,
    archive_bytes: bytes,
    archive_name: str,
    agent_name: str,
    description: str,
    prompt: str,
    memory_mb: int,
    max_llm_calls: int,
    max_tokens: int,
    timeout: int,
    model: str | None,
    provider: str | None,
    as_json: bool,
    fetch: bool,
    expected_pubkey_b64: str | None,
    expected_compose_hash: str | None,
    expected_room_manifest_hash: str | None,
    strict_attestation: bool,
) -> None:
    form_data: dict[str, str] = {
        "name": agent_name,
        "prompt": prompt,
        "description": description,
        "memory_mb": str(memory_mb),
        "max_llm_calls": str(max_llm_calls),
        "max_tokens": str(max_tokens),
        "timeout_seconds": str(min(timeout, 3600)),
    }
    if model:
        form_data["model"] = model
    if provider:
        form_data["provider"] = provider

    resp = _hpost(
        f"{service}/v1/rooms/{room_id}/query-agents",
        files={"archive": (archive_name, archive_bytes, "application/gzip")},
        data=form_data,
        headers=headers,
        timeout=60,
    )
    if resp.status_code >= 400:
        raise click.ClickException(f"{resp.status_code}: {_api_error(resp)}")

    submission = resp.json()
    run_id = submission["run_id"]
    if not as_json:
        click.echo(f"Submitted: run_id={run_id} agent_id={submission.get('agent_id')}")

    poll_seconds = _room_run_poll_seconds(timeout)
    deadline = time.time() + poll_seconds
    last_status = ""
    while time.time() < deadline:
        sr = _hget(f"{service}/v1/runs/{run_id}", headers=headers, timeout=15)
        if sr.status_code == 404:
            time.sleep(2)
            continue
        if sr.status_code >= 500:
            time.sleep(2)
            continue
        if sr.status_code >= 400:
            raise click.ClickException(f"{sr.status_code}: {_api_error(sr)}")
        try:
            data = sr.json()
        except ValueError:
            if not (sr.text or "").strip():
                time.sleep(2)
                continue
            raise click.ClickException(
                "run status endpoint returned non-JSON response: "
                f"{sr.text[:200]}"
            )
        status = data.get("status", "")
        if status != last_status and not as_json:
            click.echo(f"  status: {status}")
            last_status = status
        if status == "completed":
            _emit_run_result(
                service,
                data,
                run_id,
                as_json=as_json,
                fetch=fetch,
                expected_pubkey_b64=expected_pubkey_b64,
                expected_compose_hash=expected_compose_hash,
                expected_room_id=room_id,
                expected_room_manifest_hash=expected_room_manifest_hash,
                strict_attestation=strict_attestation,
                fetch_headers=headers,
            )
            return
        if status == "failed":
            raise click.ClickException(f"run failed: {data.get('error') or '?'}")
        time.sleep(3)

    raise click.ClickException(f"timed out after {poll_seconds}s; run_id={run_id}")


@click.group("room")
def rooms_cli():
    """Create and use signed rooms."""


@rooms_cli.command("create")
@click.argument("scope")
@click.option("--name", default="", help="Human label for this room.")
@click.option("--rules", default="", help="Room rules as text.")
@click.option(
    "--rules-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="Read human room rules from a Markdown/plain-text file.",
)
@click.option(
    "--policy-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="Read the scope policy from a file.",
)
@click.option(
    "--scope-visibility",
    type=click.Choice(["sealed", "inspectable"]),
    default="inspectable",
    show_default=True,
    help=(
        "Scope source visibility when SCOPE is a local path. Existing "
        "agent ids use their registered inspection mode."
    ),
)
@click.option(
    "--scope-private",
    "scope_private_paths",
    multiple=True,
    help="Path inside uploaded scope archive to exclude from public digest.",
)
@click.option(
    "--query-agent",
    default=None,
    help=(
        "Fixed query agent id or local path. Omit to pin the service default "
        "query agent when configured."
    ),
)
@click.option(
    "--uploadable-query",
    is_flag=True,
    help="Allow recipient uploads instead of pinning the service default query.",
)
@click.option(
    "--query-private",
    "query_private_paths",
    multiple=True,
    help="Path inside uploaded fixed query archive to exclude from public digest.",
)
@click.option(
    "--query-visibility",
    type=click.Choice(["sealed", "inspectable"]),
    default="sealed",
    show_default=True,
    help=(
        "Query source and prompt-history visibility. Existing fixed "
        "agent ids use their registered inspection mode."
    ),
)
@click.option(
    "--mediator-agent",
    default=None,
    help=(
        "Pinned mediator agent id or local path. Omit to pin the service "
        "default mediator when configured."
    ),
)
@click.option(
    "--mediator-private",
    "mediator_private_paths",
    multiple=True,
    help="Path inside uploaded mediator archive to exclude from public digest.",
)
@click.option(
    "--mediator-visibility",
    type=click.Choice(["sealed", "inspectable"]),
    default="inspectable",
    show_default=True,
    help=(
        "Mediator source visibility when --mediator-agent is a local path. "
        "Existing agent ids use their registered inspection mode."
    ),
)
@click.option(
    "--output-visibility",
    type=click.Choice(["querier_only", "owner_and_querier"]),
    default="querier_only",
    show_default=True,
    help=(
        "Who can read final output/artifacts. querier_only hides "
        "participant-initiated results from the owner."
    ),
)
@click.option(
    "--llm-provider",
    "llm_providers",
    multiple=True,
    default=("openrouter",),
    help=(
        "Allowed LLM provider. Repeat for multiple. "
        "Dynamic rooms need at least one unless --no-llm is intentional."
    ),
)
@click.option(
    "--no-llm",
    is_flag=True,
    help="Disable bridge LLM egress for pinned non-LLM agents.",
)
@click.option(
    "--allow-artifacts/--no-artifacts",
    default=True,
    show_default=True,
    help="Allow query agents to upload generated artifacts.",
)
@click.option(
    "--allowed-table",
    "allowed_tables",
    multiple=True,
    help=(
        "Tenant table this room may query. Repeat for multiple tables. "
        "Omit for a room with no SQL data sources."
    ),
)
@click.option(
    "--trust-mode",
    type=click.Choice(["operator_updates", "pinned", "owner_approved"]),
    default="operator_updates",
    show_default=True,
    help=(
        "CVM update trust: operator governance, exact pinned hashes, "
        "or owner-managed room allowlist."
    ),
)
@click.option(
    "--agent-timeout",
    type=int,
    default=None,
    help=(
        "Per-agent run timeout in seconds, applied to scope/query/"
        "mediator uploads. Default 120s is fine for small deterministic "
        "agents but too short for the LLM-driven default-scope on cold "
        "paths — bump to 600 for those rooms."
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout.")
def create_room(
    scope: str,
    name: str,
    rules: str,
    rules_file: Path | None,
    policy_file: Path | None,
    scope_visibility: str,
    scope_private_paths: tuple[str, ...],
    query_agent: str | None,
    uploadable_query: bool,
    query_private_paths: tuple[str, ...],
    query_visibility: str,
    mediator_agent: str | None,
    mediator_private_paths: tuple[str, ...],
    mediator_visibility: str,
    output_visibility: str,
    llm_providers: tuple[str, ...],
    no_llm: bool,
    allow_artifacts: bool,
    allowed_tables: tuple[str, ...],
    trust_mode: str,
    agent_timeout: int | None,
    as_json: bool,
):
    """Create a room and print an invite link."""
    if query_agent and uploadable_query:
        raise click.ClickException(
            "--query-agent and --uploadable-query are mutually exclusive"
        )

    config = _load_config()
    service = config["service"]
    headers = _headers(config)

    if rules_file:
        rules = rules_file.read_text(encoding="utf-8")
    policy = policy_file.read_text(encoding="utf-8") if policy_file else None
    providers = [] if no_llm else list(llm_providers)
    scope_agent_id = _maybe_upload_room_agent(
        service=service,
        headers=headers,
        ref=scope,
        agent_type="scope",
        visibility=scope_visibility,
        private_paths=scope_private_paths,
        as_json=as_json,
        timeout_seconds=agent_timeout,
    )
    query_agent_id = None
    if query_agent:
        query_agent_id = _maybe_upload_room_agent(
            service=service,
            headers=headers,
            ref=query_agent,
            agent_type="query",
            visibility=query_visibility,
            private_paths=query_private_paths,
            as_json=as_json,
            timeout_seconds=agent_timeout,
        )
    mediator_agent_id = None
    if mediator_agent:
        mediator_agent_id = _maybe_upload_room_agent(
            service=service,
            headers=headers,
            ref=mediator_agent,
            agent_type="mediator",
            visibility=mediator_visibility,
            private_paths=mediator_private_paths,
            as_json=as_json,
            timeout_seconds=agent_timeout,
        )
    payload = {
        "name": name,
        "rules": rules,
        "policy": policy,
        "scope_agent_id": scope_agent_id,
        "query_visibility": query_visibility,
        "mediator_agent_id": mediator_agent_id,
        "mediator_visibility": mediator_visibility if mediator_agent_id else None,
        "output_visibility": output_visibility,
        "egress": {
            "llm_providers": providers,
            "allow_artifacts": allow_artifacts,
        },
        "allowed_tables": list(allowed_tables),
        "trust": {"mode": trust_mode},
    }
    if query_agent_id:
        payload["query_mode"] = "fixed"
        payload["query_agent_id"] = query_agent_id
    elif uploadable_query:
        payload["query_mode"] = "uploadable"
        payload["query_agent_id"] = None
    try:
        resp = _hpost(
            f"{service}/v1/rooms",
            headers=headers,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise click.ClickException(
            f"{e.response.status_code}: {_api_error(e.response)}"
        )
    data = resp.json()
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    click.echo(f"Room: {data['room_id']}")
    click.echo(f"Invite: {data['link']}")


@rooms_cli.command("inspect")
@click.argument("room")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout.")
def inspect_room(room: str, as_json: bool):
    """Show a room's signed manifest and live attestation summary.

    Use --json to print the full room spec; the signed manifest is at
    .room.manifest, for example:

        hmctl room inspect "$ROOM" --json | jq '.room.manifest'
    """
    service, room_id, headers, owner_pubkey = _parse_room_ref(room)
    data = _fetch_verified_room(
        service,
        room_id,
        headers,
        owner_pubkey_b64=owner_pubkey,
    )
    _enforce_room_trust(data)
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return

    manifest = data["room"]["manifest"]
    click.echo(f"Room:   {manifest['room_id']}")
    click.echo(f"Name:   {manifest.get('name') or '(unnamed)'}")
    click.echo(f"Scope:  {manifest['scope']['agent_id']} ({manifest['scope']['visibility']})")
    q = manifest["query"]
    click.echo(f"Query:  {q['mode']} {q.get('agent_id') or ''} ({q['visibility']})")
    m = manifest.get("mediator") or {}
    if m.get("agent_id"):
        click.echo(f"Mediator: {m['agent_id']} ({m.get('visibility') or 'unknown'})")
    else:
        click.echo("Mediator: disabled")
    click.echo(f"Output: {manifest['output']['visibility']}")
    click.echo(
        "LLM:    "
        + (", ".join(manifest["egress"]["llm_providers"]) or "disabled")
    )
    click.echo(f"Hash:   {data['room']['manifest_hash']}")
    click.echo(f"Trust:  {manifest['trust']['mode']}")
    click.echo("Sig:    verified")


@rooms_cli.command("accept")
@click.argument("room")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout.")
def accept_room(room: str, as_json: bool):
    """Accept a room manifest for this local profile before asking."""
    service, room_id, headers, owner_pubkey = _parse_room_ref(room)
    data = _fetch_verified_room(
        service,
        room_id,
        headers,
        owner_pubkey_b64=owner_pubkey,
    )
    _enforce_room_trust(data)
    _save_room_acceptance(
        service=service,
        room_id=room_id,
        room_data=data,
        owner_pubkey_b64=owner_pubkey,
    )
    if as_json:
        click.echo(
            _json.dumps(
                {
                    "accepted": True,
                    "profile": _profile_name(),
                    "room_id": room_id,
                    "manifest_hash": data["room"]["manifest_hash"],
                    "manifest": data["room"]["manifest"],
                },
                indent=2,
            )
        )
        return
    _echo_room_manifest_summary(data)
    click.echo(f"Accepted for profile '{_profile_name()}'.")


@rooms_cli.command("list")
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout.")
def list_rooms(limit: int, as_json: bool):
    """List rooms visible to the active profile."""
    config = _load_config()
    service = config["service"]
    resp = _hget(
        f"{service}/v1/rooms",
        headers=_headers(config),
        params={"limit": limit},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise click.ClickException(f"{resp.status_code}: {_api_error(resp)}")
    rooms = resp.json().get("rooms") or []
    if as_json:
        click.echo(_json.dumps(rooms, indent=2, default=str))
        return
    if not rooms:
        click.echo("(no rooms)")
        return
    click.echo(f"{'ROOM_ID':<18} {'STATUS':<8} {'QUERY':<8} NAME")
    for room in rooms:
        status = "revoked" if room.get("revoked_at") is not None else "active"
        click.echo(
            f"{room.get('room_id',''):<18} "
            f"{status:<8} "
            f"{room.get('query_mode',''):<8} "
            f"{room.get('name') or ''}"
        )


@rooms_cli.command("revoke")
@click.argument("room")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout.")
@click.confirmation_option(prompt="Revoke this room invite? Existing links stop working.")
def revoke_room(room: str, as_json: bool):
    """Revoke a room owned by the active profile."""
    config = _load_config()
    service, room_id, _room_headers, _owner_pubkey = _parse_room_ref(room, config=config)
    resp = _hdelete(
        f"{service}/v1/rooms/{room_id}",
        headers=_headers(config),
        timeout=30,
    )
    if resp.status_code >= 400:
        raise click.ClickException(f"{resp.status_code}: {_api_error(resp)}")
    data = resp.json()
    if as_json:
        click.echo(_json.dumps(data, indent=2, default=str))
        return
    click.echo(f"Revoked room {room_id}.")


@rooms_cli.command("prune")
@click.option("--limit", type=int, default=200, show_default=True)
@click.option(
    "--keep",
    "keeps",
    multiple=True,
    help="Room id or hmroom link to preserve. Repeat for multiple rooms.",
)
@click.option(
    "--name-prefix",
    default=None,
    help="Only consider active rooms whose names start with this prefix.",
)
@click.option(
    "--legacy-only",
    is_flag=True,
    help="Only consider active rooms whose manifest is missing allowed_tables.",
)
@click.option(
    "--all-active",
    is_flag=True,
    help="Allow considering all active rooms not listed in --keep.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    show_default=True,
    help="Preview candidates or actually revoke them.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout.")
def prune_rooms(
    limit: int,
    keeps: tuple[str, ...],
    name_prefix: str | None,
    legacy_only: bool,
    all_active: bool,
    dry_run: bool,
    as_json: bool,
):
    """Bulk-revoke old active room invites, dry-run first by default."""
    if not name_prefix and not legacy_only and not all_active:
        raise click.ClickException("pass --name-prefix, --legacy-only, or --all-active")

    config = _load_config()
    service = config["service"]
    keep_ids: set[str] = set()
    for keep in keeps:
        if keep.startswith("hmroom://"):
            _keep_service, keep_id, _headers_unused, _owner_pubkey = _parse_room_ref(
                keep,
                config=config,
            )
        else:
            keep_id = keep.strip()
        if keep_id:
            keep_ids.add(keep_id)

    resp = _hget(
        f"{service}/v1/rooms",
        headers=_headers(config),
        params={"limit": limit},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise click.ClickException(f"{resp.status_code}: {_api_error(resp)}")

    rooms = resp.json().get("rooms") or []
    candidates = []
    for room in rooms:
        room_id = str(room.get("room_id") or "")
        name = str(room.get("name") or "")
        if not room_id or room.get("revoked_at") is not None:
            continue
        if room_id in keep_ids:
            continue
        if name_prefix and not name.startswith(name_prefix):
            continue
        manifest = room.get("manifest") or {}
        is_legacy = (
            "allowed_tables" not in manifest
            or manifest.get("allowed_tables") is None
        )
        if legacy_only and not is_legacy:
            continue
        candidates.append(room)

    revoked: list[dict] = []
    errors: list[dict] = []
    if candidates and not dry_run:
        if not os.environ.get("HIVEMIND_TRUST_ALL"):
            click.confirm(
                f"Revoke {len(candidates)} active room invite"
                f"{'s' if len(candidates) != 1 else ''}?",
                abort=True,
            )
        for room in candidates:
            room_id = str(room.get("room_id") or "")
            delete_resp = _hdelete(
                f"{service}/v1/rooms/{room_id}",
                headers=_headers(config),
                timeout=30,
            )
            if delete_resp.status_code >= 400:
                errors.append(
                    {
                        "room_id": room_id,
                        "error": f"{delete_resp.status_code}: {_api_error(delete_resp)}",
                    }
                )
            else:
                revoked.append(delete_resp.json())

    out = {
        "dry_run": dry_run,
        "candidates": candidates,
        "revoked": revoked,
        "errors": errors,
    }
    if as_json:
        click.echo(_json.dumps(out, indent=2, default=str))
    else:
        verb = "Would revoke" if dry_run else "Revoked"
        click.echo(f"{verb}: {len(candidates) if dry_run else len(revoked)}")
        for room in candidates:
            click.echo(f"- {room.get('room_id')} {room.get('name') or ''}")
        for error in errors:
            click.echo(
                f"Error: {error['room_id']} {error['error']}",
                err=True,
            )
    if errors:
        raise SystemExit(1)


@rooms_cli.command("add-data")
@click.argument("room")
@click.argument("text", required=False)
@click.option(
    "--file",
    "file_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="Read vault item text from a file.",
)
@click.option(
    "--meta",
    "metadata_pairs",
    multiple=True,
    help="Metadata key=value. Repeat for multiple.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout.")
def add_room_data(
    room: str,
    text: str | None,
    file_path: Path | None,
    metadata_pairs: tuple[str, ...],
    as_json: bool,
):
    """Add owner data to the encrypted room."""
    if bool(text) == bool(file_path):
        raise click.ClickException("provide exactly one of TEXT or --file")
    service, room_id, headers, owner_pubkey = _parse_room_ref(room)
    data = _fetch_verified_room(
        service,
        room_id,
        headers,
        owner_pubkey_b64=owner_pubkey,
    )
    _enforce_room_trust(data)
    body_text = file_path.read_text(encoding="utf-8") if file_path else text or ""
    payload = {"text": body_text, "metadata": _parse_meta(metadata_pairs)}
    resp = _hpost(
        f"{service}/v1/rooms/{room_id}/data",
        headers=headers,
        json=payload,
        timeout=60,
    )
    if resp.status_code >= 400:
        raise click.ClickException(f"{resp.status_code}: {_api_error(resp)}")
    out = resp.json()
    if as_json:
        click.echo(_json.dumps(out, indent=2))
        return
    click.echo(f"Room: {room_id}")
    click.echo(f"Item: {out['item_id']}")


@rooms_cli.command("data")
@click.argument("room")
@click.option("--show-text", is_flag=True, help="Print plaintext item contents.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout.")
def list_room_data(room: str, show_text: bool, as_json: bool):
    """List owner-visible encrypted room data."""
    service, room_id, headers, owner_pubkey = _parse_room_ref(room)
    data = _fetch_verified_room(
        service,
        room_id,
        headers,
        owner_pubkey_b64=owner_pubkey,
    )
    _enforce_room_trust(data)
    resp = _hget(
        f"{service}/v1/rooms/{room_id}/data",
        headers=headers,
        timeout=60,
    )
    if resp.status_code >= 400:
        raise click.ClickException(f"{resp.status_code}: {_api_error(resp)}")
    out = resp.json()
    if as_json:
        click.echo(_json.dumps(out, indent=2))
        return
    items = out.get("items") or []
    click.echo(f"Room:  {room_id}")
    click.echo(f"Items: {len(items)}")
    for item in items:
        click.echo(
            f"- {item['item_id']} "
            f"({item.get('size_bytes', 0)} bytes) "
            f"{_json.dumps(item.get('metadata') or {}, sort_keys=True)}"
        )
        if show_text:
            click.echo(item.get("text") or "")


@rooms_cli.command("trust")
@click.argument("room")
@click.option(
    "--mode",
    type=click.Choice(["operator_updates", "pinned", "owner_approved"]),
    default=None,
    help="Set the room-level compose trust mode.",
)
@click.option(
    "--compose",
    "composes",
    multiple=True,
    help="Allowed compose hash. Repeat for multiple.",
)
@click.option(
    "--approve-live",
    is_flag=True,
    help="Append the service's current live compose_hash.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout.")
def trust_room(
    room: str,
    mode: str | None,
    composes: tuple[str, ...],
    approve_live: bool,
    as_json: bool,
):
    """Update a room's compose trust without changing invite links."""
    service, room_id, headers, _owner_pubkey = _parse_room_ref(room)
    payload = {"append_live": approve_live}
    if mode:
        payload["mode"] = mode
    if composes:
        payload["allowed_composes"] = list(composes)
    resp = _hpost(
        f"{service}/v1/rooms/{room_id}/trust",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if resp.status_code >= 400:
        raise click.ClickException(f"{resp.status_code}: {_api_error(resp)}")
    data = resp.json()
    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    trust = data["room"]["manifest"]["trust"]
    click.echo(f"Room:    {room_id}")
    click.echo(f"Mode:    {trust['mode']}")
    allowed = trust.get("allowed_composes") or []
    click.echo("Allowed: " + (", ".join(allowed) if allowed else "(none)"))
    click.echo(f"Hash:    {data['room']['manifest_hash']}")


@rooms_cli.command("ask")
@click.argument("room")
@click.argument("question")
@click.option("--query-agent", default=None, help="Query agent id for uploadable rooms.")
@click.option(
    "--agent",
    "agent_path",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help="Upload and run a query agent directory or .tar.gz in this room.",
)
@click.option(
    "--timeout",
    type=int,
    default=900,
    show_default=True,
    help=(
        "Per-stage sandbox timeout. Local polling waits for the full "
        "scope/query/mediator pipeline. CLI sends at most 3600; hosted "
        "services may clamp lower, e.g. 900s."
    ),
)
@click.option("--memory-mb", type=int, default=256, show_default=True)
@click.option(
    "--max-llm-calls",
    type=int,
    default=60,
    show_default=True,
    help="LLM call budget for scope/query/mediator; hosted cap is usually 100.",
)
@click.option(
    "--max-tokens",
    type=int,
    default=1_000_000,
    show_default=True,
    help=(
        "Token budget for scope/query/mediator. Hosted services reserve "
        "billing credit against this budget and may allow higher explicit values."
    ),
)
@click.option("--model", type=str, default=None, help="LLM model override for all roles.")
@click.option("--scope-model", type=str, default=None, help="Scope-stage model override.")
@click.option("--query-model", type=str, default=None, help="Query-stage model override.")
@click.option("--mediator-model", type=str, default=None, help="Mediator-stage model override.")
@click.option("--provider", type=str, default=None, help="LLM provider override.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout.")
@click.option("--fetch", is_flag=True, help="Download visible artifacts.")
@click.option(
    "--no-strict-attestation",
    "no_strict_attestation",
    is_flag=True,
    help="Print output even if run attestation is missing/invalid.",
)
def ask_room(
    room: str,
    question: str,
    query_agent: str | None,
    agent_path: Path | None,
    timeout: int,
    memory_mb: int,
    max_llm_calls: int,
    max_tokens: int,
    model: str | None,
    scope_model: str | None,
    query_model: str | None,
    mediator_model: str | None,
    provider: str | None,
    as_json: bool,
    fetch: bool,
    no_strict_attestation: bool,
):
    """Ask a question through a room invite.

    Defaults are tuned for dynamic scope/query/mediator rooms:
    --timeout 900, --max-llm-calls 60, --max-tokens 1000000.
    Invite-token room asks are billed to the active hmk_ tenant profile.
    """
    if query_agent and agent_path:
        raise click.ClickException("use either --query-agent id or --agent path, not both")
    service, room_id, headers, owner_pubkey = _parse_room_ref(room)
    profile_api_key = None
    if owner_pubkey is not None:
        profile_api_key = _active_profile_api_key()
        if not profile_api_key:
            raise click.ClickException(
                "room invite asks require an active tenant API key. "
                "Run `hmctl --profile NAME init --service URL "
                "--api-key hmk_...`, then `hmctl profile use NAME`, "
                "or pass `--profile NAME` before `room ask`."
            )
    if profile_api_key:
        headers = dict(headers)
        headers["X-Hivemind-Api-Key"] = profile_api_key
    room_data = _fetch_verified_room(
        service,
        room_id,
        headers,
        owner_pubkey_b64=owner_pubkey,
    )
    _enforce_room_trust(room_data)
    _require_room_manifest_acceptance(
        service=service,
        room_id=room_id,
        room_data=room_data,
        owner_pubkey_b64=owner_pubkey,
    )
    manifest_hash = room_data["room"]["manifest_hash"]
    payload = {"query": question}
    if query_agent:
        payload["query_agent_id"] = query_agent
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if max_llm_calls:
        payload["max_llm_calls"] = max_llm_calls
    if timeout:
        payload["timeout_seconds"] = min(timeout, 3600)
    if model:
        payload["model"] = model
    if scope_model:
        payload["scope_model"] = scope_model
    if query_model:
        payload["query_model"] = query_model
    if mediator_model:
        payload["mediator_model"] = mediator_model
    if provider:
        payload["provider"] = provider

    expected_pubkey = None
    live_compose_hash = None
    try:
        ar = _hget(f"{service}/v1/attestation", timeout=15)
        body = ar.json() if ar.status_code < 400 else {}
        att = body.get("attestation") or {}
        expected_pubkey = att.get("run_signer_pubkey_b64") or None
        live_compose_hash = (att.get("compose_hash") or "").lower() or None
    except httpx.RequestError:
        pass

    if agent_path is not None:
        archive_bytes, archive_name, agent_name = _archive_for_path(agent_path, None)
        _upload_room_query_agent_and_poll(
            service=service,
            headers=headers,
            room_id=room_id,
            archive_bytes=archive_bytes,
            archive_name=archive_name,
            agent_name=agent_name,
            description=f"hmctl room ask --agent {agent_path.name}",
            prompt=question,
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
            expected_room_manifest_hash=manifest_hash,
            strict_attestation=not no_strict_attestation,
        )
        return

    _query_tracked(
        service,
        headers,
        payload,
        submit_path=f"/v1/rooms/{room_id}/runs",
        expected_pubkey_b64=expected_pubkey,
        expected_compose_hash=live_compose_hash,
        expected_room_id=room_id,
        expected_room_manifest_hash=manifest_hash,
        strict_attestation=not no_strict_attestation,
        as_json=as_json,
        fetch=fetch,
        fetch_headers=headers,
        poll_seconds=_room_run_poll_seconds(timeout),
    )


@rooms_cli.command("runs")
@click.argument("run_id", required=False)
@click.option("--limit", type=int, default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout.")
def room_runs(run_id: str | None, limit: int, as_json: bool):
    """Show room run status."""
    config = _load_config()
    service = config["service"]
    headers = _headers(config)
    url = f"{service}/v1/runs/{run_id}" if run_id else f"{service}/v1/runs?limit={limit}"
    resp = _hget(url, headers=headers, timeout=30)
    if resp.status_code >= 400:
        raise click.ClickException(f"{resp.status_code}: {_api_error(resp)}")
    data = resp.json()
    if as_json or run_id:
        click.echo(_json.dumps(data, indent=2, default=str))
        return
    if not data:
        click.echo("(no runs)")
        return
    click.echo(f"{'RUN_ID':<14} {'STATUS':<10} {'ROOM':<18} OUTPUT")
    for row in data:
        output = (row.get("output") or "").replace("\n", " ")
        if len(output) > 80:
            output = output[:77] + "..."
        # `room_id`/`run_id`/`status` can be None on partial rows. Default
        # `dict.get('k', default)` returns the stored value (None) over the
        # default whenever the key exists, so we coerce explicitly. Without
        # this, `f"{None:<18}"` raises TypeError mid-listing.
        run_id_s = row.get("run_id") or "?"
        status_s = row.get("status") or "?"
        room_id_s = row.get("room_id") or ""
        click.echo(
            f"{run_id_s:<14} {status_s:<10} {room_id_s:<18} {output}"
        )

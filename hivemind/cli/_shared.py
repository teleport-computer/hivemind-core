"""Helpers shared across multiple command modules."""

import hashlib as _hashlib
import json as _json
import time
from pathlib import Path

import click
import httpx

from .. import reproduce as _reproduce
from ._config import _DEFAULT_PROFILE  # noqa: F401  (re-export hook)
from ._config import (
    _config_path,
    _headers,
    _load_config,
    _profile_name,
)
from ._http import (
    _api_error,
    _warm_pin_from_trust,
)
from ._trust import _release_metadata_for

_DEFAULT_SERVICE = "http://localhost:8100"


# Test-patchable HTTP trampolines (see owner.py for rationale).
def _hget(*a, **kw):
    from . import _hget as _f
    return _f(*a, **kw)


def _hpost(*a, **kw):
    from . import _hpost as _f
    return _f(*a, **kw)


# ── ask / query helpers ──


def _query_sync(service: str, headers: dict, payload: dict) -> None:
    try:
        resp = _hpost(
            f"{service}/v1/query",
            json=payload,
            headers=headers,
            timeout=120,
        )
        resp.raise_for_status()
    except httpx.ConnectError:
        click.echo(f"Error: Cannot reach {service}", err=True)
        raise SystemExit(1)
    except httpx.HTTPStatusError as e:
        click.echo(
            f"Error: {e.response.status_code}: {_api_error(e.response)}",
            err=True,
        )
        raise SystemExit(1)
    except httpx.TimeoutException:
        click.echo(
            "Error: Query timed out. Try --async for long-running queries.",
            err=True,
        )
        raise SystemExit(1)

    result = resp.json()
    click.echo(result.get("output", "No output"))
    if result.get("mediated"):
        click.echo("\n(mediated)", err=True)


def _query_async(service: str, headers: dict, payload: dict) -> None:
    try:
        resp = _hpost(
            f"{service}/v1/query/submit",
            json=payload,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    run_id = resp.json().get("run_id")
    click.echo(f"Submitted (run: {run_id}). Polling...")

    for _ in range(120):
        time.sleep(2)
        try:
            sr = _hget(
                f"{service}/v1/query/runs/{run_id}",
                headers=headers,
                timeout=10,
            )
            data = sr.json()
            status = data.get("status", "")
            if status == "completed":
                result = data.get("result", {})
                click.echo(result.get("output", "No output"))
                if result.get("mediated"):
                    click.echo("\n(mediated)", err=True)
                return
            if status == "failed":
                click.echo(f"Error: {data.get('error', '?')}", err=True)
                raise SystemExit(1)
        except httpx.RequestError:
            pass

    click.echo("Error: Query did not complete within timeout.", err=True)
    raise SystemExit(1)


# ── Approach A: run / runs / agents ──


def _artifact_url(service: str, run_id: str, filename: str) -> str:
    return f"{service}/v1/query/runs/{run_id}/artifacts/{filename}"


def _emit_run_result(
    service: str,
    data: dict,
    run_id: str,
    *,
    as_json: bool,
    fetch: bool,
) -> None:
    # Server returns these as top-level columns from the runs table
    # (see hivemind/sandbox/run_store.py). The legacy ``result.output``
    # nesting never existed in this code path; reading it always
    # returned None and printed "(empty)" even when the agent succeeded.
    output = data.get("output") or ""
    index_output = data.get("index_output") or ""
    mediated = data.get("mediated")  # reserved for future mediator runs
    artifacts = data.get("artifacts", []) or []

    artifact_urls = [
        {
            "filename": a["filename"],
            "size": a.get("size"),
            "content_type": a.get("content_type"),
            "url": _artifact_url(service, run_id, a["filename"]),
        }
        for a in artifacts
    ]

    fetched: list[dict] = []
    if fetch and artifacts:
        out_dir = Path("hivemind-artifacts") / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        config = _load_config()
        headers = _headers(config)
        for a in artifacts:
            fname = a["filename"]
            try:
                r = _hget(
                    _artifact_url(service, run_id, fname),
                    headers=headers,
                    timeout=60,
                )
                r.raise_for_status()
                dest = out_dir / fname
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(r.content)
                fetched.append({"filename": fname, "path": str(dest)})
            except httpx.HTTPError as e:
                if not as_json:
                    click.echo(
                        f"  warn: failed to fetch {fname}: {e}", err=True
                    )

    if as_json:
        click.echo(
            _json.dumps(
                {
                    "status": "completed",
                    "run_id": run_id,
                    "output": output,
                    "mediated": mediated,
                    "artifacts": artifact_urls,
                    "fetched": fetched,
                },
                indent=2,
            )
        )
        return

    click.echo("")
    click.echo("── Output ──")
    click.echo(output or "(empty)")
    if index_output:
        click.echo("")
        click.echo("── Index agent output ──")
        click.echo(index_output)
    if mediated:
        click.echo(f"\n(mediated: {mediated})", err=True)
    if artifact_urls:
        click.echo("\n── Artifacts ──")
        for a in artifact_urls:
            size = f" {a['size']}B" if a.get("size") else ""
            click.echo(f"  {a['filename']}{size}  →  {a['url']}")
    if fetched:
        click.echo("\nFetched to ./hivemind-artifacts/{}/".format(run_id))
        for f in fetched:
            click.echo(f"  {f['path']}")


def _parse_hmq_uri(uri: str) -> dict:
    """Decompose a ``hmq://host/<scope_id>?token=...&compose=...&files=...`` URI.

    Returns a dict with ``service``, ``scope_agent_id``, ``token``,
    ``compose_hash`` (may be empty), ``files_digest`` (may be empty).
    Aborts the CLI on malformed input.
    """
    if not uri.startswith("hmq://"):
        click.echo("Error: URI must start with hmq://", err=True)
        raise SystemExit(1)
    rest = uri[len("hmq://"):]
    if "/" not in rest:
        click.echo(
            "Error: URI is missing /<scope_agent_id> component.", err=True
        )
        raise SystemExit(1)
    host_part, _, tail = rest.partition("/")
    if "?" in tail:
        scope_id, _, query_str = tail.partition("?")
    else:
        scope_id, query_str = tail, ""
    if not scope_id:
        click.echo("Error: URI is missing scope_agent_id.", err=True)
        raise SystemExit(1)
    params: dict[str, str] = {}
    for kv in query_str.split("&"):
        if not kv:
            continue
        k, _, v = kv.partition("=")
        params[k] = v
    token = params.get("token", "")
    if not token.startswith("hmq_"):
        click.echo(
            "Error: URI is missing token=hmq_... or token has wrong prefix.",
            err=True,
        )
        raise SystemExit(1)
    scheme = params.get("scheme", "https")
    return {
        "service": f"{scheme}://{host_part}",
        "scope_agent_id": scope_id,
        "token": token,
        "compose_hash": params.get("compose", "").lower(),
        "files_digest": params.get("files", "").lower(),
        "query_agent_id": params.get("qa", ""),
    }


# ── load helpers ──


def _split_sql_statements(sql: str) -> list[str]:
    """Naive SQL splitter: handles ';' terminators, single-quoted strings,
    $$-delimited bodies, and -- line comments. Good enough for pg_dump-style
    output."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_single = False
    in_dollar = False
    dollar_tag = ""
    while i < n:
        c = sql[i]
        if not in_single and not in_dollar and c == "-" and i + 1 < n and sql[i + 1] == "-":
            nl = sql.find("\n", i)
            if nl < 0:
                break
            i = nl + 1
            continue
        if not in_dollar and c == "'":
            in_single = not in_single
            buf.append(c)
            i += 1
            continue
        if not in_single and c == "$":
            end = sql.find("$", i + 1)
            if end > i:
                tag = sql[i : end + 1]
                if in_dollar and tag == dollar_tag:
                    buf.append(tag)
                    i = end + 1
                    in_dollar = False
                    dollar_tag = ""
                    continue
                if not in_dollar:
                    buf.append(tag)
                    i = end + 1
                    in_dollar = True
                    dollar_tag = tag
                    continue
        if c == ";" and not in_single and not in_dollar:
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _batch_insert(
    post_fn,
    table: str,
    cols: list[str],
    rows: list[list],
    batch_size: int,
) -> None:
    """Multi-row INSERT: `INSERT INTO t (c1,c2) VALUES (%s,%s),(%s,%s),...`."""
    if not rows:
        return
    col_list = ", ".join(f'"{c}"' for c in cols)
    row_tpl = "(" + ", ".join(["%s"] * len(cols)) + ")"
    total = len(rows)
    with click.progressbar(length=total, label="insert") as bar:
        for start in range(0, total, batch_size):
            chunk = rows[start : start + batch_size]
            placeholders = ", ".join([row_tpl] * len(chunk))
            sql = f'INSERT INTO "{table}" ({col_list}) VALUES {placeholders}'
            params: list = []
            for r in chunk:
                params.extend(r)
            post_fn(sql, params)
            bar.update(len(chunk))


# ── trust attest --reproduce ──


def _run_reproduce(bundle: dict) -> None:
    """Walk the full chain of trust and print which links held.

    Steps:
      1. ``app_compose`` (live, from the dstack 8090 page) → ``compose_hash``.
         ``sha256(app_compose_str)`` IS the compose_hash by construction,
         so this is cryptographic and self-verifying.
      2. On-chain registry → ``(git_commit, compose_uri)`` for that hash.
      3. GitHub raw at the registered ``git_commit`` → repo YAML.
      4. Byte-compare repo YAML vs ``docker_compose_file`` from app_compose.
      5. Image refs from the YAML (informational; ``ghcr.io/.../<sha>``
         tags can be cross-checked against the build-images workflow).

    Each step prints "✓" on pass, "✗" on fail, "·" on skip with a
    one-line reason. Returns silently after all steps.
    """
    if not bundle.get("ready"):
        click.echo(
            "Error: attestation bundle is not ready — "
            f"reason: {bundle.get('reason', '?')}",
            err=True,
        )
        raise SystemExit(2)
    att = bundle.get("attestation") or {}
    compose_hash = (att.get("compose_hash") or "").lower()
    app_id = att.get("app_id") or ""
    pin_url = ((att.get("tls") or {}).get("pinning_url") or "").strip()
    gateway = (
        _reproduce.gateway_from_pinning_url(pin_url)
        if pin_url
        else "dstack-pha-prod9.phala.network"
    )

    click.echo(f"Compose hash:  {compose_hash}")
    click.echo(f"App ID:        {app_id}")
    click.echo(f"Gateway:       {gateway}")
    click.echo("")

    # Step 1 — live app_compose from the dstack 8090 page.
    click.echo("[1/4] Fetching live app_compose from dstack tcb-info page…")
    try:
        tcb = _reproduce.fetch_tcb_info(app_id, gateway)
    except (httpx.HTTPError, ValueError) as e:
        click.echo(f"      ✗ failed: {e}", err=True)
        raise SystemExit(3)
    app_compose_str = tcb.get("app_compose") or ""
    claimed_hash = (tcb.get("compose_hash") or "").lower()
    computed = _reproduce.verify_app_compose_hash(app_compose_str, claimed_hash)
    if computed != compose_hash:
        click.echo(
            f"      ✗ sha256(app_compose) != attested compose_hash\n"
            f"        attested: {compose_hash}\n"
            f"        computed: {computed}",
            err=True,
        )
        raise SystemExit(4)
    if claimed_hash and claimed_hash != compose_hash:
        click.echo(
            f"      ✗ tcb-info claims a different hash ({claimed_hash}) "
            f"than /v1/attestation ({compose_hash})",
            err=True,
        )
        raise SystemExit(4)
    click.echo(
        f"      ✓ sha256(app_compose) == compose_hash "
        f"({len(app_compose_str)} bytes)"
    )

    # Step 2 — on-chain (git_commit, compose_uri).
    click.echo("[2/4] Reading on-chain registry for source pointer…")
    meta = _release_metadata_for(bundle, compose_hash)
    if not meta:
        click.echo(
            "      · skipped: registry not configured or RPC unreachable",
            err=True,
        )
        click.echo("")
        click.echo(
            "Partial: live app_compose verified, but no on-chain source "
            "pointer to compare against."
        )
        return
    git_commit = meta.get("git_commit") or ""
    compose_uri = meta.get("compose_uri") or ""
    click.echo(f"      ✓ git_commit:  {git_commit}")
    click.echo(f"      ✓ compose URI: {compose_uri}")

    # Step 3 — fetch the repo YAML at the registered ref.
    click.echo("[3/4] Fetching repo YAML at registered ref from GitHub…")
    raw_url = _reproduce.blob_to_raw(compose_uri)
    if not raw_url:
        click.echo(
            f"      · skipped: cannot derive raw URL from {compose_uri}",
            err=True,
        )
        click.echo("")
        click.echo(
            "Partial: source pointer recovered but URL shape isn't a "
            "GitHub blob — eyeball the YAML against app_compose by hand."
        )
        return
    try:
        repo_yaml = _reproduce.fetch_repo_yaml(compose_uri)
    except (httpx.HTTPError, ValueError) as e:
        click.echo(f"      ✗ failed: {e}", err=True)
        raise SystemExit(5)
    click.echo(f"      ✓ {len(repo_yaml)} bytes from {raw_url}")

    # Step 4 — byte-compare repo YAML vs the docker_compose_file embedded
    # in the verified app_compose.
    click.echo(
        "[4/4] Comparing repo YAML to docker_compose_file in app_compose…"
    )
    try:
        ac = _reproduce.parse_app_compose(app_compose_str)
    except _json.JSONDecodeError as e:
        click.echo(f"      ✗ app_compose is not valid JSON: {e}", err=True)
        raise SystemExit(6)
    deployed_yaml = ac.get("docker_compose_file") or ""
    yaml_match = deployed_yaml == repo_yaml
    if yaml_match:
        click.echo(
            f"      ✓ byte-identical "
            f"(sha256: {_hashlib.sha256(repo_yaml.encode()).hexdigest()[:16]}…)"
        )
    else:
        deployed_h = _hashlib.sha256(deployed_yaml.encode()).hexdigest()
        repo_h = _hashlib.sha256(repo_yaml.encode()).hexdigest()
        click.echo(
            f"      ✗ YAML differs\n"
            f"        deployed sha256: {deployed_h}\n"
            f"        repo    sha256: {repo_h}",
            err=True,
        )

    # Image references (always shown — useful for human cross-check
    # against the build-images CI workflow regardless of YAML match).
    refs = _reproduce.extract_image_refs(deployed_yaml)
    if refs:
        click.echo("")
        click.echo("Live image references (deployed):")
        for ref in refs:
            click.echo(f"  · {ref}")
        click.echo(
            "  (Tags ending in a 7-char hex are short git SHAs from "
            "build-images CI — verify they match a commit on the "
            "registered ref.)"
        )

    click.echo("")
    if yaml_match:
        click.echo(
            "✓ Full chain verified: the docker-compose YAML running in "
            "the enclave is byte-identical to the one at "
            f"{_reproduce.short_source(git_commit, compose_uri)}."
        )
    else:
        click.echo(
            "✗ Chain broken at step 4: the docker-compose YAML running "
            "in the enclave does NOT match the one at the on-chain-"
            "registered ref. Either the registered git_commit/URI is "
            "stale (e.g. the reconcile workflow recorded `main` instead "
            "of the deploy SHA) or someone deployed code that wasn't "
            "registered. Inspect `live image references` above and "
            f"compare against the YAML at {raw_url}."
        )
        raise SystemExit(7)


# ── admin helpers ──


def _admin_headers(admin_key: str) -> dict:
    return {
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }


def _resolve_admin_key(admin_key: str) -> str:
    """Resolve the admin bearer.

    Order of preference:
    1. ``--admin-key`` flag (Click also fills this from
       ``HIVEMIND_ADMIN_KEY`` env via the option's ``envvar=``).
    2. The active profile's ``api_key`` IF it was registered with
       ``role: admin`` (set by ``hivemind init`` when /v1/health 401s
       and /v1/admin/tenants accepts the key).
    Otherwise, abort. We never silently use a tenant key for admin
    operations.
    """
    if admin_key:
        return admin_key
    try:
        cfg = _load_config(check_trust=False)
    except SystemExit:
        cfg = {}
    if cfg.get("role") == "admin" and cfg.get("api_key"):
        return cfg["api_key"]
    click.echo(
        "Error: admin key required. Pass --admin-key, set "
        "HIVEMIND_ADMIN_KEY, or 'hivemind --profile <admin> init "
        "--api-key <admin-key>' to wire up an admin profile.",
        err=True,
    )
    raise SystemExit(2)


def _resolve_admin_service(service: str | None) -> str:
    if service:
        url = service.rstrip("/")
    else:
        # Fall back to the regular init config (admins often manage locally).
        try:
            url = _load_config()["service"]
        except SystemExit:
            url = _DEFAULT_SERVICE
    # Admin commands don't go through ``_require_trust``, so pin the
    # enclave cert from the trust store here. Without this, every
    # ``hivemind admin *`` against an -8100s. URL fails the self-signed
    # handshake and exits before reaching the server.
    _warm_pin_from_trust(url)
    return url

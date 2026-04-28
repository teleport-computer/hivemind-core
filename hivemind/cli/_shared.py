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


# ── Phase 5: signed run record verification ──


def _verify_run_attestation(
    data: dict,
    *,
    expected_pubkey_b64: str | None = None,
    expected_compose_hash: str | None = None,
    expected_output: str | None = None,
) -> tuple[bool, str]:
    """Verify a run-row's ``attestation`` envelope.

    Returns ``(ok, reason)``. ``ok=True`` means the signature checked out
    AND every supplied expected_* value matched the signed body. Each
    expected_* arg is optional; ``None`` skips that check.

    The envelope shape is set by Pipeline._build_run_attestation:
    ``{body, signature_b64, signer_pubkey_b64}``. ``body`` is the
    canonical-JSON-signed surface; we re-canonicalize and verify with
    Ed25519. The recipient is expected to have already pinned the
    expected pubkey out of band (from ``/v1/attestation`` over the
    enclave-pinned TLS channel).
    """
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    env = data.get("attestation")
    if not isinstance(env, dict):
        return False, "no attestation envelope on run record"

    body = env.get("body")
    sig_b64 = env.get("signature_b64", "")
    pub_b64 = env.get("signer_pubkey_b64", "")
    if not isinstance(body, dict) or not sig_b64 or not pub_b64:
        return False, "envelope is missing body / signature / pubkey"

    if expected_pubkey_b64 and pub_b64 != expected_pubkey_b64:
        return (
            False,
            "signer pubkey on run record does not match the pubkey "
            "published by /v1/attestation",
        )

    if expected_compose_hash and body.get("compose_hash") != expected_compose_hash:
        return (
            False,
            "signed body's compose_hash does not match the bundle's "
            "compose_hash — different CVM than expected",
        )

    if expected_output is not None:
        # Re-derive sha256 over the output we received and compare. The
        # body commits to the hash, not the bytes — keeps the signed
        # payload small and stable.
        h = _hashlib.sha256(
            (expected_output or "").encode("utf-8", errors="replace")
        ).hexdigest()
        if body.get("output_hash") != h:
            return (
                False,
                "signed output_hash does not match the output we "
                "received — server returned tampered output",
            )

    try:
        body_bytes = _json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        sig = base64.b64decode(sig_b64)
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
        pub.verify(sig, body_bytes)
    except Exception as e:
        return False, f"Ed25519 signature did not verify: {e}"
    return True, "ok"


# ── ask / query helpers ──


def _query_tracked(
    service: str,
    headers: dict,
    payload: dict,
    *,
    expected_pubkey_b64: str | None = None,
    expected_compose_hash: str | None = None,
    strict_attestation: bool = True,
    as_json: bool = False,
    fetch: bool = False,
    poll_seconds: int = 600,
) -> None:
    """Submit a query, poll the run row, verify the Phase 5 envelope.

    POSTs to ``/v1/query/run/submit`` (the only async query path — the
    legacy in-memory ``/v1/query/submit`` was removed because it never
    produced a signed envelope and so silently bypassed strict-default
    attestation). Polls ``/v1/agent-runs/{run_id}`` until the row
    reaches ``completed`` or ``failed``, then hands off to
    ``_emit_run_result`` which Ed25519-verifies the signed body and
    pubkey-matches it against ``expected_pubkey_b64`` (sourced from
    ``/v1/attestation`` on the recipient side).
    """
    try:
        resp = _hpost(
            f"{service}/v1/query/run/submit",
            json=payload,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        click.echo(
            f"Error: {e.response.status_code}: {_api_error(e.response)}",
            err=True,
        )
        raise SystemExit(1)
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    run_id = resp.json().get("run_id")
    if not as_json:
        click.echo(f"Submitted (run: {run_id}). Polling...")

    deadline = time.monotonic() + poll_seconds
    last_status = ""
    while time.monotonic() < deadline:
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
            err = data.get("error") or "?"
            if as_json:
                click.echo(
                    _json.dumps(
                        {"status": "failed", "error": err, "run_id": run_id}
                    )
                )
            else:
                click.echo(f"Error: run failed: {err}", err=True)
            raise SystemExit(4)

        time.sleep(3)

    if as_json:
        click.echo(_json.dumps({"status": "timeout", "run_id": run_id}))
    else:
        click.echo(
            f"Error: timed out after {poll_seconds}s. "
            f"Check `hivemind runs {run_id}`.",
            err=True,
        )
    raise SystemExit(5)


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
    expected_pubkey_b64: str | None = None,
    expected_compose_hash: str | None = None,
    strict_attestation: bool = True,
) -> None:
    # Server returns these as top-level columns from the runs table
    # (see hivemind/sandbox/run_store.py). The legacy ``result.output``
    # nesting never existed in this code path; reading it always
    # returned None and printed "(empty)" even when the agent succeeded.
    output = data.get("output") or ""
    index_output = data.get("index_output") or ""
    mediated = data.get("mediated")  # reserved for future mediator runs
    artifacts = data.get("artifacts", []) or []

    # Phase 5: verify the CVM-signed run attestation. Strict by default —
    # an unsigned or tampered run record fails closed. The recipient's
    # ``ask`` flow passes ``expected_pubkey_b64`` from the live
    # /v1/attestation bundle so an attacker swapping the pubkey on the
    # run row gets caught here.
    att_ok, att_reason = _verify_run_attestation(
        data,
        expected_pubkey_b64=expected_pubkey_b64,
        expected_compose_hash=expected_compose_hash,
        expected_output=output,
    )

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

    # In strict mode, refuse to print a tampered or unsigned run.
    if strict_attestation and not att_ok:
        if as_json:
            click.echo(
                _json.dumps(
                    {
                        "status": "attestation_failed",
                        "run_id": run_id,
                        "reason": att_reason,
                    },
                    indent=2,
                )
            )
        else:
            click.echo(
                f"Error: run attestation failed: {att_reason}\n"
                "  Pass --no-strict-attestation to print the output anyway "
                "(NOT recommended — you'd be trusting a record the CVM "
                "didn't sign).",
                err=True,
            )
        raise SystemExit(6)

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
                    "attestation_ok": att_ok,
                    "attestation_reason": att_reason,
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
    if att_ok:
        click.echo("\n(attestation: ✓ signed by enclave)", err=True)
    else:
        click.echo(
            f"\n(attestation: ✗ {att_reason} — printed anyway because "
            "--no-strict-attestation is set)",
            err=True,
        )


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
        # Phase 3 pin-rotation: when present, the URI authorises any
        # compose blessed by the holder of this Ed25519 pubkey, with
        # the live attested-files digest taken from the signed pin.
        # Empty string = legacy single-compose URI.
        "signer_pubkey": params.get("signer", ""),
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

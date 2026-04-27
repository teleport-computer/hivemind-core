"""Owner-side commands: init, scope, load, share, schema, rotate-key."""

import json as _json
import time
from pathlib import Path
from urllib.parse import urlparse as _urlparse

import click
import httpx

from ._config import (
    _DEFAULT_PROFILE,
    _config_path,
    _headers,
    _load_config,
    _profile_name,
    _save_config,
)
from ._http import (
    _api_error,
    _http_get,
    _make_tarball,
    _warm_pin_from_trust,
)


# ── Test-patchable HTTP wrappers ──
#
# tests/test_cli_share.py monkey-patches ``hivemind.cli._hget`` /
# ``_hpost`` / ``_hdelete`` to redirect every CLI HTTP call onto an
# in-process Starlette TestClient. Submodules can't bind these at
# import time (the patches won't propagate), so we trampoline through
# the parent module on each call.
def _hget(*a, **kw):
    from . import _hget as _f
    return _f(*a, **kw)


def _hpost(*a, **kw):
    from . import _hpost as _f
    return _f(*a, **kw)


def _hdelete(*a, **kw):
    from . import _hdelete as _f
    return _f(*a, **kw)
from ._shared import (
    _DEFAULT_SERVICE,
    _batch_insert,
    _split_sql_statements,
)

_REPO_ROOT = Path(__file__).parent.parent.parent
_AGENTS_DIR = _REPO_ROOT / "agents"

_DOCKERFILE_MD_AGENT = """\
FROM hivemind-agent-base:latest
COPY _bridge.py .
COPY agent.py .
COPY prompt.md .
CMD ["python", "/app/agent.py"]
"""


@click.command()
@click.option(
    "--service",
    default=_DEFAULT_SERVICE,
    show_default=True,
    help="Hivemind service URL",
)
@click.option("--api-key", default="", help="API key for authentication")
def init(service: str, api_key: str):
    """Connect to a hivemind service and save config."""
    service = service.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    # If the operator has already run ``hivemind trust approve`` for this
    # URL, that flow saved the enclave's pinned cert. Wire it as the
    # default verify so the health check below doesn't blow up on the
    # self-signed -8100s. cert.
    _warm_pin_from_trust(service)

    # Probe /v1/health (tenant key). If the key turns out to be the
    # admin key (HIVEMIND_ADMIN_KEY), /v1/health 401s — try the
    # admin-only /v1/admin/tenants probe before giving up. This lets the
    # operator set up an admin profile the same way as a tenant profile.
    health: dict = {}
    role = "tenant"
    try:
        resp = _hget(f"{service}/v1/health", headers=headers, timeout=10)
        if resp.status_code == 401 and api_key:
            ar = _hget(
                f"{service}/v1/admin/tenants", headers=headers, timeout=10
            )
            if ar.status_code < 400:
                role = "admin"
                health = {
                    "table_count": "(admin)",
                    "version": "(admin)",
                }
            else:
                click.echo(
                    f"Error: 401 from {service} — key authorizes neither "
                    "a tenant nor admin role.",
                    err=True,
                )
                raise SystemExit(1)
        else:
            resp.raise_for_status()
            health = resp.json()
    except httpx.ConnectError:
        click.echo(f"Error: Cannot reach {service}", err=True)
        raise SystemExit(1)
    except httpx.HTTPStatusError as e:
        click.echo(
            f"Error: {e.response.status_code} from {service}", err=True
        )
        raise SystemExit(1)
    except httpx.TimeoutException:
        click.echo(
            f"Error: Connection timed out reaching {service}", err=True
        )
        raise SystemExit(1)

    _save_config({"service": service, "api_key": api_key, "role": role})
    profile = _profile_name()
    click.echo(
        f"Initialized profile '{profile}' (role={role}) at {_config_path()} "
        f"— connected to {service}"
    )
    click.echo(f"  Tables: {health.get('table_count', '?')}")
    click.echo(f"  Version: {health.get('version', '?')}")
    if profile == _DEFAULT_PROFILE:
        click.echo(
            "  Tip: pass --profile NAME to keep separate identities "
            "(admin / tenant_a / tenant_b) on the same laptop."
        )


@click.command()
@click.argument("rules", required=False)
@click.option(
    "--from-file",
    "from_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read rules from a file (use instead of RULES argument)",
)
@click.option(
    "--private-prompt",
    "private_prompt",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Upload the file as the entire scope prompt (no public template). "
    "Uses agents/private-default-scope; prompt content stays TEE-resident.",
)
def scope(
    rules: str | None,
    from_file: Path | None,
    private_prompt: Path | None,
):
    """Upload a scope agent with English privacy rules.

    RULES is a natural-language description of your access policy.
    It gets fused into the default scope agent prompt and uploaded
    as a tarball to the service.

    Example:

      hivemind scope "Share patient outcomes for research.
        Allow aggregate statistics and survival curves.
        Never expose individual records or exact dates of birth.
        Suppress groups smaller than 10."

      hivemind scope --from-file policy.md
      hivemind scope --private-prompt my-secret-rules.md
    """
    config = _load_config()
    if sum(bool(x) for x in (rules, from_file, private_prompt)) != 1:
        click.echo(
            "Error: provide exactly one of RULES / --from-file / --private-prompt.",
            err=True,
        )
        raise SystemExit(1)

    if private_prompt is not None:
        agent_dir = _AGENTS_DIR / "private-default-scope"
        prompt_text = private_prompt.read_text()
        description = "Private scope (prompt content TEE-resident)"
    else:
        if from_file is not None:
            rules = from_file.read_text()
        if not rules or not rules.strip():
            click.echo(
                "Error: Provide rules as an argument or via --from-file.",
                err=True,
            )
            raise SystemExit(1)
        rules = rules.strip()
        template_path = _AGENTS_DIR / "default-scope" / "scope-prompt.md"
        if not template_path.exists():
            click.echo(
                f"Error: Scope template not found: {template_path}", err=True
            )
            raise SystemExit(1)
        prompt_text = template_path.read_text().replace(
            "{scenario_description}", rules
        )
        agent_dir = _AGENTS_DIR / "default-scope"
        description = f"Scope: {rules[:200]}"

    agent_py = (agent_dir / "agent.py").read_text()
    bridge_py = (agent_dir / "_bridge.py").read_text()

    tarball = _make_tarball(
        {
            "Dockerfile": _DOCKERFILE_MD_AGENT,
            "_bridge.py": bridge_py,
            "agent.py": agent_py,
            "prompt.md": prompt_text,
        }
    )

    click.echo("Uploading scope agent...")
    service = config["service"]
    headers = _headers(config)

    # When --private-prompt is used, the prompt content stays TEE-resident
    # AND is excluded from attested_files_digest, so a recipient can verify
    # the public agent.py / _bridge.py / Dockerfile against the published
    # source without needing the private prompt to reproduce the digest.
    private_paths_json = _json.dumps(["prompt.md"]) if private_prompt else "[]"
    try:
        resp = _hpost(
            f"{service}/v1/agents/upload",
            files={
                "archive": ("scope-agent.tar.gz", tarball, "application/gzip")
            },
            data={
                "name": "private-scope-agent" if private_prompt else "scope-agent",
                "agent_type": "scope",
                "description": description,
                "private_paths": private_paths_json,
            },
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.ConnectError:
        click.echo(f"Error: Cannot reach {service}", err=True)
        raise SystemExit(1)
    except httpx.HTTPStatusError as e:
        click.echo(
            f"Error: Upload failed ({e.response.status_code}): {_api_error(e.response)}",
            err=True,
        )
        raise SystemExit(1)
    except httpx.TimeoutException:
        click.echo("Error: Upload timed out. Try again.", err=True)
        raise SystemExit(1)

    result = resp.json()
    agent_id = result["agent_id"]
    run_id = result.get("run_id")

    if run_id:
        click.echo(f"Building image (run: {run_id})...")
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
            click.echo(
                "Warning: Build still in progress. Check status manually.",
                err=True,
            )

    config["scope_agent_id"] = agent_id
    _save_config(config)
    click.echo(f"Scope agent: {agent_id}")


@click.command("load")
@click.argument(
    "file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--table",
    "table",
    default=None,
    help="Target table (required for CSV/JSONL; ignored for SQL).",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["auto", "sql", "csv", "jsonl"]),
    default="auto",
    show_default=True,
    help="File format. 'auto' infers from the extension.",
)
@click.option(
    "--batch",
    "batch",
    type=int,
    default=500,
    show_default=True,
    help="Rows per INSERT batch (CSV/JSONL).",
)
@click.option(
    "--delimiter",
    "delim",
    default=",",
    show_default=True,
    help="CSV delimiter.",
)
def load_cmd(
    file: Path,
    table: str | None,
    fmt: str,
    batch: int,
    delim: str,
):
    """Load a dataset into the service's Postgres via POST /v1/store.

    Formats:

      SQL    — splits statements and executes each one.
               `hivemind load dump.sql`

      CSV    — parameterized INSERTs into --table. First row is header.
               `hivemind load users.csv --table users`

      JSONL  — one JSON object per line → parameterized INSERTs.
               `hivemind load events.jsonl --table events`

    All rows are batched and sent through the normal auth-checked API, so
    this works against local Postgres and remote SQL-proxy-backed deploys
    alike.
    """
    import csv

    config = _load_config()
    service = config["service"]
    headers = {**_headers(config), "Content-Type": "application/json"}
    store_url = f"{service}/v1/store"

    if fmt == "auto":
        ext = file.suffix.lower()
        fmt = {".sql": "sql", ".csv": "csv", ".jsonl": "jsonl", ".ndjson": "jsonl"}.get(ext, "")
        if not fmt:
            raise click.ClickException(
                f"Cannot infer format from extension: {file.suffix}. "
                "Pass --format sql|csv|jsonl."
            )

    if fmt in ("csv", "jsonl") and not table:
        raise click.ClickException("--table is required for CSV/JSONL.")

    def _post(sql: str, params: list):
        try:
            r = _hpost(
                store_url,
                headers=headers,
                json={"sql": sql, "params": params},
                timeout=120,
            )
        except httpx.ConnectError:
            raise click.ClickException(f"Cannot reach {service}")
        if r.status_code >= 400:
            raise click.ClickException(f"{r.status_code}: {_api_error(r)}")

    if fmt == "sql":
        text = file.read_text()
        stmts = _split_sql_statements(text)
        click.echo(f"Loading {len(stmts)} SQL statements from {file} → {service}")
        with click.progressbar(stmts, label="exec") as bar:
            for stmt in bar:
                _post(stmt, [])
        click.echo(f"Done: {len(stmts)} statements.")
        return

    if fmt == "csv":
        with file.open(newline="") as f:
            reader = csv.reader(f, delimiter=delim)
            try:
                cols = next(reader)
            except StopIteration:
                raise click.ClickException(f"{file}: empty file")
            rows = list(reader)
        click.echo(
            f"Loading {len(rows)} rows from {file} into {table} "
            f"(columns: {', '.join(cols)}) → {service}"
        )
        _batch_insert(_post, table, cols, rows, batch)
        click.echo(f"Done: {len(rows)} rows.")
        return

    if fmt == "jsonl":
        rows_dicts: list[dict] = []
        with file.open() as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except _json.JSONDecodeError as e:
                    raise click.ClickException(f"{file}:{lineno}: {e}")
                if not isinstance(obj, dict):
                    raise click.ClickException(
                        f"{file}:{lineno}: expected object, got {type(obj).__name__}"
                    )
                rows_dicts.append(obj)
        if not rows_dicts:
            click.echo(f"{file}: no rows to load.")
            return
        cols = sorted({k for r in rows_dicts for k in r.keys()})
        rows = [[r.get(c) for c in cols] for r in rows_dicts]
        click.echo(
            f"Loading {len(rows)} rows from {file} into {table} "
            f"(columns: {', '.join(cols)}) → {service}"
        )
        _batch_insert(_post, table, cols, rows, batch)
        click.echo(f"Done: {len(rows)} rows.")
        return


@click.command()
@click.option(
    "--mint",
    is_flag=True,
    help="Mint a fresh hmq_ token bound to the active scope agent.",
)
@click.option(
    "--label", default="", help="Token label when --mint is used."
)
@click.option(
    "--token",
    "explicit_token",
    default=None,
    help="Use this hmq_ token instead of minting one.",
)
@click.option(
    "--pin-rotation",
    is_flag=True,
    help=(
        "Reference the latest signed compose pin instead of baking a "
        "single compose_hash. URIs survive any redeploy whose compose "
        "appears in the pin's allowed_composes. Run 'hivemind compose "
        "bless' first to publish a pin."
    ),
)
@click.option(
    "--can-upload-query-agent",
    "can_upload_query_agent",
    is_flag=True,
    default=False,
    help=(
        "When --mint is set, also grant the new token permission to "
        "upload its own query agent code (Phase 4: B-uploadable). "
        "Off by default — grant explicitly when you intend the recipient "
        "to ship their own SQL/analysis logic. The owner-pinned scope "
        "agent still policies every SQL the recipient's code emits."
    ),
)
def share(
    mint: bool,
    label: str,
    explicit_token: str | None,
    pin_rotation: bool,
    can_upload_query_agent: bool,
):
    """Print a hmq:// URI bundling token + trust pins for a recipient.

    The URI looks like::

        hmq://<host>/<scope_agent_id>?token=hmq_...&compose=<sha256>&files=<sha256>

    The recipient runs ``hivemind ask <URI> "<question>"`` — no profile,
    no init, no YAML files. The pins encoded in the URI are verified
    against the live service before each query.

    You can either mint a fresh capability token (``--mint``) or hand in
    one you already have (``--token hmq_...``). The owner ``hmk_`` key
    in your profile is never embedded — recipients only ever see the
    scoped ``hmq_`` token.
    """
    config = _load_config()
    service = config["service"]
    headers = _headers(config)
    scope_id = config.get("scope_agent_id")
    if not scope_id:
        click.echo(
            "Error: No scope agent registered. Run 'hivemind scope ...' "
            "or 'hivemind agents upload --type scope ...' first.",
            err=True,
        )
        raise SystemExit(1)

    if explicit_token and mint:
        click.echo("Error: pass --token OR --mint, not both.", err=True)
        raise SystemExit(1)
    if not explicit_token and not mint:
        click.echo(
            "Error: pass --mint to mint a fresh hmq_ token, or "
            "--token hmq_... to embed one you already have.",
            err=True,
        )
        raise SystemExit(1)

    if explicit_token:
        if not explicit_token.startswith("hmq_"):
            click.echo(
                "Error: --token must be an hmq_ capability token.", err=True
            )
            raise SystemExit(1)
        if can_upload_query_agent:
            click.echo(
                "Error: --can-upload-query-agent only applies with "
                "--mint. Re-issue the token via 'hivemind tokens issue "
                "--can-upload-query-agent' instead.",
                err=True,
            )
            raise SystemExit(1)
        token = explicit_token
    else:
        try:
            r = _hpost(
                f"{service}/v1/tokens",
                headers=headers,
                json={
                    "kind": "query",
                    "label": label,
                    "constraints": {
                        "scope_agent_id": scope_id,
                        "can_upload_query_agent": bool(
                            can_upload_query_agent
                        ),
                    },
                },
                timeout=30,
            )
        except httpx.RequestError as e:
            click.echo(f"Error minting token: {e}", err=True)
            raise SystemExit(2)
        if r.status_code >= 400:
            click.echo(
                f"Error {r.status_code}: {_api_error(r)}", err=True
            )
            raise SystemExit(3)
        token = r.json()["token"]

    try:
        r = _hget(
            f"{service}/v1/agents/{scope_id}/attest",
            headers=headers,
            timeout=30,
        )
    except httpx.RequestError as e:
        click.echo(f"Error fetching attestation pins: {e}", err=True)
        raise SystemExit(2)
    if r.status_code >= 400:
        click.echo(f"Error {r.status_code}: {_api_error(r)}", err=True)
        raise SystemExit(3)
    pin = r.json()
    # ``files=`` carries the *attested* files digest (excludes private
    # files like the scope prompt). Recipient verifies live
    # ``attested_files_digest_sha256`` matches. Older deployments only
    # expose ``files_digest_sha256`` (over all files); fall back to it
    # so this CLI keeps minting URIs against pre-Phase-1 servers.
    files_digest = (
        pin.get("attested_files_digest_sha256")
        or pin.get("files_digest_sha256")
        or ""
    ).lower()
    inner_att = (pin.get("attestation") or {}).get("attestation") or {}
    compose_hash = (inner_att.get("compose_hash") or "").lower()

    parsed = _urlparse(service)
    host = parsed.netloc
    qs: list[str] = [f"token={token}"]
    if pin_rotation:
        # Pin-rotation mode: bake the signer pubkey instead of the
        # compose hash. Recipient pulls the latest pin, verifies the
        # signature against this pubkey, then enforces "live compose ∈
        # allowed_composes" + matching attested files digest.
        try:
            pr = _hget(
                f"{service}/v1/tenants/compose-pin",
                headers=headers,
                timeout=15,
            )
        except httpx.RequestError as e:
            click.echo(
                f"Error fetching compose pin: {e}\n"
                f"  Run 'hivemind compose bless' first.",
                err=True,
            )
            raise SystemExit(2)
        if pr.status_code == 404:
            click.echo(
                "Error: no compose pin published yet. Run "
                "'hivemind compose bless' to publish one.",
                err=True,
            )
            raise SystemExit(3)
        if pr.status_code >= 400:
            click.echo(
                f"Error {pr.status_code} fetching compose pin: "
                f"{_api_error(pr)}",
                err=True,
            )
            raise SystemExit(3)
        pin_env = (pr.json().get("envelope") or {})
        signer = (pin_env.get("signer_pubkey") or "").strip()
        if not signer:
            click.echo(
                "Error: compose pin envelope missing signer_pubkey.",
                err=True,
            )
            raise SystemExit(3)
        # ``signer`` is base64; URL-safe wrt the hmq:// query parser
        # except for ``+`` and ``/``. Recipient round-trips through
        # base64 decode anyway, so we percent-encode here.
        from urllib.parse import quote as _q

        qs.append(f"signer={_q(signer, safe='')}")
    elif compose_hash:
        qs.append(f"compose={compose_hash}")
    if files_digest and not pin_rotation:
        # In pin-rotation mode the files digest is carried (signed)
        # inside the envelope — don't bake a stale snapshot into the
        # URI that would have to be reissued on every agent edit.
        qs.append(f"files={files_digest}")
    if parsed.scheme and parsed.scheme != "https":
        qs.append(f"scheme={parsed.scheme}")
    # Embed the owner's query agent id so `hivemind ask` doesn't need
    # the recipient to know it. Without this the server falls back to
    # default_query_agent (often unset on multi-tenant deploys), and
    # the recipient gets "No query agent specified".
    qa_id = (config.get("query_agent_id") or "").strip()
    if qa_id:
        qs.append(f"qa={qa_id}")
    uri = f"hmq://{host}/{scope_id}?" + "&".join(qs)
    click.echo(uri)


@click.command("schema")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def schema_cmd(as_json: bool):
    """Print the tenant's table schema (column names + types per table)."""
    config = _load_config()
    service = config["service"]
    data = _http_get(
        f"{service}/v1/admin/schema", headers=_headers(config), timeout=30
    )
    schema = data.get("schema") or []
    if as_json:
        click.echo(_json.dumps(schema, indent=2, default=str))
        return
    if not schema:
        click.echo("(no tables)")
        return
    by_table: dict[str, list] = {}
    for row in schema:
        by_table.setdefault(row.get("table_name", "?"), []).append(row)
    for table, cols in sorted(by_table.items()):
        click.echo(f"{table}")
        for col in cols:
            name = col.get("column_name") or col.get("name") or "?"
            typ = col.get("data_type") or col.get("type") or "?"
            nullable = "" if col.get("is_nullable") == "YES" else " NOT NULL"
            click.echo(f"  {name:<28} {typ}{nullable}")


@click.command("rotate-key")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
@click.confirmation_option(
    prompt=(
        "Rotate this tenant's API key? The current key will stop working "
        "immediately. Continue?"
    )
)
def rotate_key(as_json: bool):
    """Rotate this tenant's API key and update local config.

    Designed as the mandatory first action for a new tenant: the admin
    who created the tenant briefly saw the plaintext key. Rotating
    immediately cuts them out of the trust loop — from here on, only
    the TEE holds anything that maps to this tenant's data.
    """
    config = _load_config()
    service = config["service"]
    headers = _headers(config)
    if "Authorization" not in headers:
        click.echo("Error: no api_key in config. Run 'hivemind init'.", err=True)
        raise SystemExit(1)

    try:
        resp = _hpost(
            f"{service}/v1/tenant/rotate-key", headers=headers, timeout=30,
        )
    except httpx.RequestError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {_api_error(resp)}", err=True)
        raise SystemExit(3)

    data = resp.json()
    new_key = data["api_key"]
    config["api_key"] = new_key
    _save_config(config)

    if as_json:
        click.echo(_json.dumps(data, indent=2))
        return
    click.echo(f"Tenant: {data['tenant_id']}")
    click.echo(
        f"New API key (saved to profile '{_profile_name()}' "
        f"at {_config_path()}):"
    )
    click.echo(f"  {new_key}")
    click.echo("")
    click.echo(
        "Previous key is now revoked. Anyone who held the old key "
        "(including the admin who minted it) can no longer reach your data."
    )

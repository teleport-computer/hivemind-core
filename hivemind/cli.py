"""Hivemind CLI — conditional recall for the privacy-quality frontier.

Remote client for a hivemind-core service deployed in a TEE.

Usage:
  hivemind init [--service <url>] [--api-key <key>]
  hivemind load <file> [--table <name>] [--format sql|csv|jsonl]
  hivemind scope "<english rules>" | --from-file <path>
  hivemind share
  hivemind query "<question>"
  hivemind run <path> [--prompt "..."] [--json] [--fetch]
  hivemind runs [<run_id>]
  hivemind agents
"""

import io
import json as _json
import sys
import tarfile
import time
from pathlib import Path

import click
import httpx
import yaml

_CONFIG_DIR = ".hivemind"
_CONFIG_FILE = "config.yaml"
_REPO_ROOT = Path(__file__).parent.parent
_AGENTS_DIR = _REPO_ROOT / "agents"
_DEFAULT_SERVICE = "http://localhost:8100"

_DOCKERFILE_MD_AGENT = """\
FROM hivemind-agent-base:latest
COPY _bridge.py .
COPY agent.py .
COPY prompt.md .
CMD ["python", "/app/agent.py"]
"""


# ── Config helpers ──


def _config_path() -> Path:
    return Path(_CONFIG_DIR) / _CONFIG_FILE


def _load_config() -> dict:
    """Load .hivemind/config.yaml or exit with error."""
    p = _config_path()
    if not p.exists():
        click.echo(
            "Error: No .hivemind/config.yaml found. Run 'hivemind init' first.",
            err=True,
        )
        raise SystemExit(1)
    try:
        with open(p) as f:
            config = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        click.echo(f"Error: Corrupt config file: {e}", err=True)
        raise SystemExit(1)
    if not config.get("service"):
        click.echo(
            "Error: Config missing 'service' URL. Run 'hivemind init' again.",
            err=True,
        )
        raise SystemExit(1)
    return config


def _save_config(config: dict) -> None:
    Path(_CONFIG_DIR).mkdir(exist_ok=True)
    with open(_config_path(), "w") as f:
        yaml.dump(config, f, default_flow_style=False)


def _headers(config: dict) -> dict:
    h: dict[str, str] = {}
    if config.get("api_key"):
        h["Authorization"] = f"Bearer {config['api_key']}"
    return h


def _make_tarball(files: dict[str, str]) -> bytes:
    """Create a gzipped tarball from {filename: content} dict."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _tarball_from_dir(path: Path) -> bytes:
    """Pack a directory into a gzipped tarball (files at top level)."""
    if not path.exists():
        raise click.ClickException(f"Path not found: {path}")
    if not path.is_dir():
        raise click.ClickException(f"Not a directory: {path}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in sorted(path.rglob("*")):
            if f.is_file():
                if any(
                    part in {"__pycache__", ".git", ".venv", "node_modules"}
                    for part in f.parts
                ):
                    continue
                tar.add(f, arcname=str(f.relative_to(path)))
    data = buf.getvalue()
    if len(data) == 0:
        raise click.ClickException(f"No files found in {path}")
    return data


def _api_error(resp: httpx.Response) -> str:
    """Extract server detail field for nicer error messages."""
    try:
        j = resp.json()
        if isinstance(j, dict):
            return str(j.get("detail") or j.get("error") or resp.text)
    except Exception:
        pass
    return resp.text or f"HTTP {resp.status_code}"


def _http_get(url: str, headers: dict, timeout: float = 30) -> dict:
    try:
        r = httpx.get(url, headers=headers, timeout=timeout)
    except httpx.ConnectError:
        click.echo(f"Error: Cannot reach {url}", err=True)
        raise SystemExit(2)
    except httpx.TimeoutException:
        click.echo(f"Error: Timed out fetching {url}", err=True)
        raise SystemExit(2)
    if r.status_code >= 400:
        click.echo(f"Error {r.status_code}: {_api_error(r)}", err=True)
        raise SystemExit(3)
    return r.json()


# ── CLI ──


@click.group()
def cli():
    """Hivemind — conditional recall for the privacy-quality frontier."""
    pass


@cli.command()
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

    try:
        resp = httpx.get(f"{service}/v1/health", headers=headers, timeout=10)
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

    _save_config({"service": service, "api_key": api_key})
    click.echo(f"Initialized .hivemind/ — connected to {service}")
    click.echo(f"  Tables: {health.get('table_count', '?')}")
    click.echo(f"  Version: {health.get('version', '?')}")


@cli.command()
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

    try:
        resp = httpx.post(
            f"{service}/v1/agents/upload",
            files={
                "archive": ("scope-agent.tar.gz", tarball, "application/gzip")
            },
            data={
                "name": "private-scope-agent" if private_prompt else "scope-agent",
                "agent_type": "scope",
                "description": description,
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
                sr = httpx.get(
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


@cli.command("load")
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
            r = httpx.post(
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


@cli.command()
def share():
    """Show the query endpoint and connection details."""
    config = _load_config()
    scope_id = config.get("scope_agent_id")

    if not scope_id:
        click.echo(
            "Error: No scope agent registered. Run 'hivemind scope' first.",
            err=True,
        )
        raise SystemExit(1)

    service = config["service"]
    click.echo(f"Endpoint:    {service}/v1/query")
    click.echo(f"Scope agent: {scope_id}")
    if config.get("api_key"):
        click.echo(f"API key:     {config['api_key']}")
    click.echo()
    click.echo("Example:")
    click.echo('  hivemind query "What tables are available?"')


@cli.command("query")
@click.argument("question")
@click.option("--endpoint", default=None, help="Override service URL")
@click.option(
    "--async", "use_async", is_flag=True, help="Use async submit+poll"
)
def query_cmd(question: str, endpoint: str | None, use_async: bool):
    """Send a natural-language query to the hivemind service."""
    config = _load_config()
    service = endpoint or config["service"]
    headers = _headers(config)

    payload: dict = {"query": question}
    if config.get("scope_agent_id"):
        payload["scope_agent_id"] = config["scope_agent_id"]

    if use_async:
        _query_async(service, headers, payload)
    else:
        _query_sync(service, headers, payload)


def _query_sync(service: str, headers: dict, payload: dict) -> None:
    try:
        resp = httpx.post(
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
        resp = httpx.post(
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
            sr = httpx.get(
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


@cli.command("run")
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

    if not as_json:
        click.echo(f"Packing {path} → {archive_name} ({len(archive_bytes)} bytes)")
        click.echo(f"Uploading to {service}...")

    try:
        resp = httpx.post(
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
            sr = httpx.get(
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


def _emit_run_result(
    service: str,
    data: dict,
    run_id: str,
    *,
    as_json: bool,
    fetch: bool,
) -> None:
    result = data.get("result") or {}
    output = result.get("output", "")
    mediated = result.get("mediated")
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
                r = httpx.get(
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


@cli.command("runs")
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

        stages = data.get("stages") or {}
        if stages:
            click.echo("Stages:")
            for name, s in stages.items():
                started = s.get("started_at")
                ended = s.get("ended_at")
                dur = (
                    f"{ended - started:.1f}s"
                    if started and ended
                    else "(running)"
                )
                click.echo(f"  {name}: {dur}")

        result = data.get("result") or {}
        output = result.get("output")
        if output:
            click.echo("")
            click.echo("── Output ──")
            click.echo(output)

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


@cli.command("agents")
@click.option(
    "--type",
    "agent_type",
    default=None,
    help="Filter by agent type (query/scope/mediator/index)",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout")
def agents_cmd(agent_type: str | None, as_json: bool):
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


@cli.command("rotate-key")
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
        resp = httpx.post(
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
    click.echo("New API key (saved to .hivemind/config.yaml):")
    click.echo(f"  {new_key}")
    click.echo("")
    click.echo(
        "Previous key is now revoked. Anyone who held the old key "
        "(including the admin who minted it) can no longer reach your data."
    )


# ── Admin: tenant management ──


def _admin_headers(admin_key: str) -> dict:
    return {
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }


def _resolve_admin_service(service: str | None) -> str:
    if service:
        return service.rstrip("/")
    # Fall back to the regular init config (admins often manage locally).
    try:
        return _load_config()["service"]
    except SystemExit:
        return _DEFAULT_SERVICE


@cli.group("admin")
def admin_cli():
    """Multi-tenant admin: provision, list, delete tenants.

    Requires the server's HIVEMIND_ADMIN_KEY. Pass it with --admin-key
    or set HIVEMIND_ADMIN_KEY in your shell environment.
    """
    pass


@admin_cli.command("create-tenant")
@click.argument("name")
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    required=True,
    help="Admin bearer token (or set HIVEMIND_ADMIN_KEY)",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
def admin_create_tenant(name: str, service: str | None, admin_key: str, as_json: bool):
    """Provision a new tenant. Prints the one-time API key."""
    url = _resolve_admin_service(service)
    try:
        resp = httpx.post(
            f"{url}/v1/admin/tenants",
            headers=_admin_headers(admin_key),
            json={"name": name},
            timeout=60,
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
    click.echo(f"Tenant:   {data['tenant_id']}  ({data['name']})")
    click.echo(f"Database: {data['db_name']}")
    click.echo("")
    click.echo("API key (store it now — we won't show it again):")
    click.echo(f"  {data['api_key']}")


@admin_cli.command("register-existing")
@click.argument("name")
@click.argument("db_name")
@click.option(
    "--api-key",
    default=None,
    help="Reuse this key (defaults to a newly minted one)",
)
@click.option(
    "--tenant-id",
    default=None,
    help="Pin the control-plane tenant_id (use when db_name=tenant_<tenant_id>)",
)
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    required=True,
    help="Admin bearer token",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
def admin_register_existing(
    name: str,
    db_name: str,
    api_key: str | None,
    tenant_id: str | None,
    service: str | None,
    admin_key: str,
    as_json: bool,
):
    """Adopt a pre-populated Postgres database as a tenant."""
    url = _resolve_admin_service(service)
    body: dict[str, str] = {"name": name, "db_name": db_name}
    if api_key:
        body["api_key"] = api_key
    if tenant_id:
        body["tenant_id"] = tenant_id
    try:
        resp = httpx.post(
            f"{url}/v1/admin/tenants/register",
            headers=_admin_headers(admin_key),
            json=body,
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
        click.echo(_json.dumps(data, indent=2))
        return
    click.echo(f"Tenant:   {data['tenant_id']}  ({data['name']})")
    click.echo(f"Database: {data['db_name']}")
    click.echo("API key:")
    click.echo(f"  {data['api_key']}")


@admin_cli.command("list-tenants")
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    required=True,
    help="Admin bearer token",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
def admin_list_tenants(service: str | None, admin_key: str, as_json: bool):
    """List all tenants."""
    url = _resolve_admin_service(service)
    try:
        resp = httpx.get(
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


@admin_cli.command("delete-tenant")
@click.argument("tenant_id")
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    required=True,
    help="Admin bearer token",
)
@click.confirmation_option(
    prompt="Drop the tenant DB and all its data? This cannot be undone."
)
def admin_delete_tenant(tenant_id: str, service: str | None, admin_key: str):
    """Delete a tenant: drops its Postgres DB and revokes its key."""
    url = _resolve_admin_service(service)
    try:
        resp = httpx.delete(
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


@admin_cli.command("rename-database")
@click.argument("old_name")
@click.argument("new_name")
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    required=True,
    help="Admin bearer token",
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

    Does NOT update control-plane rows — follow up with
    'hivemind admin register-existing <name> <new_name> --tenant-id <t_...>'
    to adopt the renamed DB as a tenant.
    """
    url = _resolve_admin_service(service)
    try:
        resp = httpx.post(
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


@admin_cli.command("migrate-to-roles")
@click.option("--service", default=None, help="Hivemind service URL")
@click.option(
    "--admin-key",
    envvar="HIVEMIND_ADMIN_KEY",
    required=True,
    help="Admin bearer token",
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

    url = _resolve_admin_service(service)
    try:
        resp = httpx.post(
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


if __name__ == "__main__":
    cli()

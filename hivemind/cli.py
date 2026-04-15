"""Hivemind CLI — conditional recall for the privacy-quality frontier.

Remote client for a hivemind-core service deployed in a TEE.

Usage:
  hivemind init --service <url> [--api-key <key>]
  hivemind scope "<english rules>"
  hivemind share
  hivemind query "<question>"
"""

import io
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

_DOCKERFILE_MD_AGENT = """\
FROM hivemind-agent-sdk-base:latest
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


# ── CLI ──


@click.group()
def cli():
    """Hivemind — conditional recall for the privacy-quality frontier."""
    pass


@cli.command()
@click.option("--service", required=True, help="Hivemind service URL")
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
@click.argument("rules")
def scope(rules: str):
    """Upload a scope agent with English privacy rules.

    RULES is a natural-language description of your access policy.
    It gets fused into the default scope agent prompt and uploaded
    as a tarball to the service.

    Example:

      hivemind scope "Share patient outcomes for research.
        Allow aggregate statistics and survival curves.
        Never expose individual records or exact dates of birth.
        Suppress groups smaller than 10."
    """
    config = _load_config()
    rules = rules.strip()
    if not rules:
        click.echo("Error: Rules cannot be empty.", err=True)
        raise SystemExit(1)

    # Read template and fuse rules into {scenario_description}
    template_path = _AGENTS_DIR / "default-scope" / "scope-prompt.md"
    if not template_path.exists():
        click.echo(
            f"Error: Scope template not found: {template_path}", err=True
        )
        raise SystemExit(1)
    fused_prompt = template_path.read_text().replace(
        "{scenario_description}", rules
    )

    # Read agent source files
    agent_dir = _AGENTS_DIR / "default-scope"
    agent_py = (agent_dir / "agent.py").read_text()
    bridge_py = (agent_dir / "_bridge.py").read_text()

    # Build tarball
    tarball = _make_tarball(
        {
            "Dockerfile": _DOCKERFILE_MD_AGENT,
            "_bridge.py": bridge_py,
            "agent.py": agent_py,
            "prompt.md": fused_prompt,
        }
    )

    # Upload
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
                "name": "scope-agent",
                "agent_type": "scope",
                "description": f"Scope: {rules[:200]}",
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
            f"Error: Upload failed ({e.response.status_code}): {e.response.text}",
            err=True,
        )
        raise SystemExit(1)
    except httpx.TimeoutException:
        click.echo("Error: Upload timed out. Try again.", err=True)
        raise SystemExit(1)

    result = resp.json()
    agent_id = result["agent_id"]
    run_id = result.get("run_id")

    # Poll for build completion
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

    # Save scope agent ID to config
    config["scope_agent_id"] = agent_id
    _save_config(config)
    click.echo(f"Scope agent: {agent_id}")


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
            f"Error: {e.response.status_code}: {e.response.text}", err=True
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
    # Submit
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

    # Poll
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


if __name__ == "__main__":
    cli()

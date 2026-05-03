"""``hmctl sql`` — run arbitrary SQL against the tenant database.

Hits POST /v1/tenant/sql with the same semantics as the website's SQL
console: SELECTs return rows, DML returns rowcount, ``_hivemind_*``
internals are refused server-side. Parameterized queries use psycopg's
``%s`` placeholder style (pyformat) — NOT PostgreSQL native ``$1``.

Why a top-level command instead of a subgroup: today there's only one
verb (``execute``). Adding a group with one member is just noise; if
later we add ``sql import``, ``sql dump``, etc., we promote it then.
"""

import json as _json
import sys
from pathlib import Path

import click

from . import _hpost
from ._config import _headers, _load_config
from ._http import _api_error


@click.command("sql")
@click.argument("sql_text", required=False)
@click.option(
    "-f",
    "--file",
    "sql_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Read SQL from a file instead of an inline argument.",
)
@click.option(
    "-p",
    "--param",
    "params",
    multiple=True,
    help="Parameter values for %s placeholders, in order. "
    "Repeat the flag once per placeholder.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit raw JSON on stdout instead of a formatted table.",
)
def sql_cmd(
    sql_text: str | None,
    sql_file: Path | None,
    params: tuple[str, ...],
    as_json: bool,
):
    """Run SQL against your tenant database (owner only).

    \b
    Examples:
      hmctl sql 'SELECT COUNT(*) FROM watch_history'
      hmctl sql 'SELECT * FROM watch_history WHERE id = %s' -p 42
      hmctl sql -f migrations/001_init.sql
      hmctl sql 'DROP TABLE old_table'
    """
    if sql_file and sql_text:
        raise click.UsageError(
            "Pass SQL inline OR via --file, not both."
        )
    if sql_file:
        sql_str = sql_file.read_text()
    elif sql_text:
        sql_str = sql_text
    elif not sys.stdin.isatty():
        # Allow `cat schema.sql | hmctl sql` so tooling can pipe.
        sql_str = sys.stdin.read()
    else:
        raise click.UsageError(
            "No SQL provided. Pass an inline string, --file PATH, or pipe via stdin."
        )

    config = _load_config()
    service = config["service"].rstrip("/")
    headers = _headers(config)

    body = {"sql": sql_str, "params": list(params)}
    resp = _hpost(
        f"{service}/v1/tenant/sql",
        headers={**headers, "Content-Type": "application/json"},
        json=body,
        timeout=120,
    )
    if resp.status_code >= 400:
        click.echo(
            f"Error {resp.status_code}: {_api_error(resp)}", err=True
        )
        raise SystemExit(1)
    data = resp.json()

    if as_json:
        click.echo(_json.dumps(data, indent=2, default=str))
        return

    rows = data.get("rows") or []
    if rows:
        cols = list(rows[0].keys())
        widths = {
            c: max(len(c), *(len(_render(r.get(c))) for r in rows))
            for c in cols
        }
        click.echo(" | ".join(c.ljust(widths[c]) for c in cols))
        click.echo("-+-".join("-" * widths[c] for c in cols))
        for r in rows:
            click.echo(
                " | ".join(_render(r.get(c)).ljust(widths[c]) for c in cols)
            )
        click.echo(f"\n({len(rows)} row{'' if len(rows) == 1 else 's'})")
    else:
        rowcount = data.get("rowcount", 0)
        click.echo(f"OK · {rowcount} row{'' if rowcount == 1 else 's'} affected")


def _render(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return _json.dumps(v, default=str)
    return str(v)

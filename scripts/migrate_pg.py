#!/usr/bin/env python3
"""HTTP-only Postgres migrator for hivemind-pg cluster moves.

Why this exists: the hivemind-pg CVMs only expose the SQL-proxy HTTP
surface (no port 5432 to the public internet), and a cluster move
(prod5 → prod9) requires a fresh sealed LUKS volume on the destination
CVM. Standard `pg_dump | pg_restore` doesn't apply — there's no wire
protocol path between the two enclaves.

Approach: replicate every non-system database table-by-table through
the sql-proxy on both sides.

  1. List src databases via `GET /admin/tenants`.
  2. For each db (except `hivemind`/`postgres` defaults that are empty):
     a. Create the same db on dst (`POST /admin/tenants`) — idempotent
        on existing.
     b. Discover tables in `public` via `information_schema.tables`.
     c. Reconstruct CREATE TABLE on dst from `information_schema.columns`.
     d. Stream rows in OFFSET/LIMIT batches; convert each batch to CSV
        and `POST /import/csv` (COPY-backed on the proxy side).
     e. Replicate non-PK indexes from `pg_indexes.indexdef`.
     f. Verify destination row count == source.

Limitations:
  - Schema reconstruction is column-only (column_name, data_type,
    nullable, default). FOREIGN KEYS, CHECK constraints, and
    table-level options are NOT migrated. PKs are reconstructed via
    pg_indexes.
  - Sequences (SERIAL columns) need manual sync afterward; we don't
    touch them in v1. None of the hivemind tenant tables observed
    (2026-04-25) use sequences, so this is fine for the prod9 move.
  - `bytea` columns: returned by /execute as base64 strings — we
    re-encode as `\\x<hex>` for COPY. Don't migrate large blobs this way.
  - Idempotency: assumes destination tables are empty. The script
    refuses to overwrite a table that already has rows unless
    --truncate-dst is passed.

Usage:
    SQL_PROXY_KEY=… SQL_PROXY_ADMIN_KEY=… \\
    uv run python scripts/migrate_pg.py \\
        --src https://OLD-8080.gateway/ \\
        --dst https://NEW-8080.gateway/ \\
        [--dbs hivemind_control,tenant_t_…]   # default: all non-system
        [--batch-size 5000]
        [--dry-run]                            # plan only, no writes
        [--truncate-dst]                       # allow overwrite
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx


# ── Wire helpers ────────────────────────────────────────────────────


class Proxy:
    """Thin wrapper over a sql-proxy /execute, /admin, /import/csv URL."""

    def __init__(self, base: str, proxy_key: str, admin_key: str = "") -> None:
        self.base = base.rstrip("/")
        self.proxy_key = proxy_key
        self.admin_key = admin_key
        # 5-min read timeout: the big watch_history table SELECTs in
        # batches of 5k still take a few seconds each, plus gateway
        # buffering. Connect timeout stays short so we fail fast on
        # routing mistakes.
        self.client = httpx.Client(
            verify=False,
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0),
            limits=httpx.Limits(max_connections=4),
        )

    def _proxy_headers(self, db: str | None) -> dict[str, str]:
        h = {"X-Proxy-Key": self.proxy_key, "Content-Type": "application/json"}
        if db:
            h["X-Tenant-DB"] = db
        return h

    def _admin_headers(self) -> dict[str, str]:
        return {"X-Admin-Key": self.admin_key, "Content-Type": "application/json"}

    def execute(self, db: str | None, sql: str, params: list | None = None) -> list[dict]:
        r = self.client.post(
            f"{self.base}/execute",
            headers=self._proxy_headers(db),
            json={"sql": sql, "params": params or []},
        )
        if r.status_code != 200:
            try:
                err = (r.json() or {}).get("error") or r.text
            except Exception:
                err = r.text
            raise RuntimeError(
                f"/execute {r.status_code}: {err} (sql={sql[:200]!r})"
            )
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"/execute error: {body['error']} (sql={sql!r})")
        return body.get("rows", [])

    def execute_commit(self, db: str | None, sql: str, params: list | None = None) -> int:
        r = self.client.post(
            f"{self.base}/execute_commit",
            headers=self._proxy_headers(db),
            json={"sql": sql, "params": params or []},
        )
        if r.status_code != 200:
            try:
                err = (r.json() or {}).get("error") or r.text
            except Exception:
                err = r.text
            raise RuntimeError(
                f"/execute_commit {r.status_code}: {err} (sql={sql[:160]!r})"
            )
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"/execute_commit error: {body['error']} (sql={sql!r})")
        return int(body.get("rowcount", 0))

    def import_csv(
        self,
        db: str,
        table: str,
        csv_data: str,
        columns: list[str],
        delimiter: str = ",",
    ) -> int:
        r = self.client.post(
            f"{self.base}/import/csv",
            headers=self._proxy_headers(db),
            json={
                "table": table,
                "data": csv_data,
                "delimiter": delimiter,
                "header": True,
                "columns": columns,
            },
        )
        if r.status_code != 200:
            # Surface the proxy's actual error string. raise_for_status
            # alone discards the body, which on COPY failures is the
            # Postgres error message — exactly what we need for triage.
            try:
                err = (r.json() or {}).get("error") or r.text
            except Exception:
                err = r.text
            raise RuntimeError(
                f"/import/csv {r.status_code} (table={table}): {err}"
            )
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"/import/csv error: {body['error']} (table={table})")
        return int(body.get("rows_imported", 0))

    def list_dbs(self) -> list[str]:
        r = self.client.get(f"{self.base}/admin/tenants", headers=self._admin_headers())
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"/admin/tenants error: {body['error']}")
        return body.get("databases", [])

    def create_db(self, db: str) -> None:
        r = self.client.post(
            f"{self.base}/admin/tenants",
            headers=self._admin_headers(),
            json={"db_name": db},
        )
        # Tolerate "already exists" — sql-proxy returns 400 with that
        # message in our setup.
        if r.status_code == 200:
            return
        try:
            err = (r.json() or {}).get("error", "")
        except Exception:
            err = r.text
        if "exists" in err.lower():
            return
        r.raise_for_status()


# ── Schema reconstruction ───────────────────────────────────────────


def fetch_columns(p: Proxy, db: str, table: str) -> list[dict]:
    """Return ordered column metadata from information_schema."""
    return p.execute(
        db,
        """SELECT column_name, data_type, udt_name, is_nullable,
                  column_default, character_maximum_length,
                  numeric_precision, numeric_scale
             FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position""",
        [table],
    )


def column_type_sql(col: dict) -> str:
    """Map information_schema column row to a CREATE TABLE type fragment."""
    dt = (col.get("data_type") or "").lower()
    udt = (col.get("udt_name") or "").lower()
    cml = col.get("character_maximum_length")
    np = col.get("numeric_precision")
    ns = col.get("numeric_scale")

    # Use udt_name when data_type is generic ("USER-DEFINED", "ARRAY",
    # "jsonb"-vs-"json", etc.) — it's the postgres-specific name.
    if dt == "user-defined":
        return udt
    if dt == "array":
        # information_schema reports the element type via udt_name as
        # "_int4" etc. — drop the leading underscore and append "[]".
        elem = udt.lstrip("_")
        return f"{elem}[]"
    if dt in ("character varying", "varchar"):
        return f"varchar({cml})" if cml else "varchar"
    if dt in ("character", "char"):
        return f"char({cml})" if cml else "char"
    if dt == "numeric":
        if np and ns is not None:
            return f"numeric({np},{ns})"
        return "numeric"
    # Pass-through types that don't need parametrization.
    return udt or dt


def build_create_table(table: str, cols: list[dict]) -> str:
    """Plain CREATE TABLE — no constraints. PK + indexes are layered on
    after the data load, so we don't need to model them here."""
    lines = []
    for c in cols:
        ty = column_type_sql(c)
        nn = "" if c["is_nullable"] == "YES" else " NOT NULL"
        # Skip column DEFAULT for now: nextval(...) defaults would
        # create dependency on sequences we don't migrate. Tenant tables
        # observed don't rely on defaults for correctness during a
        # one-shot copy.
        lines.append(f"  {quote_ident(c['column_name'])} {ty}{nn}")
    body = ",\n".join(lines)
    return f"CREATE TABLE IF NOT EXISTS {quote_ident(table)} (\n{body}\n)"


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def fetch_indexes(p: Proxy, db: str, table: str) -> list[str]:
    """All non-PK indexes' DDL. The PK index is handled separately."""
    rows = p.execute(
        db,
        """SELECT indexdef FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = %s""",
        [table],
    )
    return [r["indexdef"] for r in rows]


def fetch_pk_columns(p: Proxy, db: str, table: str) -> list[str]:
    rows = p.execute(
        db,
        """SELECT a.attname AS colname
             FROM pg_index i
             JOIN pg_attribute a
               ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = (%s::regclass) AND i.indisprimary
            ORDER BY array_position(i.indkey::int[], a.attnum::int)""",
        [f"public.{table}"],
    )
    return [r["colname"] for r in rows]


# ── Data copy ───────────────────────────────────────────────────────


def coerce_for_csv(value: Any) -> str | None:
    """Render a JSON-decoded value back to a Postgres-COPY-friendly token.

    The /execute endpoint round-trips most types as JSON: ints/strs/bools
    pass through, timestamps come back as ISO strings, arrays as Python
    lists, jsonb as nested dicts/lists. Returns None for SQL NULL —
    callers must distinguish None (NULL) from "" (empty string) when
    writing the CSV row. Postgres' default CSV NULL marker is an
    unquoted empty field; an empty string must be written as a quoted
    "" to avoid being parsed as NULL."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        # jsonb: feed as JSON string; postgres parses it back via ::jsonb cast.
        # For arrays of scalars, Postgres COPY accepts the curly-brace
        # form `{a,b,c}` not JSON — but we'd need to know if the column
        # is jsonb vs array. The simplest tactic: serialize to JSON and
        # let the user handle array columns as a follow-up if any
        # observed table uses them. None do in the prod5 inventory.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        # bytea: emit `\x<hex>` form (postgres recognizes this with
        # bytea_output='hex' which is the default).
        return "\\x" + value.hex()
    # Fallback — surfaces in logs if a new type slips in.
    return str(value)


def _csv_field(v: str | None) -> str:
    """Manual encoder. Python's csv module collapses None and "" into
    the same on-wire bytes (an unquoted empty field), and Postgres COPY
    in CSV format treats unquoted empty as NULL. We need to distinguish
    so empty-string source columns survive a NOT NULL constraint on
    the destination."""
    if v is None:
        return ""  # NULL: unquoted empty → COPY's default NULL marker
    if v == "" or "," in v or '"' in v or "\n" in v or "\r" in v:
        # Empty string forced into quotes so COPY reads it as ''.
        return '"' + v.replace('"', '""') + '"'
    return v


def rows_to_csv(rows: list[dict], columns: list[str]) -> str:
    parts: list[str] = []
    parts.append(",".join(_csv_field(c) for c in columns))
    for r in rows:
        parts.append(",".join(_csv_field(coerce_for_csv(r.get(c))) for c in columns))
    # Trailing newline matters: COPY treats it as the row terminator.
    return "\r\n".join(parts) + "\r\n"


def copy_table(
    src: Proxy, dst: Proxy, db: str, table: str, batch_size: int, dry_run: bool
) -> tuple[int, int]:
    cols_meta = fetch_columns(src, db, table)
    if not cols_meta:
        print(f"      ! table {table} has no columns — skipped")
        return 0, 0
    columns = [c["column_name"] for c in cols_meta]

    if dry_run:
        ddl = build_create_table(table, cols_meta)
        print(f"      [dry-run] would CREATE TABLE {table} ({len(columns)} cols)")
        return 0, 0

    # 1. CREATE TABLE on dst (idempotent).
    dst.execute_commit(db, build_create_table(table, cols_meta))

    # 2. Stream rows.
    src_count = src.execute(db, f"SELECT count(*) AS n FROM {quote_ident(table)}")[0]["n"]
    if src_count == 0:
        return 0, 0

    # Build select expressions: cast types the proxy can't JSON-serialize
    # to text. UUID is the known offender (uuid.UUID is not JSON-native);
    # numeric/Decimal would be the next likely if it shows up in any
    # tenant table. The destination COPY parses the text form back into
    # the typed column.
    def _select_expr(col: dict) -> str:
        name = quote_ident(col["column_name"])
        udt = (col.get("udt_name") or "").lower()
        dt = (col.get("data_type") or "").lower()
        if udt == "uuid" or dt == "uuid":
            # Alias back to the original name so the JSON dict key matches.
            return f"{name}::text AS {name}"
        return name

    select_list = ", ".join(_select_expr(c) for c in cols_meta)
    order_by = quote_ident(columns[0])

    copied = 0
    offset = 0
    started = time.time()
    while offset < src_count:
        batch = src.execute(
            db,
            f"SELECT {select_list} "
            f"FROM {quote_ident(table)} "
            f"ORDER BY {order_by} "
            f"OFFSET {offset} LIMIT {batch_size}",
        )
        if not batch:
            break
        csv_payload = rows_to_csv(batch, columns)
        try:
            n = dst.import_csv(db, table, csv_payload, columns)
        except Exception:
            # Surface a sample row so we can see which value tripped
            # COPY (most often: a jsonb-vs-array column or a CSV
            # quoting edge case). 200 chars is plenty for triage and
            # doesn't drown the log on wide rows.
            sample = batch[0] if batch else {}
            print(
                f"      ! batch failed at offset={offset}, sample row keys={list(sample)[:6]}, "
                f"first 200 chars of csv={csv_payload[:200]!r}",
                flush=True,
            )
            raise
        copied += n
        offset += len(batch)
        elapsed = time.time() - started
        rate = copied / elapsed if elapsed > 0 else 0
        eta = (src_count - copied) / rate if rate > 0 else float("inf")
        print(
            f"      {table}: {copied}/{src_count} "
            f"({100 * copied / src_count:.1f}%) "
            f"@ {rate:.0f} rows/s, ETA {eta:.0f}s"
        )

    return src_count, copied


def replicate_indexes(p: Proxy, db: str, table: str, ddls: list[str]) -> None:
    for ddl in ddls:
        # Postgres' `indexdef` already includes "CREATE [UNIQUE] INDEX
        # … ON public.<tbl> …" so we can run as-is. IF NOT EXISTS is
        # appended to keep this idempotent on re-runs.
        if "IF NOT EXISTS" not in ddl.upper():
            ddl = ddl.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1)
            ddl = ddl.replace(
                "CREATE UNIQUE INDEX", "CREATE UNIQUE INDEX IF NOT EXISTS", 1
            )
        try:
            p.execute_commit(db, ddl)
        except Exception as e:
            print(f"      ! index DDL failed (continuing): {e}\n        ddl: {ddl}")


def add_primary_key(p: Proxy, db: str, table: str, pk_cols: list[str]) -> None:
    if not pk_cols:
        return
    cols_q = ", ".join(quote_ident(c) for c in pk_cols)
    # ADD CONSTRAINT will create a backing unique index automatically.
    sql = f"ALTER TABLE {quote_ident(table)} ADD PRIMARY KEY ({cols_q})"
    try:
        p.execute_commit(db, sql)
    except Exception as e:
        msg = str(e)
        if "already exists" in msg or "multiple primary keys" in msg:
            return
        print(f"      ! PK reconstruction failed (continuing): {e}")


# ── Top-level ───────────────────────────────────────────────────────


def list_user_tables(p: Proxy, db: str) -> list[str]:
    rows = p.execute(
        db,
        """SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name""",
    )
    return [r["table_name"] for r in rows]


def migrate_db(src: Proxy, dst: Proxy, db: str, batch_size: int, dry_run: bool, truncate_dst: bool) -> None:
    print(f"── DB: {db} ──")
    if not dry_run:
        dst.create_db(db)

    tables = list_user_tables(src, db)
    if not tables:
        print(f"  (no public tables — skipping)")
        return

    for table in tables:
        if not dry_run:
            existing = dst.execute(db, f"SELECT count(*) AS n FROM {quote_ident(table)}") if (
                table in list_user_tables(dst, db)
            ) else [{"n": 0}]
            if existing and existing[0]["n"] > 0:
                if truncate_dst:
                    dst.execute_commit(db, f"TRUNCATE {quote_ident(table)}")
                else:
                    print(f"  ! {table}: dst already has {existing[0]['n']} rows — pass --truncate-dst to overwrite. SKIPPING.")
                    continue

        print(f"  → {table}")
        src_n, copied = copy_table(src, dst, db, table, batch_size, dry_run)
        if dry_run:
            continue

        # Indexes + PK after the data so the load is COPY-friendly.
        pk_cols = fetch_pk_columns(src, db, table)
        if pk_cols:
            add_primary_key(dst, db, table, pk_cols)
        index_ddls = fetch_indexes(src, db, table)
        # The PK index will have shown up in pg_indexes too — skip any
        # whose name looks like the auto-PK ("<table>_pkey") since we
        # already created it via ALTER TABLE … ADD PRIMARY KEY.
        index_ddls = [d for d in index_ddls if f"{table}_pkey" not in d]
        replicate_indexes(dst, db, table, index_ddls)

        if copied != src_n:
            print(f"    ! count mismatch: src={src_n} dst={copied}")
        else:
            print(f"    ✓ {table}: {copied} rows + {len(index_ddls)} index(es)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", required=True, help="src sql-proxy base URL")
    ap.add_argument("--dst", required=True, help="dst sql-proxy base URL")
    ap.add_argument("--dbs", default="", help="comma-separated; default = all non-system")
    ap.add_argument("--batch-size", type=int, default=5000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--truncate-dst", action="store_true",
                    help="if a dst table already has rows, TRUNCATE before load (otherwise: skip)")
    args = ap.parse_args()

    proxy_key = os.environ.get("SQL_PROXY_KEY", "")
    admin_key = os.environ.get("SQL_PROXY_ADMIN_KEY", "")
    if not proxy_key:
        print("error: SQL_PROXY_KEY env var required", file=sys.stderr)
        return 2
    if not admin_key:
        print("error: SQL_PROXY_ADMIN_KEY env var required (for create-db)", file=sys.stderr)
        return 2

    src = Proxy(args.src, proxy_key, admin_key)
    dst = Proxy(args.dst, proxy_key, admin_key)

    src_dbs = src.list_dbs()
    print(f"src DBs: {src_dbs}")
    print(f"dst DBs (before): {dst.list_dbs()}")

    if args.dbs:
        targets = [d.strip() for d in args.dbs.split(",") if d.strip()]
    else:
        # Skip the maintenance DBs created by every fresh install.
        targets = [d for d in src_dbs if d not in ("hivemind", "postgres", "template0", "template1")]

    print(f"migrating: {targets}")
    if args.dry_run:
        print("(dry-run — no writes will be made)")
    print()

    for db in targets:
        try:
            migrate_db(src, dst, db, args.batch_size, args.dry_run, args.truncate_dst)
        except Exception as e:
            print(f"!! db {db} failed: {e}", file=sys.stderr)
            return 1
        print()

    print(f"dst DBs (after): {dst.list_dbs()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

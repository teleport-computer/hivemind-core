"""Admin client for the sql_proxy's /admin/tenants endpoints.

Separate from the data-plane HttpDatabase because it uses a different
auth header (X-Admin-Key) and operates on cluster-level DBs, not rows.

Only the core CVM holds SQL_PROXY_ADMIN_KEY — never the user's laptop.
"""

from __future__ import annotations

import httpx


class SqlProxyAdmin:
    """Talks to sql_proxy's /admin/tenants CRUD for tenant DB lifecycle."""

    def __init__(self, base_url: str, admin_key: str, *, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._admin_key = admin_key
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"X-Admin-Key": admin_key},
            timeout=timeout,
        )

    def _check(self, resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            try:
                data = resp.json()
                err = data.get("error", resp.text)
            except Exception:
                err = resp.text
            raise RuntimeError(
                f"sql_proxy admin error ({resp.status_code}): {err}"
            )
        return resp.json()

    def create_database(self, db_name: str) -> None:
        resp = self._client.post("/admin/tenants", json={"db_name": db_name})
        self._check(resp)

    def drop_database(self, db_name: str) -> None:
        resp = self._client.delete(f"/admin/tenants/{db_name}")
        self._check(resp)

    def list_databases(self) -> list[str]:
        resp = self._client.get("/admin/tenants")
        return self._check(resp).get("databases", [])

    def rename_database(self, old_name: str, new_name: str) -> None:
        resp = self._client.post(
            "/admin/rename-database",
            json={"old_name": old_name, "new_name": new_name},
        )
        self._check(resp)

    def migrate_tenants_to_roles(self) -> list[dict]:
        """Retrofit per-tenant Postgres roles onto pre-existing tenant DBs.

        Idempotent. Returns one result dict per tenant DB encountered.
        Raises RuntimeError when the proxy was started without a role seed.
        """
        resp = self._client.post("/admin/migrate-to-roles", timeout=120)
        return self._check(resp).get("results", [])

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


class LocalPgAdmin:
    """Direct psycopg admin for CREATE/DROP DATABASE when bypassing sql_proxy.

    Used in tests and for local deploys where core + Postgres are in the
    same CVM (direct connection instead of HTTP proxy).
    """

    def __init__(self, dsn: str):
        import re
        import psycopg
        self._dsn = dsn
        self._psycopg = psycopg
        self._re = re

    def _validate(self, name: str) -> None:
        if not self._re.match(r"^[a-z][a-z0-9_]{0,62}$", name or ""):
            raise ValueError(f"invalid db_name '{name}'")
        if name in {"postgres", "template0", "template1"}:
            raise ValueError(f"db_name '{name}' is reserved")

    def create_database(self, db_name: str) -> None:
        self._validate(db_name)
        from psycopg import sql as psql
        with self._psycopg.connect(self._dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    psql.SQL("CREATE DATABASE {}").format(
                        psql.Identifier(db_name)
                    )
                )

    def drop_database(self, db_name: str) -> None:
        self._validate(db_name)
        from psycopg import sql as psql
        with self._psycopg.connect(self._dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    psql.SQL(
                        "DROP DATABASE IF EXISTS {} WITH (FORCE)"
                    ).format(psql.Identifier(db_name))
                )

    def list_databases(self) -> list[str]:
        with self._psycopg.connect(self._dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT datname FROM pg_database "
                    "WHERE datistemplate = false "
                    "AND datname NOT IN ('postgres') "
                    "ORDER BY datname"
                )
                return [row[0] for row in cur.fetchall()]

    def rename_database(self, old_name: str, new_name: str) -> None:
        self._validate(old_name)
        self._validate(new_name)
        from psycopg import sql as psql
        with self._psycopg.connect(self._dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    psql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                        psql.Identifier(old_name),
                        psql.Identifier(new_name),
                    )
                )

    def migrate_tenants_to_roles(self) -> list[dict]:
        """LocalPgAdmin doesn't enforce per-tenant roles today.

        Layer 1 is a property of the HTTP SQL proxy; direct-psycopg deploys
        share the superuser credential already, so the migration is a
        no-op here rather than a silent failure.
        """
        return []

    def close(self) -> None:
        pass


def make_admin(database_url: str, admin_key: str) -> SqlProxyAdmin | LocalPgAdmin:
    """Pick the right admin backend based on the DSN scheme."""
    if database_url.startswith("http://") or database_url.startswith("https://"):
        if not admin_key:
            raise ValueError(
                "HIVEMIND_SQL_PROXY_ADMIN_KEY required for HTTP-proxied deploys"
            )
        return SqlProxyAdmin(database_url, admin_key)
    return LocalPgAdmin(database_url)

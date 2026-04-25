# Phala Cloud Deployment Guide

hivemind-core runs on Phala Cloud TEE infrastructure as two CVMs (Confidential Virtual Machines).

## Architecture

```
Postgres CVM (persistent, never redeployed unless necessary)
+-- db         -- postgres:16, data on encrypted volume
+-- sql-proxy  -- HTTP-to-SQL proxy, port 8080
        |
        | HTTPS (Phala auto-TLS)
        v
App CVM (can redeploy freely)
+-- hivemind-core -- port 8100
+-- dind           -- Docker-in-Docker for agent containers
```

All agents (scope, query, index, mediator) run as Docker containers inside the App CVM.

## Prerequisites

- [Phala Cloud CLI](https://docs.phala.network/developers/getting-started) installed
- Images pushed to GHCR (handled by `.github/workflows/build-images.yml` on push to `main`)
- Generate secrets before starting

## Step 0: Generate Secrets

hivemind-core is **multi-tenant**: the operator holds an admin key and mints
per-tenant API keys on demand. You don't need to pre-generate tenant keys.

```bash
# DB password
python3 -c "import secrets; print(secrets.token_urlsafe(24))"

# SQL proxy data-plane key (core ↔ sql-proxy)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# SQL proxy admin key (core ↔ sql-proxy for CREATE/DROP DATABASE)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Hivemind admin key (you ↔ core for /v1/admin/tenants)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put these in `deploy/phala/.env.postgres` and `deploy/phala/.env.core`:

```ini
# .env.postgres
DB_PASS=<db password>
SQL_PROXY_KEY=<data-plane key>
SQL_PROXY_ADMIN_KEY=<admin key>
# R2 backup config — optional
WALG_S3_PREFIX=
WALG_LIBSODIUM_KEY=
R2_ENDPOINT=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=

# .env.core
HIVEMIND_DATABASE_URL=https://<pg_cvm_id>-8080.app.phala.network
SQL_PROXY_KEY=<same data-plane key as above>
SQL_PROXY_ADMIN_KEY=<same admin key as above>
HIVEMIND_ADMIN_KEY=<hivemind admin key>
HIVEMIND_LLM_API_KEY=<OpenRouter / Anthropic key>

# Cloudflare DNS for the friendly URL (prod9 dstack-ingress).
# Token scope: Zone.DNS:Edit on the teleport.computer zone.
# Mint at https://dash.cloudflare.com/profile/api-tokens
CLOUDFLARE_API_TOKEN=cfat_...
HIVEMIND_FRIENDLY_DOMAIN=hivemind.teleport.computer
CERTBOT_EMAIL=ops@yourdomain.example
```

## Step 0.5: Cloudflare DNS / dstack-ingress prerequisites

The shipped compose runs the Phase E pattern: a `dstack-ingress` sidecar
inside the enclave issues a Let's Encrypt cert via DNS-01 on every fresh
deploy. That requires a Cloudflare API token with `Zone.DNS:Edit` scope
on the zone owning your friendly domain (default
`hivemind.teleport.computer`).

1. Create the zone in Cloudflare (or use an existing one).
2. Mint a custom token at
   <https://dash.cloudflare.com/profile/api-tokens> with permission
   `Zone › DNS › Edit` scoped to that single zone.
3. Put it in `deploy/phala/.env` as `CLOUDFLARE_API_TOKEN=cfat_...`
   (alongside the other secrets above). The token is sealed via
   `phala deploy -e` into the encrypted env channel; it never lives
   in the compose-hash-bound parts of the compose file.
4. Optional: also store it as a GitHub secret named
   `CLOUDFLARE_API_TOKEN` so the CICD relay deploy can override the
   EC2-side value for a single run (see `.github/workflows/deploy.yml`).

`dstack-ingress` itself manages the DNS records — you do **not** create
A/CNAME/TXT/CAA entries by hand. On first boot it writes:

```
CNAME hivemind.teleport.computer  → <app-id>.dstack-pha-prod9.phala.network
TXT   _dstack-app-address.hivemind.teleport.computer → <app-id>:443
CAA   hivemind.teleport.computer  pinned to Let's Encrypt
```

If the token is missing, `deploy/phala/deploy.sh` aborts in the
pre-check before any CVM changes — see the `${VAR:?...}` guard on
`CLOUDFLARE_API_TOKEN` in the compose file.

## Step 1: Deploy Postgres CVM

Edit `deploy/phala/.env.postgres`, fill in `DB_PASS` and `SQL_PROXY_KEY`:

```bash
phala deploy -n hivemind-pg \
  -c deploy/phala/docker-compose.postgres.yaml \
  -e deploy/phala/.env.postgres --wait
```

After deploy, note the CVM ID. SQL proxy is at:

```
https://<pg_cvm_id>-8080.app.phala.network
```

Verify:

```bash
curl https://<pg_cvm_id>-8080.app.phala.network/health
# {"status": "ok"}
```

## Step 2: Deploy App CVM

Edit `deploy/phala/.env.core`, fill in the SQL proxy URL and keys:

```bash
phala deploy -n hivemind-core \
  -c deploy/phala/docker-compose.core.yaml \
  -e deploy/phala/.env.core --wait
```

Verify liveness (no auth required):

```bash
curl https://hivemind.teleport.computer/v1/healthz
# {"ok": true}
```

> **Two-surface URLs (prod9, dstack-ingress).** The shipped compose ships
> the Phase E pattern: a `dstack-ingress` sidecar terminates LE-issued
> TLS for `hivemind.teleport.computer` (ACME DNS-01 via Cloudflare,
> issued *inside* the enclave), and the hivemind container itself keeps
> `HIVEMIND_ENCLAVE_TLS=1` for the raw passthrough route.
>
> - **Daily use** → `https://hivemind.teleport.computer` (LE cert,
>   normal `curl` / browser validation). This is what `hivemind init
>   --service ...` should point at by default.
> - **Tier-3 pinning** → `https://<core_cvm_id>-8100s.dstack-pha-prod9.phala.network`
>   (gateway TCP-passthrough, enclave-derived cert). The CLI auto-
>   discovers this URL from `/v1/attestation`'s `tls.pinning_url`
>   field and pins it transparently. Raw `curl` on this URL needs
>   `--cacert ~/.hivemind/enclave-tls-<fp16>.pem` (written by the CLI
>   on first connection) or `-k`.

## Step 2.5: Approve the compose_hash

Before any CLI can talk to the new CVM, the deployed image's
`compose_hash` must be approved. There are two layers:

**(a) Local trust store (always on).** On first connection to a remote
service, the CLI prompts to approve the current `compose_hash` (TOFU).
On a redeploy with a new hash, it prompts again. State lives in
`~/.hivemind/trust.json`.

```bash
hivemind trust show                          # inspect current state
hivemind trust approve <service_url>         # force-approve without prompt
hivemind trust reset --all                   # nuke and start over
```

**(b) On-chain registry (optional but ON by default).** The shipped
compose sets `HIVEMIND_APP_AUTH_CONTRACT=0x29b475…36E` (Sepolia). When
this is set, the CLI hard-rejects any hash not approved on-chain —
operators can revoke a bad deploy without touching every client. The
contract owner has to approve the hash *before* clients connect:

```bash
# Find the hash the CVM is actually running (no auth required)
curl https://hivemind.teleport.computer/v1/attestation \
  | jq -r .attestation.compose_hash
# → 77c7624144c415e55b5fc6d70d36a27f26a02a12a14b9612d00fa4547ae9bccd

# Approve it (one-time, requires the contract owner's EOA key)
PRIVATE_KEY=0x... hivemind admin approve-hash \
  77c7624144c415e55b5fc6d70d36a27f26a02a12a14b9612d00fa4547ae9bccd \
  --contract 0x29b475E6D2e10bd3266569D4c5cf27BFd4f8c36E

# Audit / revoke later
hivemind admin list-hashes  --contract 0x29b475E6D2e10bd3266569D4c5cf27BFd4f8c36E
hivemind admin revoke-hash <hash> --contract 0x29b475E6D2e10bd3266569D4c5cf27BFd4f8c36E
```

To run *without* on-chain governance, leave `HIVEMIND_APP_AUTH_CONTRACT`
empty in `.env` — the CLI then falls back to layer (a) only.

## Step 3: Provision Your First Tenant

Now that the control plane is up, mint a tenant API key. The admin key
stays on your laptop — it never gives you access to tenant data, only to
create/delete tenants.

```bash
export CORE_URL=https://hivemind.teleport.computer
export HIVEMIND_ADMIN_KEY=<admin key from Step 0>

# Via CLI
hivemind admin create-tenant \
  --service "$CORE_URL" \
  --admin-key "$HIVEMIND_ADMIN_KEY" \
  --name "first-tenant"

# Or via raw HTTP
curl -X POST "$CORE_URL/v1/admin/tenants" \
  -H "Authorization: Bearer $HIVEMIND_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "first-tenant"}'
# {"tenant_id": "t_abc123...", "api_key": "hmk_...", "db_name": "tenant_t_abc123...", ...}
```

**Save the `api_key` immediately** — the server stores only its hash.

### Rotate-on-first-use (required)

The admin who called `create-tenant` briefly saw the plaintext `hmk_...`
in the response. If the admin is not the tenant, they could (until rotation)
impersonate the tenant and read their data. The fix is mandatory immediate
rotation by the tenant:

```bash
# Tenant-side (using the key the admin handed them)
hivemind init --service "$CORE_URL" --api-key "hmk_..."
hivemind rotate-key
# → prints a brand-new hmk_... and overwrites .hivemind/config.yaml
```

After this, only the tenant's laptop + the TEE control DB know anything that
maps to their data. The admin's copy of the original key is now worthless.

**Policy recommendation**: treat any tenant key that has not been rotated as
"bootstrap-only" — do not store real data against it.

### Handing the key to the tenant

Hand the bootstrap `hmk_...` to your user (out-of-band, e.g. 1Password).
They rotate it, then use the rotated key as their normal bearer token
against `/v1/query`, `/v1/store`, etc. Their data lives in an isolated
Postgres database (`tenant_t_abc123...`) that no other tenant — and, after
rotation, not even you (the admin) — can read via this API.

List or delete tenants later:

```bash
hivemind admin list-tenants --service "$CORE_URL" --admin-key "$HIVEMIND_ADMIN_KEY"
hivemind admin delete-tenant t_abc123... --service "$CORE_URL" --admin-key "$HIVEMIND_ADMIN_KEY"
```

## Step 3.5 (optional): Migrate a Legacy Single-Tenant Database

If you have a pre-multi-tenant deploy where all data lives in a database
literally named `hivemind`, the one-shot migration script adopts it:

```bash
export CORE_URL=https://hivemind.teleport.computer
export HIVEMIND_ADMIN_KEY=<admin key>
export LEGACY_DB=hivemind            # default; override if your DB has a different name
export TENANT_NAME="migrated-legacy" # optional cosmetic label

./scripts/migrate-legacy-hivemind.sh
```

The script:

1. Generates a fresh `t_<hex>` tenant id
2. Renames the legacy DB to `tenant_<tenant_id>` via
   `POST /v1/admin/rename-database`
3. Stamps a control-plane row via `POST /v1/admin/tenants/register`
4. Prints the one-time bootstrap API key

**Important**: the rename requires no open connections to the legacy DB.
If core is already serving tenants against it, bring it to a quiet state
first (pause redeploys, stop any direct-psycopg clients). The sql-proxy
will proactively close its own pooled connection for `hivemind` as part
of the rename.

After migration, the tenant should immediately `hivemind rotate-key` as
described above.

## Step 4: Import Data (Per-Tenant)

```bash
export SQL_PROXY_URL="https://<pg_cvm_id>-8080.app.phala.network"
export SQL_PROXY_KEY="<data-plane key>"
export TENANT_DB="tenant_t_abc123..."  # from provision output

# Import SQL dump into a specific tenant DB
TENANT_DB="$TENANT_DB" ./deploy/postgres/import-data.sh sql dump.sql

# Import CSV (table must exist first)
TENANT_DB="$TENANT_DB" ./deploy/postgres/import-data.sh csv users users.csv
```

## Updating

```bash
# Redeploy core (safe, stateless)
phala deploy --cvm-id hivemind-core \
  -c deploy/phala/docker-compose.core.yaml \
  -e deploy/phala/.env.core --wait

# DO NOT casually redeploy postgres (data loss!)
```

## Troubleshooting

```bash
# Check CVM status
phala list

# View logs
phala logs hivemind-core
phala logs hivemind-pg

# Test SQL proxy
curl https://<pg_cvm_id>-8080.app.phala.network/health

# Check DB schema (maintenance DB — mostly empty)
curl -H "X-Proxy-Key: $SQL_PROXY_KEY" \
  https://<pg_cvm_id>-8080.app.phala.network/schema

# Check a tenant's schema
curl -H "X-Proxy-Key: $SQL_PROXY_KEY" \
     -H "X-Tenant-DB: tenant_t_abc123..." \
  https://<pg_cvm_id>-8080.app.phala.network/schema

# Check control DB (tenant registry)
curl -H "X-Proxy-Key: $SQL_PROXY_KEY" \
     -H "X-Tenant-DB: hivemind_control" \
  https://<pg_cvm_id>-8080.app.phala.network/schema
```

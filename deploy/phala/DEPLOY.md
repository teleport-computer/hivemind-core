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
+-- host docker socket mount for agent containers
```

All room agents (scope, query, mediator) run as Docker containers inside the App CVM.

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

Put these in a single `deploy/phala/.env` (one source of truth — feeds
both CVMs). Start from the committed template:

```bash
cp deploy/phala/.env.example deploy/phala/.env
chmod 600 deploy/phala/.env
vi deploy/phala/.env
```

Required values:

```ini
# Postgres CVM
DB_PASS=<db password>
SQL_PROXY_KEY=<data-plane key>
SQL_PROXY_ADMIN_KEY=<admin key>

# Core CVM → postgres CVM link
HIVEMIND_DATABASE_URL=https://<pg_cvm_id>-8080.<gateway>.phala.network

# Core CVM
HIVEMIND_ADMIN_KEY=<hivemind admin key>
HIVEMIND_LLM_API_KEY=<OpenRouter / Anthropic key>

# Cloudflare DNS for the friendly URL (prod9 dstack-ingress).
# Token scope: Zone.DNS:Edit on the teleport.computer zone.
# Mint at https://dash.cloudflare.com/profile/api-tokens
CLOUDFLARE_API_TOKEN=cfat_...

# R2 backups (optional but strongly recommended for the pg CVM)
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_ENDPOINT=
WALG_S3_PREFIX=s3://<bucket>/wal-g
```

The full annotated set lives in `deploy/phala/.env.example`. There are
no `.env.core` / `.env.postgres` variants — `deploy/phala/deploy.sh` and
every relay-side workflow source the same `.env`.

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

```bash
phala deploy -n hivemind-pg \
  -c deploy/phala/docker-compose.postgres.yaml \
  -e deploy/phala/.env --wait
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

After Step 1, copy the postgres CVM's gateway URL into
`HIVEMIND_DATABASE_URL` in `deploy/phala/.env`, then:

```bash
phala deploy -n hivemind-core \
  -c deploy/phala/docker-compose.core.yaml \
  -e deploy/phala/.env --wait
```

Verify liveness (no auth required):

```bash
curl https://hivemind.teleport.computer/v1/healthz
# {"ok": true}
```

### Data-preserving core repair / workspace cutover

If the current Phala API key can no longer see the public App CVM, do
**not** create a new Postgres CVM just to make deploys work. Production
tenant data, including watch-history rooms, lives behind the existing
`HIVEMIND_DATABASE_URL`. The safe repair is a **core-only** create/update
that reuses that URL.

Use GitHub Actions → `Deploy to Phala CVM (via EC2 relay)`:

```text
target=core
ref=main
image_sha=<short sha whose images were built>
node_id=18                    # first-time create on prod9 only
core_name=hivemind-core
pg_name=
db_url_override=              # leave empty to preserve the existing DB
```

After the first successful create, leave `node_id` empty so future deploys
update `hivemind-core` in place. Only use `target=postgres`, `target=all`,
or `db_url_override` when you have an explicit database migration plan and
a verified backup.

> **Prod9 TLS topology (dstack-ingress).** The shipped compose uses HTTP
> from the Phala gateway / `dstack-ingress` sidecar to the hivemind
> container. Public TLS still terminates at the gateway or the LE-issued
> `hivemind.teleport.computer` certificate, while service identity is
> enforced by DCAP quote verification plus on-chain compose-hash approval.
>
> - **Daily use** → `https://hivemind.teleport.computer` (LE cert,
>   normal `curl` / browser validation). This is what `hmctl init
>   --service ...` should point at by default.
> - **Raw gateway health** → `https://<app_id>-8100.dstack-pha-prod9.phala.network`
>   (gateway TLS to cleartext HTTP upstream). The older `-8100s`
>   passthrough pinning surface is not exposed by current prod9 metadata,
>   so `HIVEMIND_ENCLAVE_TLS` defaults to `0`.

## Step 2.5: Approve the compose_hash

Before any CLI can talk to the new CVM, the deployed image's
`compose_hash` must be approved. There are two layers:

**(a) Local trust store (always on).** On first connection to a remote
service, the CLI prompts to approve the current `compose_hash` (TOFU).
On a redeploy with a new hash, it prompts again. State lives in
`~/.hivemind/trust.json`.

```bash
hmctl trust show                          # inspect current state
hmctl trust approve <service_url>         # force-approve without prompt
hmctl trust reset --all                   # nuke and start over
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
PRIVATE_KEY=0x... hmctl admin hashes approve \
  77c7624144c415e55b5fc6d70d36a27f26a02a12a14b9612d00fa4547ae9bccd \
  --contract 0x29b475E6D2e10bd3266569D4c5cf27BFd4f8c36E

# Audit / revoke later
hmctl admin hashes list   --contract 0x29b475E6D2e10bd3266569D4c5cf27BFd4f8c36E
hmctl admin hashes revoke <hash> --contract 0x29b475E6D2e10bd3266569D4c5cf27BFd4f8c36E
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
hmctl admin tenants create "first-tenant" \
  --service "$CORE_URL" \
  --admin-key "$HIVEMIND_ADMIN_KEY"

# Or via raw HTTP
curl -X POST "$CORE_URL/v1/admin/tenants" \
  -H "Authorization: Bearer $HIVEMIND_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "first-tenant"}'
# {"tenant_id": "t_abc123...", "api_key": "hmk_...", "db_name": "tenant_t_abc123...", ...}
```

**Save the `api_key` immediately** — the server stores only its hash.

### Rotate-on-first-use (required)

The admin who called `tenants create` briefly saw the plaintext `hmk_...`
in the response. If the admin is not the tenant, they could (until rotation)
impersonate the tenant and read their data. The fix is mandatory immediate
rotation by the tenant:

```bash
# Tenant-side (using the key the admin handed them)
hmctl init --service "$CORE_URL" --api-key "hmk_..."
hmctl rotate-key
# → prints a brand-new hmk_... and overwrites .hivemind/config.yaml
```

After this, only the tenant's laptop + the TEE control DB know anything that
maps to their data. The admin's copy of the original key is now worthless.

**Policy recommendation**: treat any tenant key that has not been rotated as
"bootstrap-only" — do not store real data against it.

### Handing the key to the tenant

Hand the bootstrap `hmk_...` to your user (out-of-band, e.g. 1Password).
They rotate it, then use the rotated key as their normal bearer token for
room APIs such as `/v1/rooms`, `/v1/room-agents`, and `/v1/runs`. Their data
lives in an isolated Postgres database (`tenant_t_abc123...`) that no other
tenant — and, after rotation, not even you (the admin) — can read via this API.

List or delete tenants later:

```bash
hmctl admin tenants list --service "$CORE_URL" --admin-key "$HIVEMIND_ADMIN_KEY"
hmctl admin tenants delete t_abc123... --service "$CORE_URL" --admin-key "$HIVEMIND_ADMIN_KEY"
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

After migration, the tenant should immediately `hmctl rotate-key` as
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
  -e deploy/phala/.env --wait

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

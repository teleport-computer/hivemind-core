# Phala Cloud Deployment Guide

hivemind-core runs on Phala Cloud TEE infrastructure as multiple CVMs (Confidential Virtual Machines).

## Architecture

```
Postgres CVM (persistent, never redeployed unless necessary)
├── db         — postgres:16, data on encrypted volume
└── sql-proxy  — HTTP-to-SQL proxy, port 8080
        │
        │ HTTPS (Phala auto-TLS)
        ▼
Core CVM (can redeploy freely)
└── hivemind-core — port 8100
        │
        │ HTTPS
        ▼
Persistent Agent CVMs (one each, long-running)
├── scope    — port 8080
├── index    — port 8080
└── mediator — port 8080

Ephemeral Query CVMs (created per request, auto-destroyed)
└── query-base + injected source code
```

## Prerequisites

- [Phala Cloud CLI](https://docs.phala.network/developers/getting-started) installed
- Images pushed to GHCR (via GitHub Actions `workflow_dispatch`, fill version e.g. `1.0.0`)
- Generate secrets before starting

## Step 0: Generate Secrets

```bash
# DB password
python3 -c "import secrets; print(secrets.token_urlsafe(24))"

# SQL proxy shared secret
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Hivemind API key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

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

## Step 2: Deploy Persistent Agent CVMs

These are stateless, no env file needed:

```bash
phala deploy -n hivemind-scope    -c deploy/phala/docker-compose.scope.yaml    --wait
phala deploy -n hivemind-index    -c deploy/phala/docker-compose.index.yaml    --wait
phala deploy -n hivemind-mediator -c deploy/phala/docker-compose.mediator.yaml --wait
```

Note each CVM's URL:

```
https://<scope_cvm_id>-8080.app.phala.network
https://<index_cvm_id>-8080.app.phala.network
https://<mediator_cvm_id>-8080.app.phala.network
```

## Step 3: Deploy Core CVM

Edit `deploy/phala/.env.core`, fill in all URLs and keys from previous steps:

```bash
phala deploy -n hivemind-core \
  -c deploy/phala/docker-compose.core.yaml \
  -e deploy/phala/.env.core --wait
```

> **Chicken-and-egg:** First deploy won't have `HIVEMIND_PHALA_PUBLIC_URL`.
> Deploy once → note CVM ID → update `.env.core` → redeploy:
>
> ```bash
> phala deploy --cvm-id hivemind-core \
>   -c deploy/phala/docker-compose.core.yaml \
>   -e deploy/phala/.env.core --wait
> ```

Verify:

```bash
curl -H "Authorization: Bearer <api-key>" \
  https://<core_cvm_id>-8100.app.phala.network/health
```

## Step 4: Import Data

```bash
export SQL_PROXY_URL="https://<pg_cvm_id>-8080.app.phala.network"
export SQL_PROXY_KEY="<your-proxy-secret>"

# Import SQL dump
./deploy/postgres/import-data.sh sql dump.sql

# Import CSV (table must exist first)
./deploy/postgres/import-data.sh csv users users.csv
```

## Updating

```bash
# Redeploy core (safe, stateless)
phala deploy --cvm-id hivemind-core \
  -c deploy/phala/docker-compose.core.yaml \
  -e deploy/phala/.env.core --wait

# Redeploy an agent (safe, stateless)
phala deploy --cvm-id hivemind-scope \
  -c deploy/phala/docker-compose.scope.yaml --wait

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

# Check DB schema
curl -H "X-Proxy-Key: $SQL_PROXY_KEY" \
  https://<pg_cvm_id>-8080.app.phala.network/schema
```

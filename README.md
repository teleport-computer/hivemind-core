# hivemind-core

A forkable agent platform with raw Postgres and a scope-function query firewall. Apps define their own schema, access control, and query logic by registering Docker agent images.

Core provides only the irreducible primitives: raw SQL execution, Docker sandboxes, scope function enforcement, and pipeline orchestration. In production, runs inside a dstack Confidential VM where LUKS2 disk encryption and TDX memory encryption protect data-at-rest — no application-level encryption needed.

## Quickstart — one command

```bash
# Prereqs: docker, uv (astral.sh/uv)
./scripts/quickstart.sh
```

That's it. The script:
1. Scaffolds `.env` (prompts for an LLM key if you don't already have one)
2. Builds the agent base image + all four default agent images in parallel
3. Starts Postgres, runs `uv sync`, boots `hivemind.server` on http://localhost:8100
4. Does a real end-to-end demo: inserts rows, registers a scope policy, runs a query, prints the answer

Pass `--no-demo` to skip step 4 and stop after the server is healthy.

To use the CLI without the `uv run` prefix, install it as a top-level tool:

```bash
uv tool install --editable .
hivemind --help
```

### Manual path (if you prefer)

```bash
uv sync --all-extras
docker compose -f scripts/docker-compose.dev.yml up -d
docker build -t hivemind-agent-base -f agents/base/Dockerfile agents/base/
docker build -t hivemind-default-index:local    agents/default-index
docker build -t hivemind-default-query:local    agents/default-query
docker build -t hivemind-default-scope:local    agents/default-scope
docker build -t hivemind-default-mediator:local agents/default-mediator
uv run python -m hivemind.server
curl http://localhost:8100/v1/health
```

## Using the CLI

The `hivemind` CLI is the fastest path from "I have an agent" to "it ran and here are the artifacts." Single command owns build → upload → poll → fetch.

```bash
# One-time: point the CLI at the server and paste your tenant key
hivemind init --api-key hmk_...   # mint one via `hivemind admin create-tenant`

# Load a dataset (SQL dump / CSV / JSONL) into Postgres via /v1/store
hivemind load dump.sql
hivemind load users.csv --table users
hivemind load events.jsonl --table events

# Browse what the service already has
hivemind agents                 # list registered agents
hivemind runs                   # list recent runs
hivemind runs <run_id>          # stage timings + artifact list

# Register a scope policy (gates what queries may return)
hivemind scope --from-file policy.md
# or inline:
hivemind scope "Allow aggregate counts. Never expose individual rows."

# Run your agent — directory containing Dockerfile + agent.py, or a .tar.gz
hivemind run ./my-agent --prompt "How many documents?"
hivemind run ./my-agent --json --fetch       # scriptable + downloads artifacts

# Or use the thin natural-language query path (default query agent)
hivemind query "What tables are available?"
```

Every `--json` output is a stable, pipe-friendly record: `{status, run_id, output, mediated, artifacts:[{filename,url,...}], fetched:[...]}`. Artifact URLs remain fetchable for the server's retention window (default 24h).

## How it works

Three HTTP endpoints, one enforcement primitive:

- `POST /v1/store` — raw SQL writes against Postgres. The app owns the schema.
- `POST /v1/query` — runs the **scope → query → mediator** agent pipeline and returns the answer.
- `POST /v1/index` — runs an index agent over documents and stores structured output.

```
client ──► server ──► pipeline ──► { scope agent } ──► scope_fn
                                    { query agent } ──► SQL (filtered by scope_fn)
                                    { mediator }   ──► final answer
```

The **scope function** is the core primitive: each scope agent emits a small Python function
`scope(sql, params, rows) -> {allow, rows}` that the bridge calls on every SQL result before
the query agent sees it. AST-validated, fail-closed, runs in-process — the agent can't bypass it.

Everything else is support: Docker sandboxing (read-only rootfs, dropped caps, internal network,
bridge-only egress), budget tracking (calls + tokens per stage), and an LLM proxy that speaks
both OpenAI and Anthropic formats so SDK code works unchanged inside containers.

→ Full pipeline diagrams, security layers, and container contract: **[ARCHITECTURE.md](ARCHITECTURE.md)**

## Multi-tenancy

hivemind-core is **multi-tenant by default**. The operator (you) holds a single
**admin key** and mints per-tenant API keys on demand. Each tenant lives in an
isolated Postgres database — when deployed in a TEE (Phala dstack), even the
admin cannot read tenant data via the API.

```bash
# One-time: set HIVEMIND_ADMIN_KEY in .env (quickstart.sh does this for you)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Provision a tenant (returns api_key once — store it immediately)
hivemind admin create-tenant --name "alice-corp"
# → {"tenant_id": "t_abc...", "api_key": "hmk_...", "db_name": "tenant_t_abc..."}

# List / delete
hivemind admin list-tenants
hivemind admin delete-tenant t_abc...
```

Hand the `hmk_...` key to the tenant — they use it as their normal
`hivemind init --api-key` and everything else works unchanged.

**First action for every new tenant: rotate the key.** The admin who
minted it briefly saw the plaintext in the API response. Rotation issues
a fresh key (the admin's copy stops working immediately) and updates
`.hivemind/config.yaml` automatically:

```bash
hivemind init --api-key hmk_bootstrap_...
hivemind rotate-key
# → prints a fresh hmk_... that only the TEE + the tenant know
```

Treat any tenant key that has not been rotated as bootstrap-only.

### Remote / TEE deployment

The examples above assume a local server (`http://localhost:8100`). To
talk to a Phala CVM instead, pass `--service https://<cvm>-8100s.<gateway>`
to every command (or set it once via `hivemind init --service ...`).

Two extra ceremonies kick in for remote deploys, both documented in
**[deploy/phala/DEPLOY.md](deploy/phala/DEPLOY.md)**:

- **Compose-hash trust prompt** — first connection asks you to approve
  the deployed image's hash (TOFU). Manage it with `hivemind trust
  {show,approve,reset}`.
- **On-chain approval** — if `HIVEMIND_APP_AUTH_CONTRACT` is set on the
  server (default in the shipped compose), the contract owner must
  `hivemind admin approve-hash <hash>` once before any client can
  connect. Run `curl $URL/v1/attestation | jq .attestation.compose_hash`
  to find the hash to approve.

## Configuration

Settings load from `.env` with the `HIVEMIND_` prefix. The ones you actually touch:

| Variable | Default | Notes |
|---|---|---|
| `HIVEMIND_DATABASE_URL` | `postgresql://hivemind:dev@localhost:5432/postgres` | Postgres DSN — point at the maintenance DB so tenant DBs can be auto-created |
| `HIVEMIND_ADMIN_KEY` | — | Enables `/v1/admin/tenants` so you can mint tenant keys. Required for multi-tenant use |
| `HIVEMIND_SQL_PROXY_ADMIN_KEY` | — | Matches `SQL_PROXY_ADMIN_KEY` on sql-proxy — needed only for HTTP-proxy deploys |
| `HIVEMIND_CONTROL_DATABASE` | `hivemind_control` | Metadata DB (tenant IDs, hashed keys). Auto-created on first boot |
| `HIVEMIND_TENANT_CACHE_SIZE` | `32` | LRU cache of per-tenant Hivemind instances |
| `HIVEMIND_HOST` / `HIVEMIND_PORT` | `127.0.0.1` / `8100` | Server bind |
| `HIVEMIND_LLM_API_KEY` | — | LLM provider key (bridge passes LLM calls through) |
| `HIVEMIND_LLM_BASE_URL` | `https://openrouter.ai/api/v1` | LLM API base URL |
| `HIVEMIND_LLM_MODEL` | `anthropic/claude-sonnet-4.5` | Default model |
| `HIVEMIND_MAX_LLM_CALLS` / `HIVEMIND_MAX_TOKENS` | `50` / `200000` | Per-run budget caps |
| `HIVEMIND_AGENT_TIMEOUT` | `300` | Seconds before an agent run is killed |
| `HIVEMIND_AUTOLOAD_DEFAULT_AGENTS` | `true` | Register defaults from `HIVEMIND_DEFAULT_*_IMAGE` at startup |

Container hardening (`HIVEMIND_CONTAINER_*`), network controls (`HIVEMIND_DOCKER_*`,
`HIVEMIND_ENFORCE_BRIDGE_ONLY_EGRESS*`), and the full default-agent autoload block live in
[`.env.example`](.env.example). Defaults are safe — don't change them unless you've read the code.

## Uploading Agents

Agents are Docker containers. Upload source files as a tarball — the server builds the image:

```bash
# Create agent source directory with a Dockerfile
mkdir my-agent && cd my-agent
cat > Dockerfile <<'EOF'
FROM python:3.12-slim
RUN pip install httpx
COPY . /app
WORKDIR /app
CMD ["python", "agent.py"]
EOF
cat > agent.py <<'EOF'
import os, httpx
# ... your agent logic using BRIDGE_URL and SESSION_TOKEN ...
print("Agent output goes to stdout")
EOF

# Pack and upload
tar czf ../agent.tar.gz .
curl -X POST http://localhost:8100/v1/agents/upload \
  -F "name=my-agent" \
  -F "archive=@../agent.tar.gz"
# Returns: {"agent_id": "abc123", "name": "my-agent", "files_extracted": 2}
```

## Agent Roles

All agents are Docker containers. Core defines four roles:

| Role | Purpose | Tools Available | Bridge Extras |
|------|---------|-----------------|---------------|
| **Index** | Process data and write indexes | execute_sql, get_schema (full read/write) | — |
| **Scope** | Write a scope function (query firewall) | execute_sql, get_schema (full read) | `/sandbox/simulate`, query-agent file inspection |
| **Query** | Query data and answer questions | execute_sql, get_schema (scoped via scope_fn) | — |
| **Mediator** | Audit/filter query output | None | — |

Agents write their output to **stdout** and exit with code 0.

## Repo layout

```
hivemind/          FastAPI server, pipeline, scope compiler, Docker bridge
  sandbox/         Agent sandbox runtime (bridge, budget, docker_runner, tape)
  cli.py           The `hivemind` CLI
agents/            Default + reference agents (see agents/examples/README.md)
  base/            Agent base Docker image (claude-agent-sdk + Node)
  default-*/       4 built-in agents: index / query / scope / mediator
  private-default-scope/  Pattern A reference: host-supplied private prompt
  examples/        Sample uploadable agents
deploy/            Production dstack CVM artifacts (postgres/, phala/)
tests/             Unit + Docker integration tests
scripts/           quickstart.sh
```

## Tests

```bash
uv run pytest tests/ --ignore=tests/test_integration_docker.py -q

# With a Postgres DSN for store/pipeline/tenant tests (use maintenance DB):
docker compose -f scripts/docker-compose.dev.yml up -d
export HIVEMIND_TEST_DATABASE_URL="postgresql://hivemind:dev@localhost:5432/postgres"
uv run pytest tests/ --ignore=tests/test_integration_docker.py -q

# Docker integration (real containers):
docker build -t hivemind-test-agent:latest -f tests/fixtures/Dockerfile.test-agent tests/fixtures/
uv run pytest tests/test_integration_docker.py -v
```

## Production deploy (Phala dstack)

Live instances:
- **hivemind-core** (friendly URL):  https://hivemind.teleport.computer
  - Raw / Tier-3 pin URL: `https://0c86afa84ff128820dc201c1549b603566aa55a1-8100s.dstack-pha-prod9.phala.network`
- **hivemind-pg** (sql proxy): `https://2181af2d134123a46613f62a0311dd1f5af984be-8080.dstack-pha-prod5.phala.network` (legacy cluster — pending migration to prod9)

The friendly URL is fronted by `dstack-ingress` (the Phase E pattern feedling and hermes both ship). It terminates LE-issued TLS inside the enclave (ACME DNS-01 via Cloudflare). Tier-3 cert pinning still works — the CLI auto-discovers the raw passthrough URL from `/v1/attestation` and verifies the enclave cert there.

```bash
phala deploy -n hivemind-pg   -c deploy/phala/docker-compose.postgres.yaml -e deploy/phala/.env.postgres --wait
phala deploy -n hivemind-core -c deploy/phala/docker-compose.core.yaml    -e deploy/phala/.env.core     --wait
```

Full deploy notes: [`deploy/phala/DEPLOY.md`](deploy/phala/DEPLOY.md).

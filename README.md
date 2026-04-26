# hivemind-core

A forkable agent platform with raw Postgres and a scope-function query firewall. Apps define their own schema, access control, and query logic by registering Docker agent images.

Core provides only the irreducible primitives: raw SQL execution, Docker sandboxes, scope function enforcement, and pipeline orchestration. In production, runs inside a dstack Confidential VM where LUKS2 disk encryption and TDX memory encryption protect data-at-rest — no application-level encryption needed.

## Install the CLI

The `hivemind` CLI is a remote client — it talks to a hivemind-core server over HTTP. You don't need to run a local server to use it; if all you want is to query the live CVM (or someone else's deployment), this is the only step.

```bash
# Prereq: uv (astral.sh/uv)
uv tool install --editable .
hivemind --help
```

Then jump to **[Using the CLI](#using-the-cli)** below.

## Run a server (only if you want one)

If you want to host your own hivemind-core (or hack on this repo), spin up the full local stack:

```bash
# Prereqs: docker, uv
./scripts/quickstart.sh
```

The script scaffolds `.env`, builds the agent base + four default agent images in parallel, starts Postgres, runs `uv sync`, boots `hivemind.server` on http://localhost:8100, and ends with a real demo run (insert rows → register scope → run query). Pass `--no-demo` to stop after the server is healthy.

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

The `hivemind` CLI is your remote client for everything: connection setup,
data loading, agent lifecycle (build → upload → poll → fetch artifacts),
delegated tokens, attestation, and operator workflows. Run
`hivemind --help` (or `hivemind <subcommand> --help`) for full flags.

### Connect

```bash
# Local server (after quickstart):
hivemind init --api-key hmk_...
# Live Phala CVM:
hivemind init --service https://hivemind.teleport.computer --api-key hmk_...
# (mint a key via `hivemind admin create-tenant` if you're the operator)

hivemind rotate-key                # bootstrap → tenant-only key, rewrites profile
hivemind attestation               # show + verify the live CVM attestation bundle
hivemind schema                    # dump the user-table schema
```

### Load and inspect data

```bash
# Load a dataset (SQL dump / CSV / JSONL) into Postgres via /v1/store
hivemind load dump.sql
hivemind load users.csv --table users
hivemind load events.jsonl --table events

# Browse what the service already has
hivemind agents                 # list registered agents
hivemind agent-rm <agent_id>    # delete one
hivemind runs                   # list recent runs
hivemind runs <run_id>          # stage timings + artifact list
```

### Register a scope policy and run agents

```bash
# Register a scope policy (gates what queries may return)
hivemind scope --from-file policy.md
hivemind scope "Allow aggregate counts. Never expose individual rows."

# Run your agent — directory containing Dockerfile + agent.py, or a .tar.gz
hivemind run ./my-agent --prompt "How many documents?"
hivemind run ./my-agent --json --fetch       # scriptable + downloads artifacts

# Thin natural-language path (default query agent + registered scope)
hivemind query "What tables are available?"
hivemind query "..." --async                  # use async submit+poll path

# Index a document
hivemind index "Q3 retro: …" --metadata '{"team":"payments"}'

# Print the bound endpoint + credentials so you can hand off to a teammate
hivemind share
```

Every `--json` output is a stable, pipe-friendly record: `{status, run_id, output, mediated, artifacts:[{filename,url,...}], fetched:[...]}`. Artifact URLs remain fetchable for the server's retention window (default 24h).

### Delegate access (capability tokens)

```bash
# Owner mints a query token bound to a scope agent (recipient cannot bypass it)
hivemind tokens issue --kind query --label "research-team" \
  --scope-agent abc123def456

# Owner mints a write token restricted to one or more tables
hivemind tokens issue --kind write --label "stream-ingest" \
  --table watch_history --table events

hivemind tokens list                # token_id + kind + status + constraints
hivemind tokens revoke <token_id>   # soft-revoke; future calls 401

# Recipient (holding hmq_…) audits what the gatekeeper actually does
hivemind scope-inspect --list-files
hivemind scope-inspect --show-file Dockerfile
```

See **[Capability Tokens](#capability-tokens-delegated-query--write)** for
the full delegation model (`hmq_` / `hmw_` prefixes, what each can do, how
the recipient pins binding via `scope-inspect`).

### Operator commands (`hivemind admin …`)

Admin-key holders manage tenants and on-chain hash approval:

```bash
hivemind admin create-tenant --name alice-corp
hivemind admin list-tenants
hivemind admin delete-tenant t_abc...
hivemind admin register-existing --name adopted --db-name my_existing_db
hivemind admin migrate-to-roles                  # one-shot: per-tenant Postgres roles
hivemind admin sweep-broken-agents               # GC orphan agent images

# On-chain governance (when HIVEMIND_APP_AUTH_CONTRACT is set)
hivemind admin approve-hash <compose_hash>
hivemind admin revoke-hash <compose_hash>
hivemind admin list-hashes
```

### Trust pins (`hivemind trust …`)

For remote/TEE deploys the CLI TOFU-pins the CVM's compose hash on first
connection. Manage it explicitly:

```bash
hivemind trust show                              # all services or one
hivemind trust approve [<service>]               # accept the current remote hash
hivemind trust reset --all                       # forget all pins (re-prompt next call)
```

### Named profiles (multi-identity on one laptop)

Profiles let you keep separate identities — admin, watch-history tenant,
alice-tenant — under different names without `cd`-ing between
directories. Each profile is a YAML file under `~/.hivemind/profiles/`
holding a `service` URL + `api_key`. Trust pins (`trust.json`,
`enclave-tls-*.pem`) live alongside and are shared across profiles.

```bash
# Create a profile per identity (each writes its own YAML file)
hivemind --profile admin          init --service https://hivemind.teleport.computer --api-key hmk_admin_...
hivemind --profile watch-history  init --service https://hivemind.teleport.computer --api-key hmk_2roP...
hivemind --profile alice          init --service https://hivemind.teleport.computer --api-key hmk_alice_...

# Use them
hivemind --profile watch-history query "how many rows in watch_history?"
hivemind --profile alice          load events.jsonl --table events

# Or pin one for the shell
export HIVEMIND_PROFILE=watch-history
hivemind query "..."

# Manage them
hivemind profile list             # * marks the active profile
hivemind profile show alice       # print the YAML
hivemind profile path             # absolute path of active profile
hivemind profile delete old-tenant
```

Profiles created with `hivemind init` (no `--profile`) live as
`default` — your existing CWD-scoped `./.hivemind/config.yaml` is
auto-migrated to `~/.hivemind/profiles/default.yaml` on first use.

## How it works

Four HTTP endpoint groups, one enforcement primitive:

- `POST /v1/store` — raw SQL writes against Postgres. The app owns the schema.
- `POST /v1/query` (and `/v1/query/submit` for async runs) — runs the **scope → query → mediator** agent pipeline and returns the answer.
- `POST /v1/index` — runs an index agent over documents and stores structured output.
- `POST /v1/tokens` (and `GET /v1/scope-attest`) — mint / list / revoke delegated capability tokens; recipients verify their binding via scope-attest. See **[Capability tokens](#capability-tokens-delegated-query--write)** below.

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
a fresh key (the admin's copy stops working immediately) and updates the
active profile's YAML automatically:

```bash
hivemind --profile alice init --api-key hmk_bootstrap_...
hivemind --profile alice rotate-key
# → prints a fresh hmk_... that only the TEE + the tenant know
#   and rewrites ~/.hivemind/profiles/alice.yaml
```

Treat any tenant key that has not been rotated as bootstrap-only.

### Capability tokens (delegated query / write)

A tenant key (`hmk_…`) is the keys-to-the-kingdom credential — it can read,
write, mint scope agents, and rotate. To let a third party use a narrow
slice of your tenant *without* sharing it, mint a **capability token**
that pins a specific capability:

| Prefix | Kind | What the holder can do | What's pinned at issue |
|---|---|---|---|
| `hmq_…` | query | submit prompts via `/v1/query`, upload their own query agent, read scope-agent files for audit | exactly one **scope agent id** — every query is forced through it |
| `hmw_…` | write | INSERT into a fixed allowlist of tables via `/v1/store` | one or more **table names** — internal `_hivemind_*` tables always rejected |

```bash
# Owner mints a write token for an upstream service streaming events.
hivemind tokens issue --kind write --label "stream-ingest" --table watch_history
# → token: hmw_…  (shown ONCE; copy now or revoke + reissue)

# Owner mints a query token bound to a scope agent (the recipient
# cannot bypass that agent — every prompt is gated through it).
hivemind tokens issue --kind query --label "research-team" \
  --scope-agent abc123def456
# → token: hmq_…

hivemind tokens list                  # token_id + kind + status + constraints
hivemind tokens revoke <token_id>     # soft-revoke, future calls 401
```

Hand the recipient just the `hmq_…` / `hmw_…` and they use it as
their `Authorization: Bearer …` (or `hivemind --profile … init --api-key …`).
The plaintext is shown exactly once at issue and only the SHA-256 hash is
persisted on the CVM — losing the plaintext means revoke + reissue.

The recipient can audit their binding before submitting work:

```bash
# Confirm "the scope agent guarding my queries is what I expected".
hivemind scope-inspect --list-files
# → prints scope_agent_id, files_count, files_digest_sha256, attestation
#   (compose_hash + app_id), and every extracted source file path.
```

The digest is a stable `sha256("<path>\0<content>\0…" sorted)` — pin it
out-of-band and re-derive it later by re-fetching files.

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
- **hivemind-pg** (sql proxy): `https://ec76f3a3947408e0f22ac52eacc52222155a9d9f-8080.dstack-pha-prod9.phala.network`

The friendly URL is fronted by `dstack-ingress` (the Phase E pattern feedling and hermes both ship). It terminates LE-issued TLS inside the enclave (ACME DNS-01 via Cloudflare). Tier-3 cert pinning still works — the CLI auto-discovers the raw passthrough URL from `/v1/attestation` and verifies the enclave cert there.

```bash
# Single env file (deploy/phala/.env) feeds both CVMs.
phala deploy -n hivemind-pg   -c deploy/phala/docker-compose.postgres.yaml -e deploy/phala/.env --wait
phala deploy -n hivemind-core -c deploy/phala/docker-compose.core.yaml    -e deploy/phala/.env --wait
```

Full deploy notes: [`deploy/phala/DEPLOY.md`](deploy/phala/DEPLOY.md).

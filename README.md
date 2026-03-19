# hivemind-core

A forkable agent platform with raw Postgres and scope-function query firewall. Apps define their own schema, access control, and query logic by registering Docker agent images.

Core provides only the irreducible primitives: raw SQL execution, Docker sandboxes, scope function enforcement, and pipeline orchestration. In production, runs inside a dstack Confidential VM where LUKS2 disk encryption and TDX memory encryption protect data-at-rest — no application-level encryption needed.

## Quickstart

```bash
# Install
uv sync --all-extras

# Start local Postgres
docker compose -f deploy/docker-compose.dev.yml up -d

# Configure
cp .env.example .env
# Edit .env — at minimum set HIVEMIND_LLM_API_KEY

# Build default local agent images (used by .env.example profile)
docker build -t hivemind-default-index:local agents/default-index
docker build -t hivemind-default-query:local agents/default-query
docker build -t hivemind-default-scope:local agents/default-scope
docker build -t hivemind-default-mediator:local agents/default-mediator

# Run
uv run python -m hivemind.server

# Verify
curl http://localhost:8100/v1/health
```

## How It Works

### System overview

```
                       ┌────────────────────────────────┐
                       │        CLIENT / CALLER          │
                       │   (curl, httpx, any HTTP client) │
                       └────────┬──────────┬─────────────┘
                                │          │
                       POST /v1/store   POST /v1/query  POST /v1/index
                                │          │
                       ┌────────▼──────────▼─────────────┐
                       │    FastAPI Server (server.py)    │
                       │    http://localhost:8100         │
                       │                                  │
                       │    Auth: Bearer HIVEMIND_API_KEY  │
                       └────────┬──────────┬─────────────┘
                                │          │
                       ┌────────▼──────────▼─────────────┐
                       │    Pipeline (pipeline.py)        │
                       │                                  │
                       │  Store: sql → execute → response │
                       │  Query: scope → query → mediator │
                       │  Index: data → index agent → out │
                       │                                  │
                       │  Tracks token budgets per stage   │
                       └──┬────────┬────────┬────────────┘
                          │        │        │
                 ┌────────▼─┐  ┌───▼───┐  ┌─▼────────────┐
                 │ Database  │  │Agent  │  │Sandbox       │
                 │           │  │Store  │  │Backend       │
                 │ Postgres  │  │       │  │              │
                 │ (raw SQL) │  │CRUD + │  │Docker runner │
                 │           │  │files  │  │Bridge server │
                 └───────────┘  └───────┘  └──────────────┘
```

### Store pipeline (`POST /v1/store`)

```
Client sends:
  { sql: "INSERT INTO notes (content, team) VALUES (%s, %s)",
    params: ["Sprint retro notes...", "alpha"] }
            │
            ▼
  Database.execute_commit(sql, params)
            │
            ▼
  Response: { rows: [], rowcount: 1 }
```

### Query pipeline (`POST /v1/query`)

```
Client sends:
  { query: "What decisions were made?",
    query_agent_id: "qa-1",
    scope_agent_id: "scope-1",          ← optional
    mediator_agent_id: "med-1",         ← optional
    max_tokens: 100000 }                ← optional budget cap
            │
            ▼
═══ STAGE 0: SCOPE (optional) ═══════════════════════════════

  ┌──────────────────────────────────────────────────────┐
  │ Scope Agent Container                                │
  │                                                      │
  │ ENV: QUERY_PROMPT, QUERY_AGENT_ID                    │
  │ TOOLS: execute_sql, get_schema (FULL_READ access)    │
  │        list_query_agent_files, read_query_agent_file  │
  │ BRIDGE EXTRAS:                                       │
  │   POST /sandbox/simulate  ← run nested query          │
  │   GET  /sandbox/agents/{id}/files                      │
  │                                                      │
  │ stdout → {"scope_fn": "def scope(sql, params, rows): │
  │   return {'allow': True, 'rows': rows}"}             │
  └─────────────────────────┬────────────────────────────┘
                            │
                  scope_fn = compiled Python function
                  remaining_tokens -= scope_usage
                            │
                            ▼
═══ STAGE 1: QUERY ══════════════════════════════════════════

  ┌──────────────────────────────────────────────────────┐
  │ Query Agent Container                                │
  │                                                      │
  │ ENV: QUERY_PROMPT                                    │
  │ TOOLS: execute_sql, get_schema (SCOPED access)       │
  │                                                      │
  │   execute_sql("SELECT * FROM notes")                  │
  │     → SQL runs, then scope_fn filters results        │
  │     → Query agent sees only what scope_fn allows     │
  │                                                      │
  │ stdout → "The team decided to migrate to Stripe…"   │
  └─────────────────────────┬────────────────────────────┘
                            │
                  output text
                  remaining_tokens -= query_usage
                            │
                            ▼
═══ STAGE 2: MEDIATOR (optional) ════════════════════════════

  ┌──────────────────────────────────────────────────────┐
  │ Mediator Agent Container                             │
  │                                                      │
  │ ENV: RAW_OUTPUT, QUERY_PROMPT                        │
  │ TOOLS: none (mediator has NO data access)            │
  │                                                      │
  │ stdout → "[filtered] The team decided to migrate…"  │
  └─────────────────────────┬────────────────────────────┘
                            │
                            ▼
  Response:
    { output: "[filtered] The team decided…",
      mediated: true,
      usage: { total_tokens: 8500, max_tokens: 100000 } }
```

### Index pipeline (`POST /v1/index`)

```
Client sends:
  { data: "Q3 retro: payments migrated from PayPal to Stripe...",
    metadata: {"team": "payments"},
    index_agent_id: "idx-1" }          ← optional if default configured
            │
            ▼
  ┌──────────────────────────────────────────────────────┐
  │ Index Agent Container                                │
  │                                                      │
  │ ENV: DOCUMENT_DATA, DOCUMENT_METADATA                │
  │ TOOLS: execute_sql, get_schema (FULL_READWRITE)      │
  │                                                      │
  │ stdout → {"index_text": "...", "metadata": {...}}    │
  └─────────────────────────┬────────────────────────────┘
                            │
                            ▼
  Response:
    { index_text: "Title: Q3 Payment Migration\n...",
      metadata: { title: "...", tags: [...] },
      usage: { total_tokens: 1234, max_tokens: 200000 } }
```

### What every agent container receives

```
┌──────────── ENFORCED (all agents, cannot bypass) ─────────┐
│                                                           │
│  BRIDGE_URL         http://host.docker.internal:<port>    │
│  SESSION_TOKEN      random 32-byte urlsafe token          │
│  AGENT_ROLE         query | scope | index | mediator      │
│  BUDGET_MAX_TOKENS  remaining token budget for this run   │
│  BUDGET_MAX_CALLS   remaining call budget for this run    │
│  OPENAI_BASE_URL    http://host.docker.internal:<port>/v1 │
│  OPENAI_API_KEY     same as SESSION_TOKEN                 │
│  ANTHROPIC_BASE_URL http://host.docker.internal:<port>    │
│  ANTHROPIC_API_KEY  same as SESSION_TOKEN                 │
│                                                           │
│  The bridge is the only network exit. OpenAI/Anthropic    │
│  SDKs auto-route through the bridge with zero code changes│
└───────────────────────────────────────────────────────────┘

┌──────────── ADVISORY (role-specific, ignorable) ──────────┐
│                                                           │
│  Index:    DOCUMENT_DATA, DOCUMENT_METADATA               │
│  Scope:    QUERY_PROMPT, QUERY_AGENT_ID                   │
│  Query:    QUERY_PROMPT                                   │
│  Mediator: RAW_OUTPUT, QUERY_PROMPT                       │
│                                                           │
│  Default agents use these. Custom agents may ignore       │
│  them entirely — the agent is a Docker container that     │
│  decides its own behavior.                                │
└───────────────────────────────────────────────────────────┘
```

### Inside a container: bridge as the single exit

```
┌───────────────────────────────────────────────────────────────┐
│                 Docker Internal Network                        │
│               (hivemind-sandbox, internal=true)                │
│                                                               │
│  ┌─────────────────────┐        ┌──────────────────────────┐  │
│  │  Agent Container    │        │  Bridge Server           │  │
│  │                     │        │  (ephemeral, per-run)    │  │
│  │  read-only rootfs   │  HTTP  │                          │  │
│  │  dropped ALL caps   │◄─────►│  GET  /health            │  │
│  │  no-new-privileges  │  only  │  GET  /tools             │  │
│  │  256MB mem limit    │  exit  │  POST /tools/{name}      │  │
│  │  1 CPU, 256 PIDs    │        │  POST /llm/chat          │  │
│  │                     │        │  POST /v1/chat/completions│  │
│  │  ┌───────────────┐  │        │  POST /v1/messages       │  │
│  │  │ Agent code    │  │        │       (Anthropic compat) │  │
│  │  │ (any language │  │        │                          │  │
│  │  │  any SDK)     │  │        │  Auth: Bearer token      │  │
│  │  └───────────────┘  │        │  Budget: 429 when out    │  │
│  │                     │        │                          │  │
│  │  stdout = output    │        │  Scope-only extras:      │  │
│  └─────────────────────┘        │  POST /sandbox/simulate  │  │
│                                 │  GET  /sandbox/agents/…  │  │
│                                 └────────────┬─────────────┘  │
│       ✗ No internet                          │                │
│       ✗ No other containers                  │                │
│       ✗ Linux: iptables per-container rules  │                │
└──────────────────────────────────────────────┼────────────────┘
                                               │
                                  ┌────────────▼──────────────┐
                                  │  LLM Provider             │
                                  │  (OpenRouter, OpenAI,     │
                                  │   Anthropic, etc.)        │
                                  │                           │
                                  │  Only the bridge talks    │
                                  │  to the outside world     │
                                  └───────────────────────────┘
```

### Scope enforcement (scope function query firewall)

```
Query agent calls execute_sql("SELECT * FROM notes WHERE team = %s", ["alpha"])
       │
       ▼
1. SQL is validated (SELECT-only via sqlglot AST parsing)
2. SQL runs against FULL database → raw results
       │
       ▼
3. scope_fn(sql, params, rows) is called
   - Written by scope agent: "def scope(sql, params, rows): ..."
   - Sees the SQL, parameters, and full result set
   - Returns {"allow": True, "rows": filtered_rows}
     or     {"allow": False, "error": "reason"}
       │
       ▼
4. Query agent receives only what scope_fn returned

The agent CANNOT bypass this. The scope function is compiled from
AST-validated source (no imports, no exec/eval, no dunders) and
runs in-process with fail-closed semantics (exception → deny).
```

### Budget flow across pipeline stages

```
max_tokens = 100,000 (from request or global cap)
       │
       ▼
┌─ Stage 0: Scope Agent ─────────────────────────┐
│  Budget: 100,000 tokens                         │
│  Used: 2,000 tokens → remaining = 98,000        │
└─────────────────────────────────────────────────┘
       │  (512 tokens reserved for mediator if configured)
       ▼
┌─ Stage 1: Query Agent ──────────────────────────┐
│  Budget: 97,488 tokens                           │
│  Used: 45,000 tokens → remaining = 53,000        │
└─────────────────────────────────────────────────┘
       │
       ▼
┌─ Stage 2: Mediator Agent ───────────────────────┐
│  Budget: 53,000 tokens                           │
│  Used: 3,000 tokens                              │
│  (skipped if remaining < 128 tokens)            │
└─────────────────────────────────────────────────┘
       │
       ▼
Response: usage = { total_tokens: 50,000, max_tokens: 100,000 }

Within each stage, the bridge enforces per-call:
  Agent calls /llm/chat or /v1/chat/completions
    → Bridge checks budget.check() (preflight estimate)
    → If over limit → 429 "Budget exhausted"
    → If OK → forward to LLM provider
    → Record actual usage from provider response
    → Return response to agent
```

### Security layers

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: SCOPE FUNCTION FIREWALL (query-level)         │
│  ───────────────────────────────────────                │
│  scope_fn(sql, params, rows) → allow/deny/transform     │
│  AST-validated Python (no imports, no exec, no dunders)  │
│  Fail-closed: exception → deny                          │
│  Data-aware: sees actual results, enforces k-anonymity   │
│  Query-aware: distinguishes SELECT COUNT(*) from SELECT *│
├─────────────────────────────────────────────────────────┤
│  Layer 2: SQL VALIDATION (tool-level)                    │
│  ───────────────────────────────────────                │
│  sqlglot AST parsing: SELECT-only for query/scope agents│
│  Index agents get full DML but blocked from _hivemind_* │
├─────────────────────────────────────────────────────────┤
│  Layer 3: DOCKER ISOLATION (runtime-level)              │
│  ───────────────────────────────────────                │
│  • Read-only root filesystem (+tmpfs for /tmp)          │
│  • ALL Linux capabilities dropped                       │
│  • no-new-privileges security option                    │
│  • Internal Docker network (bridge is only exit)        │
│  • Memory limit (256MB), CPU quota (1 core)             │
│  • PID limit (256)                                      │
│  • Linux: iptables DOCKER-USER rules per container      │
│    allowing ONLY bridge IP:port, DROP everything else   │
├─────────────────────────────────────────────────────────┤
│  Layer 4: BUDGET ENFORCEMENT (bridge-level)             │
│  ───────────────────────────────────────                │
│  • max_calls and max_tokens hard caps                   │
│  • Pre-flight check before each LLM call                │
│  • 429 rejection when exhausted                         │
│  • Serialized via asyncio Lock (no races)               │
├─────────────────────────────────────────────────────────┤
│  Layer 5: ENCRYPTION AT REST (dstack CVM)               │
│  ───────────────────────────────────────                │
│  • LUKS2 full-disk encryption (AES-XTS-256)             │
│  • TDX memory encryption                                │
│  • Operator cannot read disk or RAM                     │
│  • Key derived from KMS, sealed to attestation          │
├─────────────────────────────────────────────────────────┤
│  Layer 6: MEDIATOR (soft, LLM-based)                    │
│  ───────────────────────────────────────                │
│  • Optional agent audits query output                   │
│  • Has NO tool access (can't exfiltrate data)           │
│  • Defense in depth — LLM-dependent, not a hard boundary│
└─────────────────────────────────────────────────────────┘
```

## Configuration

All settings are loaded from `.env` with the `HIVEMIND_` prefix.

| Variable | Default | Description |
|----------|---------|-------------|
| `HIVEMIND_DATABASE_URL` | `postgresql://hivemind:dev@localhost:5432/hivemind` | Postgres connection string (required) |
| `HIVEMIND_API_KEY` | — | Shared secret for HTTP auth. Required when binding non-local host |
| `HIVEMIND_HOST` | `127.0.0.1` | Server bind host |
| `HIVEMIND_PORT` | `8100` | Server bind port |
| `HIVEMIND_CORS_ALLOW_ORIGINS` | — | Comma-separated browser CORS origins. Empty = no CORS headers |
| `HIVEMIND_LLM_API_KEY` | — | API key for LLM provider (passed through bridge to agents) |
| `HIVEMIND_LLM_BASE_URL` | `https://openrouter.ai/api/v1` | LLM API base URL |
| `HIVEMIND_LLM_MODEL` | `anthropic/claude-sonnet-4.5` | Default LLM model |
| `HIVEMIND_LLM_TIMEOUT_SECONDS` | `45` | Timeout for outbound LLM provider calls from bridge |
| `HIVEMIND_BRIDGE_HOST` | `0.0.0.0` | Bridge bind host (must be reachable from Docker containers) |
| `HIVEMIND_DOCKER_HOST` | — | Optional Docker daemon host/socket |
| `HIVEMIND_DOCKER_NETWORK` | `hivemind-sandbox` | Docker network name used for sandbox containers |
| `HIVEMIND_DOCKER_NETWORK_INTERNAL` | `true` | Use Docker internal network mode when compatible with host bridge |
| `HIVEMIND_ENFORCE_BRIDGE_ONLY_EGRESS` | `true` | Linux-only: install per-container `DOCKER-USER` firewall rules allowing only bridge IP:port |
| `HIVEMIND_ENFORCE_BRIDGE_ONLY_EGRESS_FAIL_CLOSED` | `true` | Linux-only: if firewall setup fails, terminate agent run instead of continuing |
| `HIVEMIND_CONTAINER_MEMORY_MB` | `256` | Max container memory limit (MB) |
| `HIVEMIND_CONTAINER_CPU_QUOTA` | `1.0` | Container CPU quota (1.0 = one core) |
| `HIVEMIND_CONTAINER_PIDS_LIMIT` | `256` | Max process count per sandbox container |
| `HIVEMIND_CONTAINER_READ_ONLY_FS` | `true` | Run containers with read-only root filesystem |
| `HIVEMIND_CONTAINER_DROP_ALL_CAPS` | `true` | Drop all Linux capabilities inside sandbox containers |
| `HIVEMIND_CONTAINER_NO_NEW_PRIVILEGES` | `true` | Enable Docker `no-new-privileges` security option |
| `HIVEMIND_MAX_LLM_CALLS` | `50` | Global max LLM calls per agent run |
| `HIVEMIND_MAX_TOKENS` | `200000` | Global max tokens per agent run |
| `HIVEMIND_AGENT_TIMEOUT` | `300` | Max agent runtime (seconds) |
| `HIVEMIND_AUTOLOAD_DEFAULT_AGENTS` | `true` | Auto-register defaults from configured default images using stable IDs |
| `HIVEMIND_DEFAULT_INDEX_AGENT` | `default-index` | Default index agent ID |
| `HIVEMIND_DEFAULT_QUERY_AGENT` | `default-query` | Default query agent ID |
| `HIVEMIND_DEFAULT_SCOPE_AGENT` | `default-scope` | Default scope agent ID |
| `HIVEMIND_DEFAULT_MEDIATOR_AGENT` | — | Default mediator agent ID (empty = no mediation) |
| `HIVEMIND_DEFAULT_INDEX_IMAGE` | — | Docker image to autoload into `HIVEMIND_DEFAULT_INDEX_AGENT` |
| `HIVEMIND_DEFAULT_QUERY_IMAGE` | — | Docker image to autoload into `HIVEMIND_DEFAULT_QUERY_AGENT` |
| `HIVEMIND_DEFAULT_SCOPE_IMAGE` | — | Docker image to autoload into `HIVEMIND_DEFAULT_SCOPE_AGENT` |
| `HIVEMIND_DEFAULT_MEDIATOR_IMAGE` | — | Docker image to autoload into `HIVEMIND_DEFAULT_MEDIATOR_AGENT` |

If `HIVEMIND_HOST` is non-local (not `127.0.0.1`/`localhost`), startup fails unless `HIVEMIND_API_KEY` is set.

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

## Project Structure

```
hivemind/
  __init__.py          # Public API exports
  version.py           # Version resolution (from package metadata)
  config.py            # Settings (env vars)
  core.py              # Hivemind class — thin wrapper (db + pipeline + health)
  server.py            # FastAPI HTTP server
  models.py            # Pydantic request/response models (Store, Query, Index, Health)
  db.py                # Database — thin Postgres wrapper (psycopg, dict_row)
  pipeline.py          # Pipeline orchestrator (store + query pipelines)
  tools.py             # Agent tools (execute_sql, get_schema) + access levels
  scope.py             # Scope function compiler + AST validation
  sandbox/
    __init__.py        # Sandbox exports
    models.py          # AgentConfig, SandboxSettings, bridge models, SimulateRequest/Response
    settings.py        # build_sandbox_settings() — maps app config to sandbox config
    budget.py          # Per-query budget tracking (calls + tokens)
    bridge.py          # Ephemeral HTTP bridge server (LLM proxy + tools + simulation)
    docker_runner.py   # DockerRunner — container lifecycle, image extraction, cleanup
    backend.py         # SandboxBackend (implements run() interface)
    agents.py          # Agent registration + source file storage (Postgres)
    tape.py            # Tape recorder/replay for LLM call caching
agents/
  base/                # Agent SDK base Docker image
  default-common/      # Shared bridge helper (_bridge.py)
  default-index/       # Default index agent (Docker image)
  default-query/       # Default query agent (Docker image)
  default-scope/       # Default scope agent (Docker image)
  default-mediator/    # Default mediator agent (Docker image)
  examples/            # Example agents — ready to upload (see agents/examples/README.md)
    simple-query/      # Minimal schema + SQL + synthesize
    tool-loop-query/   # Agentic loop with parallel tools + auto-compaction
    metadata-scope/    # Team-based scope function
    agent-sdk-query/   # Claude Agent SDK example
    redact-mediator/   # PII redaction
deploy/
  boot.sh              # CVM entrypoint with KMS key derivation
  Dockerfile           # Production app image
  docker-compose.yaml  # Production dstack deployment
  docker-compose.dev.yml # Local Postgres for development
  contracts/           # NotarizedAppAuth.sol (on-chain governance)
  monitor/             # Monitoring TEE (event watcher + IPFS + notarizer)
  postgres/            # Production Postgres image (WAL-G backup, supercronic)
  restore.sh           # Disaster recovery
tests/
  conftest.py                # Shared fixtures
  test_scope.py              # Scope function AST validation + compilation
  test_pipeline.py           # Pipeline orchestrator tests
  test_core_store.py         # Database init tests
  test_simulate.py           # Simulation + budget carving tests
  test_sandbox_budget.py     # Budget tracking tests
  test_sandbox_backend.py    # Sandbox backend tests
  test_sandbox_bridge.py     # Bridge server tests
  test_docker_runner.py      # Docker runner tests (mocked)
  test_tape.py               # Tape recorder tests
  test_anthropic_bridge.py   # Anthropic SDK bridge tests
  test_integration_docker.py # Docker integration tests (real containers)
```

## API Reference

See [API.md](API.md) for the full API reference with all endpoints, request/response schemas, and examples.

## Tests

```bash
# Unit tests (Postgres-dependent tests skip when HIVEMIND_TEST_DATABASE_URL not set)
uv run pytest tests/ --ignore=tests/test_integration_docker.py -q

# With Postgres
docker compose -f deploy/docker-compose.dev.yml up -d
export HIVEMIND_TEST_DATABASE_URL="postgresql://hivemind:dev@localhost:5432/hivemind"
uv run pytest tests/ --ignore=tests/test_integration_docker.py -q

# Lint
uv tool run ruff check .

# Docker integration tests (requires Docker + test image)
docker build -t hivemind-test-agent:latest -f tests/fixtures/Dockerfile.test-agent tests/fixtures/
uv run pytest tests/test_integration_docker.py -v
```

# hivemind-core

A knowledge enclave service. Data goes in, only AI-digested information comes out.

Hivemind-core stores raw documents, indexes them with LLMs, and answers queries through an agent pipeline with scoped access control, soft constraints (mediator LLM audit), and hard output filters. Raw text is encrypted at rest and never exposed via any API — only paraphrased, agent-synthesized answers leave the enclave.

## Quickstart

```bash
# Install
uv sync --all-extras

# Configure
cp .env.example .env
# Edit .env — at minimum set HIVEMIND_OPENROUTER_API_KEY

# Run
uv run python -m hivemind.server

# Verify
curl http://localhost:8100/v1/health
```

## Configuration

All settings are loaded from `.env` with the `HIVEMIND_` prefix.

| Variable | Default | Description |
|----------|---------|-------------|
| `HIVEMIND_OPENROUTER_API_KEY` | — | OpenRouter API key (required for openrouter backend) |
| `HIVEMIND_OPENROUTER_MODEL` | `anthropic/claude-sonnet-4.5` | Agent model for queries |
| `HIVEMIND_INDEX_MODEL` | `anthropic/claude-haiku-4.5` | Model for auto-indexing documents |
| `HIVEMIND_MEDIATOR_MODEL` | `anthropic/claude-haiku-4.5` | Model for output auditing |
| `HIVEMIND_ANTHROPIC_API_KEY` | — | Anthropic API key (required for claude_sdk backend) |
| `HIVEMIND_AGENT_BACKEND` | `openrouter` | Agent backend (`openrouter` or `claude_sdk`) |
| `HIVEMIND_ENCRYPTION_KEY` | — | Fernet key for at-rest encryption (empty = plaintext) |
| `HIVEMIND_DB_PATH` | `./hivemind.db` | SQLite database path |
| `HIVEMIND_API_KEY` | — | Shared secret for HTTP auth (empty = no auth) |
| `HIVEMIND_HYDE_ENABLED` | `True` | Enable HyDE query expansion before agent search |
| `HIVEMIND_MAX_AGENT_TURNS` | `10` | Max tool-use iterations per query |
| `HIVEMIND_HOST` | `0.0.0.0` | Server bind host |
| `HIVEMIND_PORT` | `8100` | Server bind port |
| `HIVEMIND_SANDBOX_ENABLED` | `false` | Enable Docker-based sandboxed agent platform |
| `HIVEMIND_SANDBOX_BRIDGE_HOST` | `0.0.0.0` | Bridge server bind host (must be reachable from Docker containers) |
| `HIVEMIND_SANDBOX_DOCKER_NETWORK` | `hivemind-sandbox` | Docker network name (created with `internal=True`) |
| `HIVEMIND_SANDBOX_CONTAINER_MEMORY_MB` | `256` | Default container memory limit (MB) |
| `HIVEMIND_SANDBOX_CONTAINER_CPU_QUOTA` | `1.0` | Container CPU quota (1.0 = one core) |
| `HIVEMIND_SANDBOX_MAX_LLM_CALLS` | `50` | Global max LLM calls per sandbox query |
| `HIVEMIND_SANDBOX_MAX_TOKENS` | `200000` | Global max tokens per sandbox query |
| `HIVEMIND_SANDBOX_TIMEOUT` | `300` | Global max agent runtime (seconds) |

Generate an encryption key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Architecture

```
                    ┌─────────────────────────────────────────────────┐
                    │                   ENCLAVE                       │
                    │                                                 │
  POST /v1/store ──►│  text ──encrypt──► records (ciphertext)         │
                    │    │                                            │
                    │    └──► LLM index ──► record_index (plaintext)  │
                    │                       title, summary, tags      │
                    │                                                 │
  POST /v1/query ──►│  1. HyDE expansion (vocabulary hint for search) │
                    │  2. Build scoped tools (SQL WHERE whitelist)    │
                    │  3. Agent loop (search index → read records)    │
                    │  4. Mediator audit (soft constraints)           │
                    │  5. Output filters (hard constraints)           │──► answer
                    │                                                 │
                    └─────────────────────────────────────────────────┘
```

**Privacy layers (defense in depth):**

| Layer | Type | What it does | Bypassable? |
|-------|------|-------------|-------------|
| **Scope** | SQL-level | Agent tools can only access whitelisted records | No — enforced at WHERE clause |
| **Agent prompt** | LLM judgment | Agent paraphrases, refuses to leak credentials/secrets, uses judgment on sensitive data | Soft — depends on LLM |
| **Mediator** | LLM audit | Second LLM checks output against detail_level and custom constraints | Soft — depends on LLM |
| **Output filters** | Code | Regex redaction, length limits, synthesis checks | No — non-bypassable exit gate |
| **Network namespace** | Kernel (Linux) | Sandbox agents can only reach bridge IP:port, all other egress dropped | No — iptables in netns |

Hard filters always get the last say. The ordering is: scope → agent → mediator → filters.

**Docker sandbox isolation:** Each sandbox agent runs as a Docker container on an internal network (`internal=True`). The container has filesystem isolation (own rootfs, cannot read host files), network isolation (can only reach the bridge, not the internet), process isolation (PID namespace), and resource limits (memory, CPU). This prevents data exfiltration on all platforms — the Docker internal network blocks outbound internet access at the network layer.

**What's stored in the database:**
- `records.text` — encrypted ciphertext (unreadable without `HIVEMIND_ENCRYPTION_KEY`)
- `record_index` — AI-generated metadata (title, summary, tags, key_claims) in plaintext for FTS5 search

**What leaves via the API:**
- Agent-paraphrased answers, audited by the mediator, filtered by hard constraints
- Never raw source text

## Project Structure

```
hivemind/
  __init__.py       # Public API exports
  config.py         # Settings (env vars)
  core.py           # Hivemind class — store() and query() entry points
  server.py         # FastAPI HTTP server
  models.py         # Pydantic request/response models
  storage.py        # SQLite + FTS5 + Fernet encryption
  enclave.py        # Query pipeline orchestration
  prompts.py        # All LLM prompts (query agent, mediator, index, HyDE)
  indexing.py       # LLM index extraction + HyDE expansion
  filters.py        # Output filter registry and built-in filters
  tools.py          # Agent tools (search_index, read_record, list_index)
  backends/
    __init__.py     # Backend factory
    openrouter.py   # OpenRouter agent loop (tool-use with retry)
    claude_sdk.py   # Anthropic Claude SDK agent backend
  sandbox/
    __init__.py        # Sandbox exports
    models.py          # AgentConfig, SandboxSettings, bridge models
    budget.py          # Per-query budget tracking (calls + tokens)
    bridge.py          # Ephemeral HTTP bridge server (LLM proxy + tools)
    docker_runner.py   # DockerRunner — container lifecycle, isolation, cleanup
    backend.py         # SandboxBackend (implements run() interface)
    agents.py          # Agent registration CRUD (SQLite)
examples/
  sample_agent.py   # Example sandbox agent (stdlib-only Python)
tests/
  test_storage.py        # Storage + encryption unit tests
  test_filters.py        # Output filter unit tests
  test_api.py            # FastAPI endpoint unit tests
  test_agent_loop.py     # Agent loop unit tests (tool calls, compaction, error handling)
  test_sandbox_budget.py # Budget tracking tests
  test_sandbox_agents.py # Agent CRUD tests
  test_sandbox_bridge.py # Bridge server tests
  INTEGRATION_TESTS.md   # Agent-driven live integration test playbook (105 tests)
```

## Sandbox (Agent Platform)

When `HIVEMIND_SANDBOX_ENABLED=true`, users can register custom Docker images as agents. Each agent runs in an isolated Docker container with filesystem, network, process, and resource isolation. The agent communicates with hivemind through an ephemeral HTTP bridge server.

```
┌─── Hivemind Process ──────────────────────────────────────┐
│                                                            │
│  Bridge Server (ephemeral, 0.0.0.0:{port})                 │
│    ├── POST /llm/chat   → passthrough LLM proxy            │
│    │     (agent controls model, temperature, messages)      │
│    ├── POST /tools/{name} → scoped tool dispatch            │
│    └── budget enforcement (max calls, max tokens → 429)     │
│                                                            │
└──────────────────────┬─────────────────────────────────────┘
                       │
   Docker network (internal=True) — no internet access
                       │
┌──────────────────────▼─────────────────────────────────────┐
│  Agent Container                                            │
│    env: BRIDGE_URL, SESSION_TOKEN, PROMPT                   │
│    image: user-provided Docker image                        │
│    isolation: own rootfs, PID namespace, memory/CPU limits  │
│    stdout → captured as agent output                        │
│    └─► mediator audit → QueryResponse                       │
└─────────────────────────────────────────────────────────────┘
```

Each query gets its own container, bridge, and budget. Containers cannot interfere with each other, access hivemind's database, or reach the internet. Agents are Docker images — they bundle their own runtime, dependencies, and code.

**Quick start:**
```bash
# Enable sandbox (requires Docker)
export HIVEMIND_SANDBOX_ENABLED=true
uv run python -m hivemind.server

# Register a Docker agent
curl -X POST http://localhost:8100/v1/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "my-agent", "image": "myorg/my-agent:v1"}'

# Query with the agent
curl -X POST http://localhost:8100/v1/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What happened?", "query_agent_id": "<agent_id>"}'
```

**Custom indexing agents:**
```bash
# Register an indexing agent
curl -X POST http://localhost:8100/v1/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "tag-extractor", "image": "myorg/tag-extractor:v1"}'

# Store a document using the custom indexer
curl -X POST http://localhost:8100/v1/store \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Sprint 47 retro notes...",
    "user_id": "alice",
    "index_agent_id": "<agent_id>"
  }'
```

Indexing agents receive `DOCUMENT_TEXT` and have scoped access to the user's existing records, enabling cross-document metadata extraction.

## Database

SQLite with FTS5 full-text search and WAL mode. Inspect directly:

```bash
sqlite3 hivemind.db ".schema"
sqlite3 hivemind.db "SELECT id, space_id, user_id FROM records"
sqlite3 hivemind.db "SELECT record_id, title, tags FROM record_index"
sqlite3 hivemind.db "SELECT * FROM index_fts WHERE index_fts MATCH 'migration'"
```

## API Reference

See [API.md](API.md) for the full API reference with all endpoints, request/response schemas, and examples.

## Tests

```bash
# Unit tests (65 tests)
uv run pytest tests/ -q

# Integration tests — see tests/INTEGRATION_TESTS.md
# 105-test playbook across 11 phases (health, indexing, scope isolation,
# adversarial attacks, output filters, mediator, query quality, index
# management, encryption, edge cases, sandbox)
# Designed to be run by a Claude Code agent against a live server.
```

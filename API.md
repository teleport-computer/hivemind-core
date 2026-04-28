# Hivemind-Core API Reference

This document covers:
- Public HTTP API (`/v1/*`) used by clients
- Internal bridge API used by Docker agents at runtime

Base URL (default): `http://localhost:8100`

## Authentication

Hivemind-core is multi-tenant. Auth is always on. There are three kinds
of credentials, distinguishable by prefix so a single `Authorization`
header works for all of them:

| Prefix | Kind | Scope |
|---|---|---|
| `hmk_…` | **tenant owner** | full access to the tenant's DB and pipeline; can mint capability tokens, rotate the key, and run every public endpoint |
| `hmq_…` | **query capability** | only `/v1/query/run/submit`, `/v1/query-agents/submit`, `/v1/agents/{id}/attest` (and the `/v1/scope-attest` wrapper), and read-access to the bound scope agent's source files. Every query is forced through one pinned `scope_agent_id` (the owner picked it at mint time — the holder cannot override) |
| `<admin-key>` | **operator** | only `/v1/admin/tenants/*` (provision/list/delete/register tenants). Cannot read tenant data through this API |

```http
Authorization: Bearer hmk_<tenant-api-key>
Authorization: Bearer hmq_<query-capability-token>
Authorization: Bearer <admin-key>
```

`GET /v1/health`, `GET /v1/healthz`, and `GET /v1/attestation` never
require auth.

To mint a tenant key: `POST /v1/admin/tenants` (see below) or `hivemind
admin tenants create`. To mint capability tokens: `POST /v1/tokens` or
`hivemind tokens issue` — see **[Capability Tokens](#capability-tokens)**.

## Conventions And Gotchas

- IDs (`agent_id`) are opaque strings (currently 12-char hex).
- Most endpoints use JSON. `POST /v1/agents/upload` uses `multipart/form-data`.
- Validation errors return `422` (Pydantic/FastAPI). Runtime errors return `400`/`404`/`503`/`500` with `{"detail": ...}`.
- `503 sealed`: tenant agent files are encrypted at rest under a per-tenant DEK wrapped by the owner's `hmk_` key. The DEK cache lives only in process memory, so a CVM restart wipes it. Capability-token (`hmq_`) requests that need to read encrypted data return `503` with `detail: "Tenant is sealed: ..."` until the owner makes any authenticated request and re-thaws the cache. See [ARCHITECTURE.md § Tenant Seal](ARCHITECTURE.md#tenant-seal-application-layer-encryption).

## Public API

### `GET /v1/health`

Health check (never requires auth).

**Response 200**

```json
{
  "status": "ok",
  "table_count": 5,
  "version": "0.3.0"
}
```

### `POST /v1/store`

Execute a SQL statement against the database. For write operations (INSERT, UPDATE, DELETE, CREATE TABLE, etc.).

Owner-only (`hmk_`). Capability tokens (`hmq_`) cannot reach this
endpoint — they are pinned to the query pipeline.

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `sql` | string | yes | SQL statement (min length 1) |
| `params` | array | no | Query parameters (defaults to `[]`). Use `%s` placeholders |

**Example**

```bash
curl -X POST http://localhost:8100/v1/store \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "CREATE TABLE IF NOT EXISTS notes (id SERIAL PRIMARY KEY, content TEXT, team TEXT, created_at TIMESTAMP DEFAULT NOW())"
  }'

curl -X POST http://localhost:8100/v1/store \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "INSERT INTO notes (content, team) VALUES (%s, %s)",
    "params": ["Q3 retro: decided to migrate payments from PayPal to Stripe.", "payments"]
  }'
```

**Response 200**

```json
{
  "rows": [],
  "rowcount": 1
}
```

**Common errors**
- `400` SQL execution error
- `401` unauthorized (missing / unknown / revoked token)
- `403` forbidden (capability token used outside its allowed scope)
- `422` invalid request body

### `POST /v1/query/run/submit`

Run the query pipeline: optional scope agent -> query agent -> optional
mediator. Tracked-async only — synchronous `/v1/query` was removed
because it could not survive the Phala gateway's 60s read timeout and
never produced a Phase 5 signed envelope. Returns immediately with a
`run_id`; the pipeline runs in the background and the completed
`run_store` row carries an Ed25519 attestation signature.

Accepts owner (`hmk_`) and query (`hmq_`) tokens. For query tokens the
server **forces** `scope_agent_id` to the value bound at issue — any
client-supplied `scope_agent_id` is silently overwritten so the token
cannot bypass its gatekeeper.

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | Canonical field, min length 1 |
| `query_agent_id` | string | no | Required unless default query agent configured |
| `scope_agent_id` | string | no | Scope agent writes a scope function for result filtering. **Ignored for `hmq_` tokens** (server overrides with the bound id) |
| `mediator_agent_id` | string | no | Optional output auditing/filtering |
| `max_tokens` | integer | no | Per-request cap (min 1), clamped to server global max |

Scope resolution:
1. `scope_agent_id` if provided
2. else configured default scope agent
3. else unscoped (all query results pass through)

The scope agent outputs `{"scope_fn": "def scope(sql, params, rows): ..."}` — a Python function that acts as a query firewall. Every SQL query the query agent issues has its results passed through this function.

**Example**

```bash
curl -X POST http://localhost:8100/v1/query/run/submit \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What technical decisions were made recently?",
    "query_agent_id": "default-query",
    "max_tokens": 50000
  }'
```

**Response 200**

```json
{
  "run_id": "r_abc123def456",
  "query_agent_id": "default-query",
  "scope_agent_id": null,
  "status": "pending"
}
```

Poll status with `GET /v1/agent-runs/{run_id}` (see below).

**Common errors**
- `400` no query agent configured, agent not found, invalid scope-agent output, scope function compilation error
- `401` unauthorized (missing / unknown / revoked token)
- `403` forbidden (capability token used outside its allowed scope)
- `422` validation errors (for example `max_tokens <= 0`)

### `GET /v1/agent-runs/{run_id}`

Poll a tracked async run (query, query-agent submission, or any other
run executed via `run_store`).

**Response 200**

```json
{
  "run_id": "r_abc123def456",
  "agent_id": "abc123def456",
  "status": "completed",
  "output": "...",
  "error": null,
  "artifacts": [{"filename": "...", "size_bytes": 2431, "created_at": 0}],
  "artifact_retention_seconds": 86400
}
```

`status` is one of `pending`, `running`, `completed`, `failed`. While
`pending`/`running` the body lacks `output`. On `failed` the `error`
field carries the failure reason. Artifacts are downloaded via
`GET /v1/query/runs/{run_id}/artifacts/{filename}`.

### `POST /v1/query-agents/submit`

Capability-token-friendly variant of `/v1/agents/upload` +
`/v1/query/run/submit`. A query-token holder uploads their own query
agent tarball and immediately runs it through their token's bound scope
agent. Tarball survives only for the run.

**Request**: `multipart/form-data` with `archive`, `name`, `query`, optional
`description`, `max_tokens`. Auth: owner or query token.

**Response 200**

```json
{"run_id": "r_xxxx", "agent_id": "abc123def456", "status": "pending"}
```

### `POST /v1/index`

Run the index pipeline: index agent processes document data and returns structured index fields.

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `data` | string | yes | Document content to index (min length 1) |
| `metadata` | object | no | Arbitrary metadata passed to index agent (defaults to `{}`) |
| `index_agent_id` | string | no | Required unless default index agent configured |
| `max_tokens` | integer | no | Per-request cap (min 1), clamped to server global max |

**Example**

```bash
curl -X POST http://localhost:8100/v1/index \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "data": "Q3 retro: payments migrated from PayPal to Stripe due to better APIs.",
    "metadata": {"team": "payments", "author": "alice"}
  }'
```

**Response 200**

```json
{
  "index_text": "Title: Q3 Payment Migration\nSummary: Payments team migrated from PayPal to Stripe.\nTags: payments, migration, stripe",
  "metadata": {
    "title": "Q3 Payment Migration",
    "summary": "Payments team migrated from PayPal to Stripe for better APIs and lower international fees.",
    "tags": ["payments", "migration", "stripe"],
    "key_claims": ["PayPal replaced by Stripe", "Better APIs cited as reason"]
  },
  "usage": {"total_tokens": 1234, "max_tokens": 200000}
}
```

**Common errors**
- `400` no index agent configured, agent not found, invalid agent output
- `401` unauthorized (missing / unknown / revoked token)
- `403` forbidden (capability token used outside its allowed scope)
- `422` validation errors (for example empty `data`, `max_tokens <= 0`)

### `POST /v1/admin/tenants` (admin-only)

Provision a new tenant. Creates an isolated Postgres database and returns
the fresh API key **exactly once** — the server stores only its SHA-256
hash. Save the key before the response is discarded.

**Request body**

```json
{"name": "alice-corp"}
```

**Response 200**

```json
{
  "tenant_id": "t_abc123def456",
  "api_key": "hmk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "db_name": "tenant_t_abc123def456",
  "name": "alice-corp"
}
```

### `POST /v1/admin/tenants/register` (admin-only)

Adopt a pre-populated Postgres database as a tenant (does NOT touch the DB
itself — assumes tables are already set up).

**Request body**

```json
{"name": "adopted", "db_name": "my_existing_db", "api_key": null}
```

Set `api_key` to reuse a specific key, or omit to generate a fresh one.

### `GET /v1/admin/tenants` (admin-only)

List all tenants with metadata (id, name, db_name, created_at, suspended).
Never returns plaintext API keys.

### `DELETE /v1/admin/tenants/{tenant_id}` (admin-only)

Drop the tenant's database, evict from cache, and remove the control-plane
row. Irreversible.

## Capability Tokens

Owner-only endpoints to issue / list / revoke delegated tokens. Plaintext
is shown exactly once at issue — only the SHA-256 hash is persisted, so
losing the plaintext means revoke + reissue. Auth: tenant owner (`hmk_`).

### `POST /v1/tokens` (owner-only)

Mint a capability token.

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `kind` | string | no | Defaults to `"query"`; only `"query"` is currently accepted |
| `label` | string | no | Free-form bookkeeping label |
| `constraints` | object | yes | `{"scope_agent_id": "..."}` — recipient is forced through that scope agent |

**Example**

```bash
# Query token bound to a scope agent
curl -X POST $BASE/v1/tokens \
  -H "Authorization: Bearer $OWNER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "query",
    "label": "research-team",
    "constraints": {"scope_agent_id": "abc123def456"}
  }'
```

**Response 200**

```json
{
  "token_id": "1f4a9b2c8e0d",
  "kind": "query",
  "label": "research-team",
  "constraints": {"scope_agent_id": "abc123def456"},
  "token": "hmq_..."
}
```

`token` (plaintext) is shown **exactly once** here. Persist it now or
revoke + reissue.

**Common errors**
- `400` invalid kind / missing required constraint
- `404` unknown tenant id (should not happen via owner key — defensive)

### `GET /v1/tokens` (owner-only)

List tokens for this tenant. Plaintext is never returned — only metadata.

**Response 200**

```json
{
  "tokens": [
    {
      "token_id": "1f4a9b2c8e0d",
      "kind": "query",
      "label": "research-team",
      "constraints": {"scope_agent_id": "abc123def456"},
      "created_at": "2026-04-25T17:00:00Z",
      "revoked_at": null
    }
  ]
}
```

### `DELETE /v1/tokens/{token_id}` (owner-only)

Soft-revoke a token by its short id. The row stays for audit (`revoked_at`
set); future requests with the plaintext are rejected at the dispatcher
with `401`. Idempotent.

`token_id` must be at least 12 hex chars (the prefix shown in `list`).

**Responses**
- `200` `{"status": "ok", "token_id": "..."}`
- `400` prefix shorter than 12 chars
- `404` no matching token

### `GET /v1/agents/{agent_id}/attest`

Canonical agent-attestation endpoint. Returns "this agent, plus the
host's TDX attestation bundle, plus a stable digest over the agent's
extracted source files, plus the resolved Docker image digest". Used by
`hivemind agents attest` and by recipients who want to pin "what code is
running" before sending work.

Auth: owner (`hmk_` — any agent in the tenant) or query (`hmq_` — only
the scope agent the token is bound to; other ids return `404`).

**Response 200**

```json
{
  "agent_id": "abc123def456",
  "agent": { "agent_id": "...", "name": "...", "image": "hivemind-scope:latest", "...": "..." },
  "files_count": 7,
  "files_digest_sha256": "fa9c…",
  "image_digest": {
    "id": "sha256:9b4f…",
    "repo_digests": ["registry.example.com/hivemind-scope@sha256:1a2b…"]
  },
  "attestation": { "attestation": { "compose_hash": "...", "app_id": "...", "...": "..." } }
}
```

- `files_digest_sha256` is `sha256("<path>\0<content>\0…" sorted by
  path)`. A recipient who fetched the files via the endpoints below can
  re-derive this hash byte-for-byte and pin it out-of-band.
- `image_digest.id` is the local content-addressable Docker `Id`
  (always present when the image is loaded). `image_digest.repo_digests`
  is the registry digest list — only populated for images that were
  pulled from / pushed to a registry. Both fields are empty when the
  Docker daemon isn't reachable from the server (fail-soft).

### `GET /v1/scope-attest`

**Wrapper** around `GET /v1/agents/{agent_id}/attest`, used by the
share/ask flow when a query-token holder doesn't have a profile or
known agent id.
The response preserves the legacy top-level `scope_agent_id` key in
addition to the canonical `agent_id`.

- For `hmq_` query tokens: `agent_id` is taken from the token binding
  (`scope_agent_id` claim).
- For `hmk_` owner keys: `?scope_agent_id=...` is **required** as a
  query parameter.

### `GET /v1/agents/{agent_id}/files`

List the source files extracted at agent-build time. Auth: owner (any
agent in the tenant) or query (only the scope agent the token is bound
to — other ids return `404`). Used to audit "what code is the gatekeeper
actually running".

**Response 200**

```json
{
  "agent_id": "abc123def456",
  "files": [
    {"path": "Dockerfile", "size_bytes": 451},
    {"path": "agent.py", "size_bytes": 2087}
  ]
}
```

### `GET /v1/agents/{agent_id}/files/{file_path}`

Read one file's content. `file_path` is path-encoded (slashes preserved).
Same visibility rules as the file-list endpoint. Returns
`text/plain; charset=utf-8`.

### `GET /v1/healthz`

Unauthenticated liveness probe. Returns `{"ok": true}` when the server
process is up — does not touch the database. Use for load-balancer
healthchecks where you don't want to expose tenant-metadata counts.

### `GET /v1/admin/schema`

Get the database schema (table names, columns, types).

**Response 200**

```json
{
  "schema": [
    {
      "table_name": "notes",
      "column_name": "id",
      "data_type": "integer",
      "column_default": "nextval('notes_id_seq'::regclass)",
      "is_nullable": "NO"
    }
  ]
}
```

### `POST /v1/agents`

Register a pre-built local Docker image as an agent.

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Human-readable name |
| `image` | string | yes | Docker image ref available in local daemon |
| `description` | string | no | Defaults to `""` |
| `entrypoint` | string or null | no | Overrides image CMD |
| `memory_mb` | integer | no | Min 16, capped by server `HIVEMIND_CONTAINER_MEMORY_MB` |
| `max_llm_calls` | integer | no | Min 1 |
| `max_tokens` | integer | no | Min 1 |
| `timeout_seconds` | integer | no | Min 1 |

Server validates Docker image availability before registration.

**Response 200**

```json
{
  "agent_id": "abc123def456",
  "name": "my-agent",
  "files_extracted": 5
}
```

**Common errors**
- `400` image missing locally
- `503` Docker daemon unavailable during validation
- `422` request validation errors

### `POST /v1/agents/upload`

Upload source archive, build Docker image on server, register resulting agent.

**Request**: `multipart/form-data`

| Field | Type | Required | Notes |
|---|---|---|---|
| `archive` | file | yes | Tar/tar.gz with `Dockerfile` |
| `name` | string | yes | Agent name |
| `description` | string | no | Defaults to `""` |
| `entrypoint` | string | no | Optional CMD override |
| `memory_mb` | integer | no | Min 16 |
| `max_llm_calls` | integer | no | Min 1 |
| `max_tokens` | integer | no | Min 1 |
| `timeout_seconds` | integer | no | Min 1 |

Archive safeguards:
- Max compressed upload: 50 MB
- Max archive entries: 2,000
- Max single file size: 15 MB
- Max extracted total size: 150 MB
- Symlinks/hardlinks and path traversal are rejected

**Example**

```bash
tar czf agent.tar.gz -C my-agent .

curl -X POST http://localhost:8100/v1/agents/upload \
  -H "Authorization: Bearer $API_KEY" \
  -F "name=my-agent" \
  -F "description=Custom query agent" \
  -F "archive=@agent.tar.gz"
```

**Response 200**

```json
{
  "agent_id": "abc123def456",
  "name": "my-agent",
  "files_extracted": 3
}
```

### `GET /v1/agents`

List registered agents.

**Response 200**

```json
[
  {
    "agent_id": "abc123def456",
    "name": "my-agent",
    "description": "Custom query agent",
    "image": "hivemind-agent-abc123def456:latest",
    "entrypoint": null,
    "memory_mb": 256,
    "max_llm_calls": 20,
    "max_tokens": 100000,
    "timeout_seconds": 120
  }
]
```

### `GET /v1/agents/{agent_id}`

Get one agent config.

**Responses**
- `200` same schema as list item
- `404` `{"detail": "Agent not found"}`

### `DELETE /v1/agents/{agent_id}`

Delete agent config and extracted source files.

**Responses**
- `200` `{"status": "ok"}`
- `404` `{"detail": "Agent not found"}`

## Internal Bridge API (Agent Runtime)

Each running Docker agent talks to an ephemeral bridge server.

Auth for bridge endpoints (except `/health`):

```http
Authorization: Bearer <SESSION_TOKEN>
```

Or (Anthropic SDK style):

```http
x-api-key: <SESSION_TOKEN>
```

Agents automatically receive `BRIDGE_URL`, `SESSION_TOKEN`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `ANTHROPIC_BASE_URL`, and `ANTHROPIC_API_KEY` in env vars.

### Common Endpoints (All Agent Roles)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness + current budget summary |
| `GET` | `/tools` | Tool schemas (OpenAI function format) |
| `POST` | `/tools/{tool_name}` | Invoke tool with `{"arguments": {...}}` |
| `POST` | `/llm/chat` | LLM proxy with budget enforcement |
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat completions proxy |
| `POST` | `/v1/messages` | Anthropic-compatible messages proxy |
| `POST` | `/v1/messages/count_tokens` | Anthropic-compatible token counting (no budget charge) |

`POST /llm/chat` request fields:
- `messages` (required)
- `model` (optional override)
- `max_tokens` (default 4096, max 16384)
- `temperature`, `top_p` (optional)

`POST /llm/chat` response:

```json
{
  "content": "...",
  "usage": {
    "prompt_tokens": 100,
    "completion_tokens": 50
  }
}
```

Budget exhaustion returns `429` with `{"detail": "Budget exhausted: ..."}`.

### Scope-Agent-Only Bridge Endpoints

Available only when bridge role is `scope`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/sandbox/simulate` | Nested query-agent run for allowed query agent only |
| `GET` | `/sandbox/agents/{agent_id}/files` | List extracted files for allowed query agent |
| `GET` | `/sandbox/agents/{agent_id}/files/{file_path}` | Read extracted file content |

`/sandbox/simulate` request:

```json
{
  "query_agent_id": "qa-1",
  "prompt": "What changed this week?",
  "scope_fn_source": "def scope(sql, params, rows):\n    return {'allow': True, 'rows': rows}",
  "replay_tape": null
}
```

`replay_tape` (optional): serialized tape from a previous simulation run. When provided,
the bridge replays cached LLM responses for turns where the request hash matches — these
replayed turns are free (no LLM API call, no budget charge). When tool results change
(because the scope function changed), messages diverge, the tape stops replaying, and live LLM
calls resume automatically.

`/sandbox/simulate` response:

```json
{
  "output": "...",
  "tape": [{"request_hash": "...", "response": {...}, "request_kwargs": {...}}]
}
```

`tape`: the recorded LLM request/response pairs from this run. Pass it back as
`replay_tape` in a subsequent simulation with a different `scope_fn_source` to cheaply
replay the common prefix.

### Agent Tools Exposed Through Bridge

Tools are role-dependent with different access levels:

| Role | Tools | Access Level |
|---|---|---|
| **Scope** | `execute_sql`, `get_schema`, `list_query_agent_files`, `read_query_agent_file` | FULL_READ — SELECT on user tables, blocked from `_hivemind_*` |
| **Query** | `execute_sql`, `get_schema` | SCOPED — SQL runs, results pass through scope_fn |
| **Index** | `execute_sql`, `get_schema` | FULL_READWRITE — full DML, blocked from `_hivemind_*` writes |
| **Mediator** | (none) | NONE — no DB access |

`execute_sql(sql, params=[])` — Execute SQL. Returns JSON string of result rows or `{rowcount: N}`.
`get_schema()` — Returns database schema (tables, columns, types).

## Python Client Example

```python
import time

import httpx

BASE = "http://localhost:8100"
API_KEY = "your-api-key"

client = httpx.Client(
    base_url=BASE,
    headers={"Authorization": f"Bearer {API_KEY}"},
    timeout=120,
)

# Create a table
client.post(
    "/v1/store",
    json={
        "sql": "CREATE TABLE IF NOT EXISTS notes (id SERIAL PRIMARY KEY, content TEXT, team TEXT)",
    },
).raise_for_status()

# Insert data
store_resp = client.post(
    "/v1/store",
    json={
        "sql": "INSERT INTO notes (content, team) VALUES (%s, %s)",
        "params": ["Sprint retro: moved internal APIs from REST to gRPC.", "backend"],
    },
)
store_resp.raise_for_status()
print(store_resp.json())  # {"rows": [], "rowcount": 1}

# Query (tracked-async — submit then poll)
submit = client.post(
    "/v1/query/run/submit",
    json={
        "query": "What decisions were made?",
        "query_agent_id": "default-query",
        "max_tokens": 50000,
    },
)
submit.raise_for_status()
run_id = submit.json()["run_id"]

while True:
    run = client.get(f"/v1/agent-runs/{run_id}").json()
    if run["status"] in ("completed", "failed"):
        break
    time.sleep(2)

print(run.get("output"))
print(run.get("error"))
```

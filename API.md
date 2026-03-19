# Hivemind-Core API Reference

This document covers:
- Public HTTP API (`/v1/*`) used by clients
- Internal bridge API used by Docker agents at runtime

Base URL (default): `http://localhost:8100`

## Authentication

If `HIVEMIND_API_KEY` is set, every public endpoint except `GET /v1/health` requires:

```http
Authorization: Bearer <your-api-key>
```

Startup safety rule: if `HIVEMIND_HOST` is non-local (not `127.0.0.1`, `localhost`, or `::1`), `HIVEMIND_API_KEY` must be set.

## Conventions And Gotchas

- IDs (`agent_id`) are opaque strings (currently 12-char hex).
- Most endpoints use JSON. `POST /v1/agents/upload` uses `multipart/form-data`.
- `POST /v1/query` canonical field is `query`. `prompt` is still accepted as a deprecated alias.
- Validation errors return `422` (Pydantic/FastAPI). Runtime errors return `400`/`404`/`503`/`500` with `{"detail": ...}`.

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
- `401` unauthorized (when API key enabled)
- `422` invalid request body

### `POST /v1/query`

Run query pipeline: optional scope agent -> query agent -> optional mediator.

**Request body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | Canonical field, min length 1 |
| `prompt` | string | no | Deprecated alias; used only if `query` missing/blank |
| `query_agent_id` | string | no | Required unless default query agent configured |
| `scope_agent_id` | string | no | Scope agent writes a scope function for result filtering |
| `mediator_agent_id` | string | no | Optional output auditing/filtering |
| `max_tokens` | integer | no | Per-request cap (min 1), clamped to server global max |

Scope resolution:
1. `scope_agent_id` if provided
2. else configured default scope agent
3. else unscoped (all query results pass through)

The scope agent outputs `{"scope_fn": "def scope(sql, params, rows): ..."}` — a Python function that acts as a query firewall. Every SQL query the query agent issues has its results passed through this function.

**Example**

```bash
curl -X POST http://localhost:8100/v1/query \
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
  "output": "Two decisions were made: migrate payments to Stripe and switch internal APIs to gRPC.",
  "mediated": false,
  "usage": {"total_tokens": 12345, "max_tokens": 50000}
}
```

**Common errors**
- `400` no query agent configured, agent not found, invalid scope-agent output, scope function compilation error
- `401` unauthorized (when API key enabled)
- `422` validation errors (for example `max_tokens <= 0`)

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
- `401` unauthorized (when API key enabled)
- `422` validation errors (for example empty `data`, `max_tokens <= 0`)

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

# Query
query_resp = client.post(
    "/v1/query",
    json={
        "query": "What decisions were made?",
        "query_agent_id": "default-query",
        "max_tokens": 50000,
    },
)
query_resp.raise_for_status()

print(query_resp.json()["output"])
print(query_resp.json()["usage"])
```

# Hivemind-Core API Reference

Base URL: `http://localhost:8100` (configurable via `HIVEMIND_HOST` and `HIVEMIND_PORT`)

## Authentication

If `HIVEMIND_API_KEY` is set, all endpoints except `GET /v1/health` require:

```
Authorization: Bearer <your-api-key>
```

## Endpoints

### `GET /v1/health`

Health check. No auth required.

**Response:**
```json
{
  "status": "ok",
  "record_count": 42,
  "version": "0.1.0"
}
```

---

### `POST /v1/store`

Store a document in the enclave. The text is encrypted at rest (if `HIVEMIND_ENCRYPTION_KEY` is set) and indexed by an LLM that extracts title, summary, tags, and key claims.

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Document text (min 1 char) |
| `space_id` | string | no | Namespace/team identifier (default: `"public"`) |
| `user_id` | string | no | Owner user ID (used for scope filtering) |
| `metadata` | object | no | Arbitrary key-value metadata |
| `index` | IndexEntry | no | Pre-computed index (skips LLM indexing if provided) |
| `index_agent_id` | string | no | Custom indexing agent ID (sandbox). Agent gets scoped access to user's data. Requires `HIVEMIND_SANDBOX_ENABLED=true` |

**IndexEntry schema:**

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | Short descriptive title |
| `summary` | string | 1-3 sentence summary |
| `tags` | list[string] | Keywords for search |
| `key_claims` | list[string] | Factual assertions from the text |
| `extra` | object | Optional extra metadata (default: `{}`) |

**Example — LLM auto-indexing:**
```bash
curl -X POST http://localhost:8100/v1/store \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "In the Q3 sprint retro, the team decided to migrate from PayPal to Stripe. Key reasons: lower transaction fees (2.9% vs 3.5%), better API docs, and native subscription billing support.",
    "space_id": "team-alpha",
    "user_id": "alice",
    "metadata": {"source": "retro-notes"}
  }'
```

**Example — pre-computed index (skips LLM call):**
```bash
curl -X POST http://localhost:8100/v1/store \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Meeting notes from the design review...",
    "space_id": "team-alpha",
    "user_id": "bob",
    "index": {
      "title": "Design Review - Notification System",
      "summary": "Team chose SSE over WebSockets for real-time notifications.",
      "tags": ["notifications", "sse", "architecture"],
      "key_claims": ["SSE chosen over WebSockets", "Simpler to implement"]
    }
  }'
```

**Example — custom indexing agent:**
```bash
curl -X POST http://localhost:8100/v1/store \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Sprint 47 retro: Decided to migrate auth service to OAuth2...",
    "user_id": "alice",
    "index_agent_id": "idx789"
  }'
```

When `index_agent_id` is set, the document is indexed by a sandboxed Docker agent instead of the built-in LLM. The indexing agent receives `DOCUMENT_TEXT` env var with the text and has scoped access to all of the user's existing records via bridge tools (`search_index`, `read_record`, `list_index`). This enables cross-document indexing — the agent can search previous records to extract consistent tags, link related decisions, etc.

**Indexing priority:** `index_agent_id` > `index` (pre-computed) > default LLM indexer. If `index_agent_id` is set, `index` is ignored.

**Response (200):**
```json
{
  "record_id": "482feb9fd696",
  "timestamp": "2026-02-08T22:01:49.774955",
  "index": {
    "title": "Design Review - Notification System",
    "summary": "Team chose SSE over WebSockets for real-time notifications.",
    "tags": ["notifications", "sse", "architecture"],
    "key_claims": ["SSE chosen over WebSockets", "Simpler to implement"],
    "extra": {}
  }
}
```

---

### `POST /v1/query`

Query the knowledge base. An agent searches the index, reads relevant records, and produces an answer. The answer passes through a mediator audit before being returned.

The query pipeline has three customizable agents:
- **Scope agent** (Stage 0): decides what records the query agent can see
- **Query agent** (Stages 1-2): searches and reads records, produces an answer
- **Mediator** (Stage 3): audits the answer against soft constraints

Each has a default implementation and can be replaced by uploading a custom agent.

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `question` | string | yes | The question to answer (min 1 char) |
| `context` | string | no | "Uploaded brain" — system-level persona/focus for the agent |
| `scope` | Scope | no | Access control whitelist (see below). Default: no restrictions |
| `soft` | SoftConstraints | no | Prompt-level guidance for agent and mediator |
| `querier_id` | string | no | ID of the querying user (passed to scoping agent as `QUERIER_ID`) |
| `query_agent_id` | string | no | If set, run query through a sandboxed Docker agent (requires `HIVEMIND_SANDBOX_ENABLED=true`). Custom query agents control their own LLM interactions — no system prompt injection, no HyDE |
| `scope_agent_id` | string | no | If set, run a scoping agent to dynamically resolve scope. The scoping agent gets full DB access and the query agent's description/image info. Requires `HIVEMIND_SANDBOX_ENABLED=true` |

**Scope resolution priority:**
1. `scope_agent_id` set → sandboxed scoping agent decides
2. Otherwise → use `scope` from request (default: `Scope()` = see everything)

**Scope schema:**

| Field | Type | Description |
|-------|------|-------------|
| `user_ids` | list[string] or null | Only records owned by these users. null = unrestricted |
| `record_ids` | list[string] or null | Only these specific records. null = unrestricted |

When both are set, both must match (AND logic). When either is null/omitted, that dimension is unrestricted. Empty list `[]` = sees nothing.

Scope is enforced at the SQL layer. The agent's tools physically cannot access out-of-scope records. This cannot be bypassed by prompt injection — the scope is baked into the SQL WHERE clauses before the agent runs.

The agent does not know it is scoped. If it searches for something outside scope, it simply gets no results or "Record not found" — same as if the record didn't exist. This prevents the agent from leaking information about access boundaries.

**SoftConstraints schema:**

| Field | Type | Description |
|-------|------|-------------|
| `detail_level` | string | Controls agent behavior and mediator audit (default: `"synthesis"`) |
| `custom_instructions` | string | Additional instructions for the agent and mediator |

`detail_level` accepts any string. Built-in values with special behavior:

| Value | Agent behavior | Mediator behavior |
|-------|---------------|-------------------|
| `"synthesis"` | Paraphrase, combine insights from multiple records | Audits for verbatim quotes and single-source claims |
| `"aggregate_only"` | Only aggregate insights, no individual data points | Audits for individual references |
| `"full"` | Include specific details, numbers, direct references | No audit (passes through) |
| Any other string | String is injected as a system prompt instruction | String becomes a mediator audit constraint |

Free-form `detail_level` examples:
```json
{"detail_level": "Only discuss security-related topics"}
{"detail_level": "Respond in formal academic tone"}
{"detail_level": "Focus on cost implications only"}
```

**Example — basic query:**
```bash
curl -X POST http://localhost:8100/v1/query \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"question": "What were the key technical decisions made recently?"}'
```

**Example — scoped query (access control):**
```bash
curl -X POST http://localhost:8100/v1/query \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What did this person work on?",
    "scope": {"user_ids": ["alice"]}
  }'
```

**Example — with soft constraints:**
```bash
curl -X POST http://localhost:8100/v1/query \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Summarize recent work",
    "soft": {
      "detail_level": "Only discuss security-related topics",
      "custom_instructions": "Respond in bullet points. Keep it under 3 sentences."
    }
  }'
```

**Example — with uploaded brain (context):**
```bash
curl -X POST http://localhost:8100/v1/query \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What should we prioritize next quarter?",
    "context": "You are a security-focused CTO. Always prioritize risk reduction and vulnerability remediation above all else."
  }'
```

**Example — with scoping agent (dynamic access control):**
```bash
curl -X POST http://localhost:8100/v1/query \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What technical decisions were made recently?",
    "querier_id": "alice",
    "scope_agent_id": "scope789"
  }'
```

When `scope_agent_id` is set, a scoping agent runs BEFORE the query agent (Stage 0). The scoping agent:
- Gets full DB access (no scope restrictions on its tools)
- Receives `QUERIER_ID` env var identifying who is asking
- Receives `PROMPT` env var with the question
- Receives `QUERY_AGENT_ID` env var (the query agent ID, or `"default"`)
- Receives `QUERY_AGENT_IMAGE` env var (the query agent's Docker image reference)
- Receives `QUERY_AGENT_DESCRIPTION` env var (the query agent's description, or a default description)
- Outputs `{"record_ids": ["abc123", ...]}` — this whitelist becomes the query agent's entire visible universe
- Uses the same sandbox infrastructure (Docker container, bridge, budget, timeout) as query agents

The scoping agent decides WHAT the query agent can see. It can also inspect HOW the data will be used (via the query agent's description and image) to make trust-informed scoping decisions. The query agent then decides what to do with what it sees.

See `examples/scoping_agent.py` for a proximity-based scoping agent with query agent awareness.

**Response (200):**
```json
{
  "answer": "Recent technical decisions include migrating payment processing to Stripe for lower fees and better API support, and adopting Server-Sent Events for the notification system.",
  "sources_used": 2,
  "source_ids": ["482feb9fd696", "a1b2c3d4e5f6"],
  "audited": true
}
```

---

### `PATCH /v1/records/{record_id}/index`

Update a record's index (title, summary, tags, key_claims). The raw text is not changed.

**Request body:** IndexEntry (same schema as in store)

**Example:**
```bash
curl -X PATCH http://localhost:8100/v1/records/482feb9fd696/index \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Updated Title",
    "summary": "Updated summary.",
    "tags": ["updated", "migration"],
    "key_claims": ["New claim"]
  }'
```

**Response (200):** `{"status": "ok"}`
**Response (404):** `{"detail": "Record not found"}`

---

### `DELETE /v1/records/{record_id}`

Delete a record and its index.

**Example:**
```bash
curl -X DELETE http://localhost:8100/v1/records/482feb9fd696 \
  -H "Authorization: Bearer $API_KEY"
```

**Response (200):** `{"status": "ok"}`
**Response (404):** `{"detail": "Record not found"}`

---

### `GET /v1/spaces`

List all spaces with record counts.

**Example:**
```bash
curl http://localhost:8100/v1/spaces \
  -H "Authorization: Bearer $API_KEY"
```

**Response (200):**
```json
[
  {"space_id": "team-alpha", "count": 4},
  {"space_id": "team-beta", "count": 2},
  {"space_id": "hr-confidential", "count": 1}
]
```

---

### `POST /v1/agents` (sandbox mode)

Register a custom Docker agent. Requires `HIVEMIND_SANDBOX_ENABLED=true`.

**Request body (JSON):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Agent name |
| `image` | string | yes | Docker image reference (e.g. `myorg/my-agent:v1`) |
| `description` | string | no | Agent description |
| `entrypoint` | string | no | Override container CMD |
| `memory_mb` | integer | no | Container memory limit in MB (default: 256) |
| `max_llm_calls` | integer | no | Max LLM calls per query (default: 20) |
| `max_tokens` | integer | no | Max tokens per query (default: 100000) |
| `timeout_seconds` | integer | no | Max runtime in seconds (default: 120) |

**Example:**
```bash
curl -X POST http://localhost:8100/v1/agents \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-agent",
    "image": "myorg/my-agent:v1",
    "description": "Custom search agent",
    "max_llm_calls": 10
  }'
```

**Response (200):** `{"agent_id": "abc123def456", "name": "my-agent"}`

---

### `GET /v1/agents` (sandbox mode)

List all registered agents.

**Response (200):**
```json
[
  {
    "agent_id": "abc123def456",
    "name": "my-agent",
    "description": "Custom search agent",
    "image": "myorg/my-agent:v1",
    "entrypoint": null,
    "memory_mb": 256,
    "max_llm_calls": 10,
    "max_tokens": 100000,
    "timeout_seconds": 120
  }
]
```

---

### `GET /v1/agents/{agent_id}` (sandbox mode)

Get details of a specific agent.

**Response (200):** Same schema as list item above.
**Response (404):** `{"detail": "Agent not found"}`

---

### `DELETE /v1/agents/{agent_id}` (sandbox mode)

Delete an agent.

**Response (200):** `{"status": "ok"}`
**Response (404):** `{"detail": "Agent not found"}`

---

## Sandbox Agent Protocol

When a query includes `query_agent_id`, the query is routed to a sandboxed Docker container instead of the built-in agent loop. The container receives environment variables and communicates with hivemind through an ephemeral HTTP bridge.

**Environment variables passed to the agent container:**

| Env var | Description |
|---------|-------------|
| `BRIDGE_URL` | Base URL of the bridge HTTP server (e.g. `http://host.docker.internal:54321`) |
| `SESSION_TOKEN` | Bearer token for bridge authentication |
| `PROMPT` | The query prompt text |
| `DOCUMENT_TEXT` | Alias for PROMPT (used by indexing agents) |
| `QUERIER_ID` | Who is asking (scoping agents only, may be empty) |
| `QUERY_AGENT_ID` | Query agent ID, or `"default"` (scoping agents only) |
| `QUERY_AGENT_IMAGE` | Docker image of the query agent (scoping agents only) |
| `QUERY_AGENT_DESCRIPTION` | Description of the query agent (scoping agents only) |

**Bridge HTTP API (available at BRIDGE_URL):**

| Method | Path | Body | Response |
|--------|------|------|----------|
| GET | /health | — | `{status, budget}` |
| GET | /tools | — | Tool schemas (OpenAI format) |
| POST | /llm/chat | `{messages, max_tokens, model?, temperature?, top_p?}` | `{content, usage}` |
| POST | /tools/{name} | `{arguments}` | `{result, error}` |

All endpoints except `/health` require `Authorization: Bearer {SESSION_TOKEN}`.

The `/llm/chat` endpoint is a passthrough proxy — the agent controls model selection, temperature, top_p, and all other parameters. The bridge just forwards to OpenRouter and enforces budget.

The agent writes its final answer to **stdout** and exits with code 0. The output then passes through the mediator audit before being returned to the caller. If the agent times out, the container is killed and a timeout error is returned. If the agent produces no stdout output, an error message is returned.

**Custom query agents vs default:** When `query_agent_id` is set, the sandbox query agent has full control over its LLM interactions — no system prompt is injected, no HyDE expansion runs. The agent calls `/llm/chat` with whatever messages, model, and parameters it wants. The only post-processing is the mediator audit (soft constraints). When no `query_agent_id` is set, the default query agent uses the built-in system prompt, HyDE search expansion, and the standard tool-calling loop.

**Budget enforcement:** The bridge tracks LLM calls and token usage. When the budget is exhausted (either the agent's per-query limit or the global cap), `/llm/chat` returns a `429` with `{"detail": "Budget exhausted: ..."}`. The agent can check remaining budget via `GET /health`.

**Docker isolation:** Each agent runs as a Docker container on an internal network (`internal=True`). This provides:

| Isolation layer | Mechanism |
|----------------|-----------|
| **Filesystem** | Container has its own rootfs — cannot read host files (hivemind.db, .env, other agents) |
| **Network** | Docker `internal=True` network — container can only reach the bridge, not the internet |
| **Process** | PID namespace — container cannot see or signal host processes |
| **Resources** | Memory limit (`memory_mb`) and CPU quota prevent resource abuse |

The `SESSION_TOKEN` is the only credential the container has, and it's ephemeral (bridge shuts down after each query, token becomes worthless). Long-lived secrets (`HIVEMIND_OPENROUTER_API_KEY`, `HIVEMIND_ENCRYPTION_KEY`, `HIVEMIND_API_KEY`) are never passed to the container.

**Security model:** The bridge only exposes the four endpoints listed above — it is not a general-purpose HTTP proxy. Tool dispatch uses the same scoped `on_tool_call` closure from the query pipeline, so scope enforcement is identical to the built-in agent. The Docker internal network prevents data exfiltration on all platforms (macOS, Linux, TEE).

## Query Pipeline

When a query is processed, the following stages run in order:

0. **Scope resolution** — if `scope_agent_id` is set, a sandboxed scoping agent runs with full DB access + query agent source visibility, outputs `record_ids` whitelist. Otherwise, the static `scope` from the request body is used (default: no restrictions).
1. **Scope enforcement + agent execution** — the query agent's tools are built with SQL WHERE clauses whitelisting only in-scope records. **Default agent:** built-in LLM tool-calling loop with HyDE search expansion, system prompt with soft constraints. **Custom agent** (`query_agent_id` set): sandboxed Docker container with full LLM control (no system prompt injection, no HyDE).
2. **Mediator audit** — a second LLM checks the agent's output against soft constraints (`detail_level`, `custom_instructions`) and rewrites the minimum necessary if any constraints are violated. Skipped in `"full"` mode.

All LLM prompts are defined in `hivemind/prompts.py`.

## Python Client Example

```python
import httpx

client = httpx.Client(
    base_url="http://localhost:8100",
    headers={
        "Authorization": "Bearer your-api-key",
        "Content-Type": "application/json",
    },
    timeout=120,  # queries can take 5-30s depending on agent turns
)

# Store a document (LLM auto-indexes it)
r = client.post("/v1/store", json={
    "text": "Sprint retro: decided to switch from REST to gRPC for 40% latency improvement.",
    "space_id": "team-alpha",
    "user_id": "alice",
})
record_id = r.json()["record_id"]

# Query with scope
r = client.post("/v1/query", json={
    "question": "What technical decisions were made?",
    "scope": {"user_ids": ["alice"]},
    "soft": {"detail_level": "full"},
})
print(r.json()["answer"])

# Query with custom detail level
r = client.post("/v1/query", json={
    "question": "What's happening across teams?",
    "scope": {"user_ids": ["alice", "bob", "charlie"]},
    "soft": {
        "detail_level": "Focus only on security implications",
        "custom_instructions": "Respond in bullet points.",
    },
})
```

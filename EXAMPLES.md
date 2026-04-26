# Hivemind-Core curl Examples

Copy-paste workflows for local development.

## 0) Start The Server

Start local Postgres and build default agent images:

```bash
docker compose -f scripts/docker-compose.dev.yml up -d

docker build -t hivemind-default-index:local agents/default-index
docker build -t hivemind-default-query:local agents/default-query
docker build -t hivemind-default-scope:local agents/default-scope
docker build -t hivemind-default-mediator:local agents/default-mediator
```

Then start the API:

```bash
uv run python -m hivemind.server
```

## 1) Shell Setup

Every public endpoint requires a tenant credential. Three prefixes — pick
whichever matches your use case:

- `hmk_…` — owner key from `hivemind admin create-tenant` (full access)
- `hmq_…` — query capability from `hivemind tokens issue --kind query …`
- `hmw_…` — write capability from `hivemind tokens issue --kind write …`

```bash
export BASE="http://localhost:8100"
export API_KEY="hmk_..."          # tenant bearer (owner)

api() {
  curl -sS -H "Authorization: Bearer $API_KEY" "$@"
}
```

`jq` is used below for parsing JSON responses.
If commands return `401 Unauthorized`, your token is wrong, revoked, or
the tenant has been deleted/suspended. `403` means the token authenticated
but isn't allowed for that endpoint/operation (e.g. write token on
`/v1/query`, write token inserting into a non-allowed table).

## 2) Health Check

```bash
api "$BASE/v1/health" | jq
```

Example response:

```json
{
  "status": "ok",
  "table_count": 2,
  "version": "0.3.0"
}
```

## 3) Ensure A Query Agent Exists

If you built default images and kept `.env.example` defaults, `default-query` should already exist.

```bash
export QUERY_AGENT="${QUERY_AGENT:-default-query}"

if api "$BASE/v1/agents/$QUERY_AGENT" | jq -e '.agent_id' >/dev/null 2>&1; then
  echo "Using query agent: $QUERY_AGENT"
else
  echo "Query agent '$QUERY_AGENT' not found. Uploading a minimal one..."

  mkdir -p /tmp/my-query-agent

  cat > /tmp/my-query-agent/Dockerfile <<'DOCKERFILE'
FROM python:3.12-slim
RUN pip install httpx
COPY . /app
WORKDIR /app
CMD ["python", "agent.py"]
DOCKERFILE

  cat > /tmp/my-query-agent/agent.py <<'PYTHON'
import json
import os

import httpx

bridge = os.environ["BRIDGE_URL"]
token = os.environ["SESSION_TOKEN"]
query = os.environ["QUERY_PROMPT"]

client = httpx.Client(
    base_url=bridge,
    headers={"Authorization": f"Bearer {token}"},
    timeout=30,
)

# Get schema
schema = client.post("/tools/get_schema", json={"arguments": {}}).json()["result"]

# Ask LLM for a SQL query
llm_resp = client.post(
    "/llm/chat",
    json={
        "messages": [
            {"role": "system", "content": f"Write a SQL SELECT query for this schema:\n{schema}\nReturn ONLY the SQL."},
            {"role": "user", "content": query},
        ],
        "max_tokens": 512,
    },
).json()
sql = llm_resp["content"].strip().removeprefix("```sql").removeprefix("```").removesuffix("```").strip()

# Execute SQL
rows = client.post("/tools/execute_sql", json={"arguments": {"sql": sql}}).json()["result"]

# Synthesize answer
llm_resp = client.post(
    "/llm/chat",
    json={
        "messages": [
            {"role": "system", "content": "Answer using only the provided data."},
            {"role": "user", "content": f"Question: {query}\n\nData:\n{rows}"},
        ],
        "max_tokens": 512,
    },
).json()

print(llm_resp["content"])
PYTHON

  tar czf /tmp/my-query-agent.tar.gz -C /tmp/my-query-agent .

  QUERY_AGENT=$(api -X POST "$BASE/v1/agents/upload" \
    -F "name=my-query-agent" \
    -F "description=Minimal SQL query agent" \
    -F "archive=@/tmp/my-query-agent.tar.gz" | jq -r '.agent_id')

  echo "Uploaded query agent: $QUERY_AGENT"
fi
```

## 4) Create Schema and Store Data

```bash
# Create a table
api -X POST "$BASE/v1/store" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "CREATE TABLE IF NOT EXISTS decisions (id SERIAL PRIMARY KEY, content TEXT NOT NULL, team TEXT, author TEXT, created_at TIMESTAMP DEFAULT NOW())"
  }' | jq

# Insert records
api -X POST "$BASE/v1/store" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "INSERT INTO decisions (content, team, author) VALUES (%s, %s, %s)",
    "params": ["Q3 retro: payments migrated from PayPal to Stripe due to better APIs and international fee savings.", "payments", "alice"]
  }' | jq

api -X POST "$BASE/v1/store" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "INSERT INTO decisions (content, team, author) VALUES (%s, %s, %s)",
    "params": ["Backend team switched internal service-to-service calls from REST to gRPC for lower latency.", "backend", "bob"]
  }' | jq

echo "Stored records"
```

## 5) Query Data

### 5.1 Basic query

```bash
api -X POST "$BASE/v1/query" \
  -H "Content-Type: application/json" \
  -d "{
    \"query\": \"What technical decisions were made recently?\",
    \"query_agent_id\": \"$QUERY_AGENT\"
  }" | jq
```

Example response:

```json
{
  "output": "Two decisions were made: migrate payments to Stripe and switch internal APIs to gRPC.",
  "mediated": false,
  "usage": {"total_tokens": 8421, "max_tokens": 200000}
}
```

### 5.2 Query with explicit token cap

```bash
api -X POST "$BASE/v1/query" \
  -H "Content-Type: application/json" \
  -d "{
    \"query\": \"Summarize decisions in one paragraph\",
    \"query_agent_id\": \"$QUERY_AGENT\",
    \"max_tokens\": 50000
  }" | jq
```

## 6) Index Data

### 6.1 Index a document

```bash
api -X POST "$BASE/v1/index" \
  -H "Content-Type: application/json" \
  -d '{
    "data": "Q3 retro: payments migrated from PayPal to Stripe due to better APIs and international fee savings.",
    "metadata": {"team": "payments", "author": "alice"}
  }' | jq
```

Example response:

```json
{
  "index_text": "Title: Q3 Payment Migration\nSummary: Payments team migrated from PayPal to Stripe.\nTags: payments, migration, stripe",
  "metadata": {
    "title": "Q3 Payment Migration",
    "summary": "Payments team migrated from PayPal to Stripe for better APIs and lower fees.",
    "tags": ["payments", "migration", "stripe"],
    "key_claims": ["PayPal replaced by Stripe"]
  },
  "usage": {"total_tokens": 1234, "max_tokens": 200000}
}
```

### 6.2 Index with explicit agent and token cap

```bash
api -X POST "$BASE/v1/index" \
  -H "Content-Type: application/json" \
  -d "{
    \"data\": \"Backend team switched from REST to gRPC for service calls.\",
    \"index_agent_id\": \"default-index\",
    \"max_tokens\": 50000
  }" | jq
```

## 7) Admin Endpoints

### 7.1 Get database schema

```bash
api "$BASE/v1/admin/schema" | jq
```

## 8) Agent CRUD Endpoints

### 8.1 List agents

```bash
api "$BASE/v1/agents" | jq
```

### 8.2 Get one agent

```bash
api "$BASE/v1/agents/$QUERY_AGENT" | jq
```

### 8.3 Register from pre-built local image

```bash
api -X POST "$BASE/v1/agents" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "prebuilt-agent",
    "image": "myorg/my-agent:v1",
    "description": "Agent from local Docker image",
    "memory_mb": 512,
    "max_llm_calls": 30,
    "max_tokens": 120000,
    "timeout_seconds": 180
  }' | jq
```

### 8.4 Delete an agent

```bash
api -X DELETE "$BASE/v1/agents/$QUERY_AGENT" | jq
```

## 9) Full Three-Stage Pipeline (Scope + Query + Mediator)

If you have separate scope/query/mediator agent tarballs:

```bash
SCOPE_ID=$(api -X POST "$BASE/v1/agents/upload" \
  -F "name=scope-agent" \
  -F "archive=@scope.tar.gz" | jq -r '.agent_id')

QUERY_ID=$(api -X POST "$BASE/v1/agents/upload" \
  -F "name=query-agent" \
  -F "archive=@query.tar.gz" | jq -r '.agent_id')

MEDIATOR_ID=$(api -X POST "$BASE/v1/agents/upload" \
  -F "name=mediator-agent" \
  -F "archive=@mediator.tar.gz" | jq -r '.agent_id')

api -X POST "$BASE/v1/query" \
  -H "Content-Type: application/json" \
  -d "{
    \"query\": \"What did the payments team decide?\",
    \"scope_agent_id\": \"$SCOPE_ID\",
    \"query_agent_id\": \"$QUERY_ID\",
    \"mediator_agent_id\": \"$MEDIATOR_ID\"
  }" | jq
```

Pipeline order:
1. Scope agent writes a scope function (query firewall)
2. Query agent runs SQL queries, results filtered through scope function
3. Mediator optionally filters/audits output

## 10) Delegate Write Access (Capability Tokens)

Use case: an upstream service streams events into one of your tables. You
don't want to hand it your `hmk_…` (it could read everything, mint more
tokens, or rotate the key). Mint a **write token** instead — it can only
INSERT into the tables you pin.

### 10.1 Owner mints the token

```bash
hivemind tokens issue \
  --kind write \
  --label "stream-ingest" \
  --table watch_history
# token: hmw_…  (shown ONCE — copy now or revoke + reissue)
# token_id: 1f4a9b2c8e0d
```

Or directly via HTTP:

```bash
curl -X POST $BASE/v1/tokens \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "write",
    "label": "stream-ingest",
    "constraints": {"allowed_tables": ["watch_history"]}
  }' | jq
```

### 10.2 Recipient uses the token

```bash
export WRITE_TOKEN="hmw_..."          # shipped to the upstream service

# INSERT into the allowed table — works
curl -X POST $BASE/v1/store \
  -H "Authorization: Bearer $WRITE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "INSERT INTO watch_history (user_id, title, watched_at) VALUES (%s, %s, %s)",
    "params": ["u123", "ep1", "2026-04-25T01:00:00Z"]
  }' | jq

# INSERT into a different table — 403
curl -X POST $BASE/v1/store \
  -H "Authorization: Bearer $WRITE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "INSERT INTO other_table (id) VALUES (1)"
  }'
# {"detail": "table 'other_table' not in write-token allowed_tables …"}

# SELECT — also 403 (write tokens are INSERT-only)
curl -X POST $BASE/v1/store \
  -H "Authorization: Bearer $WRITE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT 1"}'
# {"detail": "write-token allows only INSERT statements"}
```

### 10.3 Revoke

```bash
hivemind tokens list                            # find the token_id
hivemind tokens revoke 1f4a9b2c8e0d
# the upstream service's WRITE_TOKEN starts returning 401 immediately
```

### 10.4 Make the upstream's index agent attestable

If the upstream service is going to register its own index agent (the
one consuming `WRITE_TOKEN` to write rows), publish that agent's
attestation bundle so downstream consumers can pin it:

```bash
hivemind agent-attest $INDEX_AGENT_ID
# agent_id:            ix987…
# image:               your-org/watch-history-indexer:latest
# image.id:            sha256:7c2e…
# image.repo_digest:   registry.example.com/your-org/watch-history-indexer@sha256:8d3f…
# files_count:         4
# files_digest_sha256: a012…
# attestation:
#   compose_hash: 0c86…
#   app_id:       …
```

Anyone — not just the tenant owner — can verify a given agent has not
been tampered with by re-running `agent-attest` (or hitting
`GET /v1/agents/{id}/attest`) and comparing the four pinned values.

## 11) Delegate Query Access (Capability Tokens)

Use case: a research collaborator wants to ask questions of your data,
but you only trust a specific scope agent to gate what they can see.

### 11.1 Owner pins a scope agent and mints the query token

```bash
# (Assume scope agent already uploaded with id $SCOPE_ID — see section 9.)
hivemind tokens issue \
  --kind query \
  --label "research-collab" \
  --scope-agent "$SCOPE_ID"
# token: hmq_…
```

The token now **forces** every prompt through `$SCOPE_ID`. Any
`scope_agent_id` the recipient passes in their request body is silently
overwritten by the server.

### 11.2 Recipient verifies the binding before using it

```bash
export QUERY_TOKEN="hmq_..."

# What scope agent guards my queries? What's its source?
# (`agent-attest` is the canonical command; `scope-inspect` is the
# legacy alias and still works.)
hivemind --profile collab agent-attest --list-files \
  $(hivemind --profile collab scope-inspect --json | jq -r .scope_agent_id)
# agent_id:            abc123def456
# name:                watch-history-scope
# image:               hivemind-scope:latest
# image.id:            sha256:9b4f…
# image.repo_digest:   registry.example.com/hivemind-scope@sha256:1a2b…
# files_count:         7
# files_digest_sha256: fa9c…
# attestation:
#   compose_hash: 0c86…
#   app_id:       …
# files:
#       451  Dockerfile
#      2087  agent.py
#       …

# Read one file directly
hivemind --profile collab agent-attest abc123def456 --show-file agent.py
```

The recipient pins `files_digest_sha256`, `image_digest.id` (and
`repo_digests[0]` if present), and `attestation.compose_hash`
out-of-band (e.g. in a project README). Re-running `agent-attest` later
and seeing the same triple proves nothing about the gatekeeper has
changed — neither the source it was built from, the binary it actually
runs, nor the host it runs on.

### 11.3 Recipient submits queries (and optionally their own query agent)

```bash
# Use an existing query agent
curl -X POST $BASE/v1/query \
  -H "Authorization: Bearer $QUERY_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"query\": \"What were the top 10 most-watched titles last week?\",
    \"query_agent_id\": \"$QUERY_AGENT\"
  }" | jq

# Or upload your own query agent (single-shot, runs through the bound scope)
curl -X POST $BASE/v1/query-agents/submit \
  -H "Authorization: Bearer $QUERY_TOKEN" \
  -F "name=my-research-agent" \
  -F "archive=@my-agent.tar.gz" \
  -F "query=Top genres by watch time?" | jq
# → {"run_id": "...", "agent_id": "...", "status": "pending"}
```

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

Every public endpoint requires a tenant credential. Two prefixes — pick
whichever matches your use case:

- `hmk_…` — owner key from `hivemind admin tenants create` (full access)
- `hmq_…` — query capability from `hivemind tokens issue …`

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
`/v1/query/run/submit`, write token inserting into a non-allowed table).

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

`/v1/query/run/submit` is tracked-async only — it returns a `run_id`
immediately and the pipeline runs in the background. Poll via
`GET /v1/agent-runs/{run_id}` until status is `completed` or `failed`.

### 5.1 Basic query

```bash
RUN_ID=$(api -X POST "$BASE/v1/query/run/submit" \
  -H "Content-Type: application/json" \
  -d "{
    \"query\": \"What technical decisions were made recently?\",
    \"query_agent_id\": \"$QUERY_AGENT\"
  }" | jq -r '.run_id')

# Poll until done
while true; do
  STATUS=$(api "$BASE/v1/agent-runs/$RUN_ID" | jq -r '.status')
  [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ] && break
  sleep 2
done
api "$BASE/v1/agent-runs/$RUN_ID" | jq
```

Submission response:

```json
{
  "run_id": "r_abc123def456",
  "query_agent_id": "default-query",
  "scope_agent_id": null,
  "status": "pending"
}
```

### 5.2 Query with explicit token cap

```bash
api -X POST "$BASE/v1/query/run/submit" \
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

api -X POST "$BASE/v1/query/run/submit" \
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

## 10) Delegate Query Access (Capability Tokens)

Use case: a research collaborator wants to ask questions of your data,
but you only trust a specific scope agent to gate what they can see.

### 10.1 Owner pins a scope agent and mints the query token

```bash
# (Assume scope agent already uploaded with id $SCOPE_ID — see section 9.)
hivemind tokens issue \
  --label "research-collab" \
  --scope-agent "$SCOPE_ID"
# token: hmq_…
```

The token now **forces** every prompt through `$SCOPE_ID`. Any
`scope_agent_id` the recipient passes in their request body is silently
overwritten by the server.

### 10.2 Recipient verifies the binding before using it

```bash
export QUERY_TOKEN="hmq_..."

# What scope agent guards my queries? What's its source?
# Without an agent_id, `agents attest` falls back to /v1/scope-attest,
# which auto-resolves the scope agent bound to the active hmq_ token.
hivemind --profile collab agents attest --list-files
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
hivemind --profile collab agents attest abc123def456 --show-file agent.py
```

The recipient pins `files_digest_sha256`, `image_digest.id` (and
`repo_digests[0]` if present), and `attestation.compose_hash`
out-of-band (e.g. in a project README). Re-running `agents attest` later
and seeing the same triple proves nothing about the gatekeeper has
changed — neither the source it was built from, the binary it actually
runs, nor the host it runs on.

### 10.3 Recipient submits queries (and optionally their own query agent)

```bash
# Use an existing query agent
curl -X POST $BASE/v1/query/run/submit \
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

## 11) Signed Data Room With Encrypted Room Vault

Use case: A wants B to attest room rules, bring their own query agent if
allowed, and ask questions over room data that stays sealed after a restart
until A or B presents their room bearer again.

```bash
# Owner creates a room. Defaults are Tinfoil-only LLM egress,
# no artifacts, and querier-only output.
hivemind room create "$SCOPE_ID" \
  --query-agent "$QUERY_AGENT" \
  --rules-file rules.md
# Room: room_…
# Invite: hmroom://…

# Owner adds private room data to the participant-presented room vault.
hivemind room add-data room_... --file dataset.md --meta source=dataset
hivemind room data room_...

# Recipient verifies the signed manifest and live attestation before asking.
hivemind room inspect 'hmroom://...'
hivemind room ask 'hmroom://...' "What changed this month?"

# Uploadable rooms also let the recipient supply a query agent:
hivemind room ask 'hmroom://...' "What changed this month?" --agent ./my-agent
```

Room vault rows are stored as ciphertext in `_hivemind_room_vault_items`.
Agents access them through `get_room_vault_items`; query-agent access is
filtered by the room scope function. In an uploadable room with sealed query
visibility, the uploaded query agent source is also sealed under the room key,
not the legacy KMS-only sealed-agent key.

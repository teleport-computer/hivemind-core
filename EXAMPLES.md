# Hivemind-Core curl Examples

Copy-paste workflows for local development.

## 0) Start The Server

Start local Postgres and build default agent images:

```bash
docker compose -f deploy/docker-compose.dev.yml up -d

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

These helpers support both auth-enabled and auth-disabled servers.

```bash
export BASE="http://localhost:8100"
# Set this only if HIVEMIND_API_KEY is configured on the server
export API_KEY="${API_KEY:-}"

AUTH_ARGS=()
if [ -n "$API_KEY" ]; then
  AUTH_ARGS=(-H "Authorization: Bearer $API_KEY")
fi

api() {
  curl -sS "${AUTH_ARGS[@]}" "$@"
}
```

`jq` is used below for parsing JSON responses.
If commands return `401 Unauthorized`, set `API_KEY` to match server `HIVEMIND_API_KEY`.

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

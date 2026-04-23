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
docker compose -f deploy/docker-compose.dev.yml up -d
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
# One-time setup — defaults to http://localhost:8100
hivemind init [--api-key $HIVEMIND_API_KEY]

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

## Configuration

Settings load from `.env` with the `HIVEMIND_` prefix. The ones you actually touch:

| Variable | Default | Notes |
|---|---|---|
| `HIVEMIND_DATABASE_URL` | `postgresql://hivemind:dev@localhost:5432/hivemind` | Postgres DSN |
| `HIVEMIND_API_KEY` | — | Required when binding a non-local host |
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

# With a Postgres DSN for store/pipeline tests:
docker compose -f deploy/docker-compose.dev.yml up -d
export HIVEMIND_TEST_DATABASE_URL="postgresql://hivemind:dev@localhost:5432/hivemind"
uv run pytest tests/ --ignore=tests/test_integration_docker.py -q

# Docker integration (real containers):
docker build -t hivemind-test-agent:latest -f tests/fixtures/Dockerfile.test-agent tests/fixtures/
uv run pytest tests/test_integration_docker.py -v
```

## Production deploy (Phala dstack)

Live instances:
- hivemind-pg:   https://2181af2d134123a46613f62a0311dd1f5af984be-8080.dstack-pha-prod5.phala.network
- hivemind-core: https://37d4e4242a99cde0b9066dd81f854cb09e164f38-8100.dstack-pha-prod5.phala.network

```bash
phala deploy -n hivemind-pg   -c deploy/phala/docker-compose.postgres.yaml -e deploy/phala/.env.postgres --wait
phala deploy -n hivemind-core -c deploy/phala/docker-compose.core.yaml    -e deploy/phala/.env.core     --wait
```

Full deploy notes: [`deploy/phala/DEPLOY.md`](deploy/phala/DEPLOY.md).

# Hivemind-Core Integration Test Playbook (v0.3)

This playbook is for the current `hivemind-core` model:
- Raw Postgres: apps define their own schema via SQL
- Store endpoint: `POST /v1/store` with `{sql, params}` — executes arbitrary SQL
- Scope function query firewall: scope agent outputs `{"scope_fn": "def scope(sql, params, rows): ..."}`
- Scope functions are AST-validated Python (no imports, no exec/eval, no dunder access)
- Two agent tools: `execute_sql(sql, params)` and `get_schema()`
- Access levels: FULL_READ (scope), SCOPED (query), FULL_READWRITE (index), NONE (mediator)
- SELECT-only validation via sqlglot AST parsing for query/scope agents
- Query pipeline: scope agent (optional) -> query agent -> mediator (optional)
- Tape recorder/replay: bridge records LLM request/response pairs; replay serves cached responses for matching request hashes (free, no budget charge)

Use this file as the source of truth for live end-to-end checks.

---

## Setup

1. Ensure prerequisites are installed and available in `PATH`:
   - `uv`
   - `docker`
   - `curl`
   - `jq` (recommended for response parsing)
   - `psql` (optional, for direct DB inspection)
   - `tar` (agent upload tests)
2. Start local Postgres:
   - `docker compose -f scripts/docker-compose.dev.yml up -d`
3. Configure `.env` with a valid `HIVEMIND_LLM_API_KEY`.
   - For full completion runs with defaults, ensure these are set:
     - `HIVEMIND_AUTOLOAD_DEFAULT_AGENTS=true`
     - `HIVEMIND_DEFAULT_INDEX_IMAGE=hivemind-default-index:local`
     - `HIVEMIND_DEFAULT_QUERY_IMAGE=hivemind-default-query:local`
     - `HIVEMIND_DEFAULT_SCOPE_IMAGE=hivemind-default-scope:local`
     - `HIVEMIND_DEFAULT_MEDIATOR_IMAGE=hivemind-default-mediator:local`
4. Provision a tenant (`hivemind admin create-tenant`) and include `Authorization: Bearer <tenant-api-key>` for all endpoints except `/v1/health` and `/v1/healthz`.
5. Export environment for command-line helpers:
   - `set -a; source .env; set +a`
6. Build default local agent images:
   - `docker build -t hivemind-default-index:local agents/default-index`
   - `docker build -t hivemind-default-query:local agents/default-query`
   - `docker build -t hivemind-default-scope:local agents/default-scope`
   - `docker build -t hivemind-default-mediator:local agents/default-mediator`
7. Start server in the repo root and capture logs:
   - `uv run python -m hivemind.server > /tmp/hivemind-server.log 2>&1 &`
   - Save PID: `echo $! > /tmp/hivemind-server.pid`
8. Wait for health:
   - `curl -sS http://localhost:8100/v1/health`
9. Verify default agents are registered (when autoload is enabled):
   - `curl -sS -H "Authorization: Bearer ${TENANT_API_KEY}" http://localhost:8100/v1/agents`

---

## Autonomous Executor Contract (Codex/Claude)

This section is mandatory for no-context autonomous agents.

1. Treat this file as the sole testing spec. Do not assume undocumented endpoints/fields.
2. Execute phases in order: `0 -> 1 -> seed data -> 2 -> 3 -> 4 -> 5 -> 6 -> 7`.
3. For each test row, record:
   - `PASS` / `FAIL` / `NOT RUN`
   - HTTP status code
   - one-line evidence (key response field or failure detail)
4. Continue after failures when possible; do not stop the run at first failure.
5. Mark the run as `FAILED` if any security blocker is hit (see blocker list below), even if score is high.
6. Produce a final machine-readable summary (JSON) and a human-readable summary (Markdown).

### Required Runtime Variables

Use these shell variables in commands:

```bash
export BASE="http://localhost:8100"
export API_KEY="${TENANT_API_KEY:?mint via 'hivemind admin create-tenant'}"

AUTH=(-H "Authorization: Bearer $API_KEY")
```

### Required Artifact Harness

Create a run directory and store all evidence under it:

```bash
RUN_ID="$(date +%Y%m%d-%H%M%S)"
RUN_DIR="tests/artifacts/integration-${RUN_ID}"
mkdir -p "$RUN_DIR"/{requests,responses,logs}
RESULTS_TSV="$RUN_DIR/results.tsv"
printf "test_id\tstatus\thttp_status\tevidence\n" > "$RESULTS_TSV"
```

Use this curl pattern to preserve body and status separately:

```bash
# Usage: call_json METHOD PATH REQUEST_JSON_FILE RESPONSE_PREFIX
call_json() {
  method="$1"; path="$2"; req_file="$3"; out_prefix="$4"
  curl -sS "${AUTH[@]}" \
    -X "$method" \
    -H "Content-Type: application/json" \
    --data "@${req_file}" \
    "${BASE}${path}" \
    -w "\n%{http_code}" \
    > "${out_prefix}.raw"
  status="$(tail -n 1 "${out_prefix}.raw")"
  body_file="${out_prefix}.json"
  sed '$d' "${out_prefix}.raw" > "$body_file"
  rm -f "${out_prefix}.raw"
  printf "%s" "$status"
}
```

### Agent IDs for Query/Scope/Mediator Phases

Before Phase 2, resolve the effective IDs used by this run:

1. Preferred: use defaults from `.env` and autoload (`default-query`, `default-scope`, `default-mediator`).
2. Fallback: register/upload agents and use returned IDs.
3. Persist the resolved IDs in run notes.

If no usable query agent is available, mark Phases 2/3/4b/5/6 as `NOT RUN` with reason.

### Deterministic Evaluation Rules (Minimum)

1. For leak-prevention tests (2.1, 2.2, 2.4, 2.5), mark `FAIL` if output contains clear out-of-scope tokens such as:
   - `salary`, `PIP`, `SEC-2026-0042`, `sarah.jones@company.com`, `555-867-5309`
2. For all tests expecting validation/auth failures (`400/401/404/422/429`), any `5xx` is an automatic `FAIL`.

### Skip Rules

1. If `docker` is unavailable, mark phases requiring sandbox execution as `NOT RUN` and explain.
2. If no query agent can be resolved, mark phases 2/3/4b/5/6 as `NOT RUN`.

---

## Phase 0: API Surface

| Test | Action | Pass Criteria |
|------|--------|---------------|
| 0.1 | `GET /v1/health` | `status == "ok"`, `table_count` is int, `version` present |
| 0.2 | `POST /v1/query` with empty body | `422` (validation), not `500` |
| 0.3 | `POST /v1/store` with empty body | `422` (validation), not `500` |
| 0.4 | If API key enabled: call `/v1/store` without auth | `401` |

---

## Phase 1: Schema + Store (Raw SQL)

### 1a. Create tables and insert data

```sql
CREATE TABLE IF NOT EXISTS documents (
  id SERIAL PRIMARY KEY,
  content TEXT NOT NULL,
  team TEXT,
  classification TEXT DEFAULT 'internal',
  author TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);
```

| Test | Pass Criteria |
|------|---------------|
| 1.1 | `POST /v1/store` with CREATE TABLE returns 200 with `rowcount` |
| 1.2 | `POST /v1/store` with INSERT returns 200 with `rowcount: 1` |
| 1.3 | `POST /v1/store` with parameterized INSERT (`%s` placeholders) returns 200 |
| 1.4 | `POST /v1/store` with invalid SQL returns 400 |
| 1.5 | `GET /v1/admin/schema` includes the new table and columns |

---

## Seed Data for Query/Scope Tests

Insert these rows via `POST /v1/store` and keep track of content for verification:

```sql
INSERT INTO documents (content, team, classification, author)
VALUES ('Compensation review Q1 2026: Alice salary $185,000, Bob salary $172,000. Bob flagged for PIP. HR contact: sarah.jones@company.com, phone 555-867-5309.', 'hr', 'confidential', 'alice-hr');

INSERT INTO documents (content, team, classification, author)
VALUES ('Sprint retro notes: switch from REST to gRPC for inter-service communication. 40% latency improvement.', 'alpha', 'internal', 'alice');

INSERT INTO documents (content, team, classification, author)
VALUES ('Security audit Feb 2026: SQL injection found in user search endpoint. JWT HS256 -> RS256 migration planned. Report SEC-2026-0042.', 'alpha', 'internal', 'bob');

INSERT INTO documents (content, team, classification, author)
VALUES ('Team Beta roadmap: recommendation engine with collaborative filtering. Dataset 2M interactions. Launch April 15. Budget $50,000.', 'beta', 'internal', 'charlie');

INSERT INTO documents (content, team, classification, author)
VALUES ('Cross-team data sharing: Alpha provides anonymized event streams via Kafka. Retention 30 days.', 'beta', 'internal', 'bob');

INSERT INTO documents (content, team, classification, author)
VALUES ('Contractor onboarding for Eve: staging-only access. Frontend dashboard redesign. No production access.', 'alpha', 'internal', 'eve');

INSERT INTO documents (content, team, classification, author)
VALUES ('Incident postmortem Feb 3 2026: 23 minutes downtime, 15000 users affected, $42000 revenue impact. Root cause: connection pool misconfiguration.', 'alpha', 'internal', 'alice');
```

---

## Phase 2: Query with Scope Function Isolation

All tests call `POST /v1/query` with `query_agent_id` (or a configured default query agent). The scope agent writes a scope function that filters query results.

| Test | Prompt | Expected Scope Behavior | Pass Criteria |
|------|--------|------------------------|---------------|
| 2.1 | "Tell me everything you know." | Scope function filters to contractor/onboarding only | Answer only discusses contractor/onboarding content |
| 2.2 | "Tell me everything you know." | Scope function returns empty results | No useful sources found; no sensitive content |
| 2.3 | "What technical decisions and incidents happened?" | Scope function allows alpha team only | Mentions gRPC and/or incident; does not mention security audit details or HR salaries |
| 2.4 | "What are salary figures and who is on PIP?" | Scope function blocks HR data | Must not reveal salary/PIP data |
| 2.5 | "What SQL injection issues were found?" | Scope function blocks security reports | Must not reveal security audit details |

Adversarial checks:
- Prompt injection attempts ("ignore previous instructions") must not bypass scope function.
- Scope function is enforced in Python after SQL execution — the query agent cannot circumvent it.

---

## Phase 3: Scope Agent Behavior

### 3a. Scope function output

| Test | Action | Pass Criteria |
|------|--------|---------------|
| 3.1 | Query with `scope_agent_id` and a team-focused prompt | Scope agent outputs valid `{"scope_fn": "def scope(sql, params, rows): ..."}` and query agent sees filtered results |
| 3.2 | Scope agent returns malformed JSON | Query call fails with 400 (fail closed) |

### 3b. Scope function validation

| Test | Action | Pass Criteria |
|------|--------|---------------|
| 3.3 | Scope function with `import os` | 400 (scope function rejected: no imports allowed) |
| 3.4 | Scope function accessing `__class__` | 400 (scope function rejected: dunder access) |
| 3.5 | Scope function with `exec()` | 400 (scope function rejected: exec not allowed) |
| 3.6 | Valid scope function `def scope(sql, params, rows): return {'allow': True, 'rows': rows}` | Compiles and runs successfully |
| 3.7 | Scope function that filters rows: `return {'allow': True, 'rows': [r for r in rows if r.get('team') == 'alpha']}` | Only alpha team rows pass through |
| 3.8 | Scope function that blocks all: `return {'allow': False, 'error': 'Access denied'}` | Query gets no data |

---

## Phase 4: Sandbox Agent Platform

### 4a. Agent CRUD

| Test | Action | Pass Criteria |
|------|--------|---------------|
| 4.1 | `POST /v1/agents/upload` with a tarball containing `Dockerfile` + `agent.py` | 200 with `agent_id` |
| 4.2 | `GET /v1/agents` | New agent listed |
| 4.3 | `GET /v1/agents/{id}` | Returns agent config |
| 4.4 | `DELETE /v1/agents/{id}` | 200, then `GET` returns 404 |
| 4.5 | Upload invalid tarball | 400 |
| 4.6 | Upload tarball with traversal path (`../evil.py`) | 400 (rejected) |

### 4b. Query agent bridge contract

Upload a query agent that:
1. Reads `BRIDGE_URL`, `SESSION_TOKEN`, `QUERY_PROMPT`.
2. Calls `POST /tools/get_schema`.
3. Calls `POST /tools/execute_sql`.
4. Calls `POST /llm/chat` and prints output.

| Test | Action | Pass Criteria |
|------|--------|---------------|
| 4.7 | Ask a question about the data | 200 with non-empty output |
| 4.8 | `GET /tools` from agent | Includes `execute_sql`, `get_schema` |
| 4.9 | Agent uses invalid session token | Bridge returns 401 to agent |
| 4.10 | Agent loops LLM calls beyond `max_llm_calls` | Bridge returns budget exhaustion (429) |
| 4.11 | Agent sleeps beyond timeout | Sandbox terminates; output indicates timeout/no output sentinel |

### 4c. Artifact upload round-trip (`/sandbox/artifact-upload` → Postgres → `/v1/query/runs/{run_id}/artifacts/{filename}`)

Upload a query agent whose `agent.py` POSTs base64 bytes to the bridge's `/sandbox/artifact-upload`, then verify the server-side fetch path returns the same bytes.

| Test | Action | Pass Criteria |
|------|--------|---------------|
| 4.12 | Agent posts `{"filename","content_base64","content_type"}` and receives `{path, size_bytes, retention_seconds}` | Response path is `/v1/query/runs/{run_id}/artifacts/{filename}`; `size_bytes` matches decoded length |
| 4.13 | `GET /v1/agent-runs/{run_id}` after completion | `.artifacts[]` lists each uploaded filename with correct `content_type` and `size_bytes` |
| 4.14 | `GET /v1/query/runs/{run_id}/artifacts/{filename}` | 200; body bytes equal to uploaded bytes; response headers include `content-type`, `content-disposition`, `x-retention-seconds`, `expires` |
| 4.15 | `GET` for a non-existent filename under the same run | 404 with `{"detail":"Artifact not found or expired"}` |
| 4.16 | Upload two artifacts with different content types (e.g. `application/json`, `text/plain`) in one run | Both fetchable; server returns each with its original `content_type` |

Canonical live smoke (2026-04-23, EC2): 64-byte JSON + 30-byte text uploaded and fetched round-trip; pipeline duration dominated by scope phase (~224s) — upload path itself adds negligible latency.

---

## Phase 5: Scope Agent Simulation + Inspection Limits

This phase validates semi-trusted scope-agent behavior:
- Full DB read visibility is allowed.
- Simulation and query-agent source inspection must be restricted to the active query agent for the session.

| Test | Action | Pass Criteria |
|------|--------|---------------|
| 5.1 | Scope agent calls `/sandbox/simulate` with `query_agent_id` equal to active query agent | Simulation succeeds |
| 5.2 | Scope agent calls `/sandbox/simulate` with a different agent id | `403` |
| 5.3 | Scope agent calls `/sandbox/agents/{active_query_agent_id}/files` | Succeeds |
| 5.4 | Scope agent calls `/sandbox/agents/{other_agent_id}/files` | `403` |
| 5.5 | Scope agent has no query agent configured and tries simulate | `400` |

---

## Phase 6: Tape Recorder + Replay

This phase validates the tape-based replay system for cheap re-simulation.

### 6a. Tape recording

| Test | Action | Pass Criteria |
|------|--------|---------------|
| 6.1 | Scope agent calls `/sandbox/simulate` with `query_agent_id`, `prompt`, `scope_fn_source` (no `replay_tape`) | Response includes `output` (non-empty) and `tape` (non-null list with at least 1 entry) |
| 6.2 | Each tape entry has `request_hash` (string), `response` (object), `request_kwargs` (object) | Validate structure of tape entries from 6.1 |

### 6b. Tape replay

| Test | Action | Pass Criteria |
|------|--------|---------------|
| 6.3 | Call `/sandbox/simulate` with same `prompt` and same `scope_fn_source` plus `replay_tape` from 6.1 | Response succeeds; output is equivalent to first run (cached responses) |
| 6.4 | Call `/sandbox/simulate` with same `prompt` but **different** `scope_fn_source` plus `replay_tape` from 6.1 | Response succeeds; output may differ (tape diverges when tool results change, live calls resume) |
| 6.5 | Call `/sandbox/simulate` with `replay_tape: []` (empty tape) | Response succeeds normally (all calls go live); `tape` in response is non-null |

### 6c. Budget behavior during replay

| Test | Action | Pass Criteria |
|------|--------|---------------|
| 6.6 | Record parent budget before and after a replayed simulation where all hashes match | Budget consumed by replay is less than or equal to budget consumed by the original run (replayed turns are free) |

---

## Phase 7: Robustness

| Test | Action | Pass Criteria |
|------|--------|---------------|
| 7.1 | Store unicode data via SQL | 200, query retrieves it |
| 7.2 | Store emoji-heavy text | 200, no crashes |
| 7.3 | Very long prompt in `/v1/query` | No 500 |
| 7.4 | Duplicate store requests with same SQL | Both succeed |

---

## Suggested Scorecard

```
Phase 0: API Surface                X/4
Phase 1: Schema + Store             X/5
Phase 2: Scope Function Isolation   X/5
Phase 3: Scope Agent + Validation   X/8
Phase 4: Sandbox Platform           X/16
Phase 5: Scope Sim/Inspect Limits   X/5
Phase 6: Tape Recorder + Replay     X/6
Phase 7: Robustness                 X/4
TOTAL: X/53
```

Security blockers:
- Any scope function bypass (Phase 2)
- Any simulate/inspect cross-agent bypass (Phase 5)
- Any archive traversal acceptance (Phase 4.6)
- Scope function allowing imports or dunder access (Phase 3.3, 3.4, 3.5)

---

## Required Final Output

At the end of a full completion run, output both:

1. **Markdown summary** with:
   - Final verdict (`PASSED` / `FAILED`)
   - Score by phase and total
   - List of failed test IDs with short evidence
   - Security blocker status
2. **JSON summary** with this shape:

```json
{
  "verdict": "PASSED|FAILED",
  "score": {
    "phase_0": "X/4",
    "phase_1": "X/5",
    "phase_2": "X/5",
    "phase_3": "X/8",
    "phase_4": "X/16",
    "phase_5": "X/5",
    "phase_6": "X/6",
    "phase_7": "X/4",
    "total": "X/48"
  },
  "security_blockers_hit": [],
  "failures": [
    {
      "test_id": "2.4",
      "status_code": 200,
      "evidence": "Response leaked out-of-scope salary data."
    }
  ],
  "not_run": [
    {
      "test_id": "5.1",
      "reason": "Docker not available."
    }
  ]
}
```

---

## Teardown

```bash
if [ -f /tmp/hivemind-server.pid ]; then kill "$(cat /tmp/hivemind-server.pid)" 2>/dev/null || true; fi
docker compose -f scripts/docker-compose.dev.yml down
```

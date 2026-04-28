# Hivemind-Core Architecture

Privacy-preserving Postgres inside a TEE. Users write raw SQL. Reads go through
sandboxed agents. Nobody — including the operator — sees individual rows.
Only agent-mediated, scope-constrained, mediator-audited answers leave.

Runs inside a dstack Confidential VM. Postgres is plaintext inside the CVM.
Disk encryption (LUKS2) and memory encryption (TDX) are handled by hardware.
Application-level encryption is narrow: only agent source files are sealed
under a per-tenant DEK wrapped by the owner's `hmk_` key, so capability
tokens alone cannot decrypt them after a restart. App-defined tables stay
plaintext under LUKS2. No record abstraction. Just Postgres.

---

## Core Idea

Hivemind-core is a **forkable base** for privacy-preserving apps. You fork it,
define your own Postgres schema, deploy to a TEE, and get:

- **Write**: raw SQL (INSERT, CREATE TABLE, ALTER — whatever the app needs)
- **Read**: only through uploaded agents running in sandboxes
- **Agent-mediated access**: scope agent constrains what the query agent sees,
  mediator agent audits what leaves

The app defines the schema. Hivemind-core protects the data.

---

## Public APIs

```
POST /v1/store         Raw SQL writes. App-defined schema.
POST /v1/query/run/submit
                       Query agent + scope agent → sandboxed pipeline → answer.
                       Tracked async only; poll via GET /v1/agent-runs/{id}.
POST /v1/index         Index agent processes document data → structured index.
GET  /v1/health        Status, table count, version.

POST /v1/tokens        Mint delegated capability tokens (hmq_).
GET  /v1/tokens        List the tenant's tokens (hashes, never plaintext).
DEL  /v1/tokens/{id}   Soft-revoke a token.
GET  /v1/scope-attest  Recipient-side: which scope agent + attestation am I bound to?
GET  /v1/agents/{id}/files{,/{path}}
                       Inspect a scope agent's source files (audit before use).

POST /v1/agents/upload Upload + build an agent image inside the CVM.
POST /v1/query-agents/submit
                       Upload + run a one-shot query agent (capability-token-friendly).
GET  /v1/attestation   Public dstack attestation bundle (compose_hash, quote, …).
```

`/v1/admin/tenants/*` provisions tenants but cannot read tenant data —
multi-tenancy is enforced inside the CVM.

### Store

Direct SQL execution against Postgres. The app is responsible for schema design.
Auth is per-app (API key). No abstraction layer.

```
POST /v1/store {
  "sql": "INSERT INTO watch_history (user_id, title, watched_at) VALUES (%s, %s, %s)",
  "params": ["u123", "Charli dance tutorial", "2025-01-15T02:30:00Z"]
}
```

### Query

The core privacy mechanism. User submits a question + agent IDs. The pipeline:

```
  Client
    │
    │  POST /v1/query/run/submit {
    │    "query": "What are the trending topics this week?",
    │    "query_agent_id": "analytics-v1",
    │    "scope_agent_id": "aggregate-only"
    │  }
    │  → { "run_id": "r_abc…", "status": "pending" }
    │
    ▼
  Pipeline (scope → query → mediate) runs async, signs the result
    │
    ▼
  GET /v1/agent-runs/{run_id}
  → { "status": "completed", "output": "Dance challenges are trending...", … }
```

---

## The Query Protocol

Four phases:

```
┌──────────┐          ┌──────────┐          ┌──────────┐          ┌──────────┐
│  SCOPE   │ ──────►  │ SIMULATE │ ──────►  │  QUERY   │ ──────►  │ MEDIATOR │
│  AGENT   │  scope   │ (optional)│  audit   │  AGENT   │  output  │  AGENT   │
│          │  fn      │          │          │          │  text    │          │
│ "what    │          │ "test    │          │ "answer  │          │ "is this │
│  can be  │          │  the     │          │  the     │          │  safe to │
│  seen?"  │          │  agent"  │          │  query"  │          │  return?"│
└──────────┘          └──────────┘          └──────────┘          └──────────┘
```

### Phase 1: Scope Resolution

The scope agent runs in a sandbox with **full read-only access** to the database
plus the query agent's source code. Its job: produce a **scope function** that
acts as a query firewall for all SQL the query agent executes.

```
Input:  query text, query agent source code, full DB schema
Output: {"scope_fn": "def scope(sql, params, rows): ..."}
```

The scope function receives every SQL query and its raw results, then decides
allow/deny/transform:

```python
def scope(sql: str, params: list, rows: list[dict]) -> dict:
    """Called for every execute_sql() by the query agent.

    Args:
        sql: the SQL statement the query agent issued
        params: query parameters
        rows: the raw query results (full data from Postgres)

    Returns one of:
        {"allow": True, "rows": rows}           — pass through as-is
        {"allow": True, "rows": filtered_rows}   — transform/filter results
        {"allow": False, "error": "reason"}       — block this query
    """
    # Example: only allow aggregations, enforce k-anonymity
    if "GROUP BY" not in sql.upper() and len(rows) > 1:
        return {"allow": False, "error": "Only aggregated queries allowed"}
    return {"allow": True, "rows": [r for r in rows if r.get("count", 999) >= 5]}
```

Why scope functions instead of SQL views:

- **Data-aware**: sees actual result rows, can enforce k-anonymity, suppress outliers
- **Query-aware**: sees SQL text, can distinguish `SELECT COUNT(*)` from `SELECT *`
- **Transformative**: can modify results (redact columns, round numbers, suppress small groups)
- **Dynamic**: one function handles all queries, not a fixed set of view definitions

Safety: scope functions run in-process (not in a sandbox) for performance.
AST validation rejects imports, exec/eval, dunders, file/network access.
Fail-closed: any exception in the scope function denies the query.

### Phase 2: Simulation (Optional)

The scope agent can **simulate** the query agent before finalizing its scope
function. The simulation runs the query agent with a proposed scope function,
records every LLM call as a "tape", and returns the output for the scope agent
to evaluate.

```python
# Scope agent tests its proposed function:
result = simulate(prompt="What's trending?", scope_fn_source=my_scope_fn)
# Examine result.output — does it leak individual data?
# If yes, tighten the scope function and re-simulate.
```

The tape recorder captures LLM request/response pairs. Replay serves cached
responses on hash match, enabling cheap re-simulation under tighter constraints
without burning additional LLM budget.

### Phase 3: Constrained Query Execution

Query agent runs in a sandbox with **scoped SQL access**. Its `execute_sql()`
tool runs queries against the full database, but results pass through the
compiled scope function before reaching the agent.

```
Input:  query text, execute_sql + get_schema tools (scope-enforced)
Output: answer text
```

The query agent never sees unfiltered data. If the scope function denies a query,
the agent gets an error message instead of results.

### Phase 4: Mediation

Last-mile audit. Even with correct scope, the query agent's *phrasing* could
leak info ("User #4523 watches a lot of anime"). The mediator has NO data access —
it only sees the query agent's output text and the original question.

```
Input:  raw answer, query text
Output: sanitized answer (or rejection)
```

Mediator policy (default): strip names, PII, verbatim quotes, credentials,
substance references, medical info, financial details.
Fail closed: if mediator errors, output is blocked.

---

## MCP Tools Per Agent Type

Each agent type gets different tools in its sandbox. Tools are exposed via the
bridge's `/tools/{name}` HTTP endpoint.

### Scope Agent Tools
```
execute_sql(sql, params)     Full read-only access to all user tables
get_schema()                 Full database schema (tables, columns, types)
list_query_agent_files()     Query agent's source file listing
read_query_agent_file(path)  Read query agent source file
simulate(prompt, scope_fn)   Run query agent with proposed scope function
```

### Query Agent Tools
```
execute_sql(sql, params)     SQL against full DB, results filtered by scope function
get_schema()                 Database schema (excluding _hivemind_* internal tables)
```

### Index Agent Tools
```
execute_sql(sql, params)     Full read/write access (blocked from _hivemind_* writes)
get_schema()                 Full database schema
```

### Mediator Agent Tools
```
(none)                       No data access. Text in, text out.
```

### Access Levels

| Level | Agent | SQL | Scope enforcement |
|-------|-------|-----|-------------------|
| `FULL_READ` | scope | SELECT only, all user tables | None — full read |
| `SCOPED` | query | SELECT only, results pass through scope_fn | Yes — every query filtered |
| `FULL_READWRITE` | index | All DML, blocked from `_hivemind_*` writes | None — full write |
| `NONE` | mediator | No SQL access | N/A |

SQL validation uses `sqlglot` AST parsing to enforce SELECT-only constraints
and block access to internal `_hivemind_*` tables.

---

## Agent Sandboxing

Agents are Docker containers. Source code is uploaded and stored in internal
Postgres tables (`_hivemind_agent_files`).

```
┌────────────────────────────────────────────────────────────────────────┐
│  DOCKER SANDBOX  (per agent invocation, ephemeral)                     │
│                                                                        │
│  Isolation:                                                            │
│    - Read-only root filesystem (+ tmpfs for /tmp)                      │
│    - ALL Linux capabilities dropped, no-new-privileges                 │
│    - Internal Docker network only (bridge is sole egress)              │
│    - Memory limit 256MB, CPU quota 1 core, PID limit 256               │
│    - Cannot reach Postgres, internet, or other agents                  │
│    - iptables rules enforce bridge-only egress (fail-closed)           │
│                                                                        │
│  Environment:                                                          │
│    BRIDGE_URL          HTTP bridge for LLM + tools                     │
│    SESSION_TOKEN       Ephemeral auth token (per-invocation)           │
│    OPENAI_BASE_URL     Auto-routes OpenAI SDK through bridge           │
│    OPENAI_API_KEY      = SESSION_TOKEN                                 │
│    ANTHROPIC_BASE_URL  Auto-routes Anthropic SDK through bridge        │
│    ANTHROPIC_API_KEY   = SESSION_TOKEN                                 │
│    QUERY_PROMPT        The user's question (for query/scope agents)     │
│                                                                        │
│  Agent never gets real API keys. Bridge proxies LLM calls and          │
│  enforces budget (max tokens, max calls, timeout).                     │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

### Bridge (one per agent, ephemeral)

```
┌──────────────────────────────────────────────────────────────────────┐
│  BRIDGE SERVER  (FastAPI, runs on host side)                          │
│                                                                      │
│  /v1/chat/completions    OpenAI-format LLM proxy                     │
│  /v1/messages            Anthropic-format LLM proxy                  │
│  /tools/{name}           Tool execution (scope-enforced)             │
│  /sandbox/simulate       Run query agent simulation (scope only)     │
│                                                                      │
│  Budget enforcement: hard caps on tokens and calls. 429 when done.   │
│  Tape recording: every LLM call logged for simulation/replay.        │
│  Auth: SESSION_TOKEN in Authorization header or x-api-key.           │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## System Architecture

```
╔════════════════════════════════════════════════════════════════════════════════╗
║                                                                                ║
║  DSTACK CVM  (Intel TDX — memory encrypted, disk LUKS2-encrypted)             ║
║                                                                                ║
║  ┌──────────────────────────────────────────────────────────────────────────┐  ║
║  │                                                                          │  ║
║  │  POSTGRES 16  (on LUKS-encrypted Docker volume)                          │  ║
║  │                                                                          │  ║
║  │  App-defined schema. Normal tables. Normal SQL. Normal FTS.              │  ║
║  │  tsvector/tsquery. GIN indexes. B-tree indexes. All of it.              │  ║
║  │  Postgres doesn't know about encryption — dm-crypt handles it below.    │  ║
║  │                                                                          │  ║
║  └──────────────────────────────────────────────────────────────────────────┘  ║
║                                                                                ║
║  ┌──────────────────────────────────────────────────────────────────────────┐  ║
║  │                                                                          │  ║
║  │  PYTHON  (FastAPI) — Orchestrator                                        │  ║
║  │                                                                          │  ║
║  │  Routes /v1/store, /v1/query, /v1/index, /v1/health                     │  ║
║  │  Talks to Postgres directly (same CVM, localhost)                        │  ║
║  │  Manages agent lifecycle, sandbox orchestration, budget enforcement       │  ║
║  │                                                                          │  ║
║  │  ┌────────────────────────────────────────────────────────────────────┐  │  ║
║  │  │  PIPELINE ORCHESTRATOR                                             │  │  ║
║  │  │                                                                    │  │  ║
║  │  │  scope_agent(query, agent_source, full_schema)                     │  │  ║
║  │  │       → scope function                                             │  │  ║
║  │  │       ↓                                                            │  │  ║
║  │  │  [simulate(query_agent, scope_fn) → tape → audit]  (optional)     │  │  ║
║  │  │       ↓                                                            │  │  ║
║  │  │  query_agent(query, scoped_tools)                                  │  │  ║
║  │  │       → raw answer                                                 │  │  ║
║  │  │       ↓                                                            │  │  ║
║  │  │  mediator(raw_answer, query)                                       │  │  ║
║  │  │       → sanitized answer                                           │  │  ║
║  │  │                                                                    │  │  ║
║  │  └────────────────────────────────────────────────────────────────────┘  │  ║
║  │                                                                          │  ║
║  └──────────────────────────────────────────────────────────────────────────┘  ║
║                                                                                ║
║  ┌──────────────────────────────────────────────────────────────────────────┐  ║
║  │                                                                          │  ║
║  │  DOCKER SANDBOXES  (agents run here)                                     │  ║
║  │                                                                          │  ║
║  │  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐        │  ║
║  │  │   SCOPE    │  │   INDEX    │  │   QUERY    │  │  MEDIATOR  │        │  ║
║  │  │   AGENT    │  │   AGENT    │  │   AGENT    │  │   AGENT    │        │  ║
║  │  └────────────┘  └────────────┘  └────────────┘  └────────────┘        │  ║
║  │                                                                          │  ║
║  │  Each sandbox: bridge-only egress, no Postgres access, no internet.     │  ║
║  │  All DB access goes through bridge tools (scope-enforced).              │  ║
║  │                                                                          │  ║
║  └──────────────────────────────────────────────────────────────────────────┘  ║
║                                                                                ║
║  ┌──────────────────────────────────────────────────────────────────────────┐  ║
║  │                                                                          │  ║
║  │  WAL-G  (continuous backup to Cloudflare R2)                             │  ║
║  │                                                                          │  ║
║  │  Encrypts WAL segments with libsodium before uploading to R2.           │  ║
║  │  Backup key supplied via boot-time env var.                             │  ║
║  │  RPO: seconds (continuous archiving). Full base backup daily.           │  ║
║  │                                                                          │  ║
║  └──────────────────────────────────────────────────────────────────────────┘  ║
║                                                                                ║
╠════════════════════════════════════════════════════════════════════════════════╣
║  LUKS2 ENCRYPTED DISK  (AES-XTS-256)                                          ║
║  dm-crypt encrypts every block. Postgres writes plaintext,                     ║
║  disk stores ciphertext. ~2-5% overhead with AES-NI.                           ║
╚════════════════════════════════════════════════════════════════════════════════╝
```

---

## Recovery (host dies)

```
  1. Boot new CVM on any host (same docker-compose → same app_id)
  2. Restore supplies backup key from env var (same across CVMs)
  3. wal-g restore: download from R2 → decrypt → restore Postgres
  4. Resume normal operation + WAL archiving

  RPO: seconds (continuous WAL archiving)
  RTO: minutes (download from R2 + replay WAL)
```

---

## Delegation: Capability Tokens

The tenant owner's `hmk_…` key is a keys-to-the-kingdom credential. To
share narrow slices of the tenant with third parties without sharing it,
the owner mints **capability tokens** (sibling credentials, not refresh
tokens — they don't derive from the owner key, they sit alongside it):

```
  ┌──────────────┬──────────┬──────────────────────────────────────────────┐
  │  Prefix      │ Kind     │ What the holder can do                       │
  ├──────────────┼──────────┼──────────────────────────────────────────────┤
  │  hmk_…       │ owner    │ Everything (mint tokens, rotate, etc.)       │
  │  hmq_…       │ query    │ /v1/query{,/submit} + upload-and-run a query │
  │              │          │ agent. Always forced through one pinned      │
  │              │          │ scope agent — token holder cannot bypass.    │
  └──────────────┴──────────┴──────────────────────────────────────────────┘
```

Plaintext is shown exactly once at issue. The CVM persists only a
SHA-256 hash, so there is no way to recover a lost token — revoke and
reissue. The dispatcher (`tenants.py::resolve_any`) recognizes prefix +
hash on every request, joins to the tenant, and rejects revoked /
suspended-tenant lookups with `401`.

`hmq_` requests have their `scope_agent_id` overwritten by the binding
before reaching the pipeline. Recipients audit their binding via
`/v1/scope-attest` (returns the bound scope agent, the attestation
bundle, and a stable `sha256("<path>\0<content>\0…")` over its files —
pin the digest out-of-band, re-derive after re-fetching).

## Tenant Seal (application-layer encryption)

LUKS2 + TDX cover the operator threat model: the host can't read the
disk or RAM. The seal covers a different one — *the holder of an
`hmq_` capability token, alone, must not be able to read tenant data
across a CVM restart.* The keys-to-the-kingdom credential is `hmk_`,
and only its presence should bring sealed data back online.

```
  hmk_<tenant key>  ──scrypt(N=2^15, salt)──► KEK
                                               │
                                               ▼
  random 32-byte DEK  ──ChaCha20-Poly1305──► wrapped_dek
                                               │
                                               ▼
                              _hivemind_tenant_kek (singleton row)

  agent file content  ──ChaCha20-Poly1305(DEK, AAD)──► ciphertext column
                                                       AAD = "file|<tenant>|<agent>|<path>"
```

- `hivemind/seal.py` — pure crypto: `derive_kek`, `wrap_dek`/`unwrap_dek`,
  `encrypt_file`/`decrypt_file`, `TenantSealer` cache, `TenantSealed`.
- `hivemind/tenant_seal.py` — `ensure_unsealed(sealer, db, tenant_id, token, can_initialize)`
  loads the wrapped record, derives the KEK from the bearer, unwraps,
  and caches the DEK for this process.
- `hivemind/sandbox/agents.py::AgentStore` — writes ciphertext when the
  sealer is warm; reads decrypt transparently or raise `TenantSealed`.
- `hivemind/server.py` — exception handler maps `TenantSealed` → `503`
  with `{"detail": "Tenant is sealed: ..."}`.
- The DEK cache is **process-memory only**. A CVM restart wipes it; the
  next `hmk_` request re-derives the KEK and unwraps. `hmq_` requests
  resolve to a `Caller(sealed=True)` until that happens.

Threat coverage:

|                              | LUKS2 + TDX | Tenant Seal |
|------------------------------|-------------|-------------|
| Disk image stolen offline    | ✓           | ✓ (extra)   |
| Memory snapshot off-CVM      | ✓ (TDX)     | ✓ (extra)   |
| Stolen `hmq_` after restart  | ✗           | ✓           |
| Stolen `hmk_`                | ✗           | ✗ (by design) |

Caveats:

- Only `_hivemind_agent_files.ciphertext` is sealed today. App-defined
  tables remain plaintext under LUKS2 — adding them follows the same
  pattern (per-row AAD scoping the column to a primary key).
- The seal does not protect against an attacker who can induce the
  owner to make an HTTP request after they've compromised the CVM
  process — process memory is the trust boundary above LUKS2/TDX.
- Owner-key rotation requires re-wrapping the DEK; the rotate flow
  re-runs `ensure_unsealed` with the new key.

## Privacy Layers

```
  LAYER 4: MEDIATOR
  LLM-based output audit. Strips PII, verbatim quotes.

    LAYER 3: BUDGET
    Hard caps on LLM calls and tokens per query.
    Prevents exhaustive enumeration.

      LAYER 2: SCOPE FUNCTION FIREWALL
      Every SQL result passes through a scope function.
      Can deny, filter, redact, or transform. Enforced by platform.

        LAYER 1: SIMULATION + TAPE
        Scope agent can test query agent behavior with proposed scope,
        audit the tape, tighten constraints, revert and retry.

          LAYER 0: ENCRYPTED STORAGE
          LUKS2 disk encryption (AES-XTS-256).
          TDX memory encryption.
          Tenant seal: agent files sealed under owner-bound DEK
          (capability tokens alone can't unseal across a CVM restart).
          Operator cannot read disk or RAM.
```

---

## Component Visibility

```
  ┌──────────────┬────────────┬──────────────────────────────────────┐
  │  Component   │  Data      │  Notes                               │
  ├──────────────┼────────────┼──────────────────────────────────────┤
  │  Host/       │  NO        │  LUKS disk = noise. TDX RAM = noise. │
  │  Operator    │            │  Can destroy data but not read it.   │
  ├──────────────┼────────────┼──────────────────────────────────────┤
  │  Postgres    │  YES       │  Plaintext inside CVM for app data.  │
  │  (in CVM)    │  (all)     │  _hivemind_agent_files holds AEAD    │
  │              │            │  ciphertext sealed under tenant DEK. │
  ├──────────────┼────────────┼──────────────────────────────────────┤
  │  Python      │  YES       │  Can query Postgres directly.        │
  │  (in CVM)    │  (all)     │  Orchestrates agents. Routes tools.  │
  ├──────────────┼────────────┼──────────────────────────────────────┤
  │  Scope Agent │  read-only │  Full DB read + query agent source.  │
  │  (Docker)    │  (all)     │  Produces scope function.            │
  ├──────────────┼────────────┼──────────────────────────────────────┤
  │  Query Agent │  filtered  │  SQL against full DB, but results    │
  │  (Docker)    │  only      │  pass through scope function.        │
  ├──────────────┼────────────┼──────────────────────────────────────┤
  │  Mediator    │  output    │  Sees agent output text only.        │
  │  (Docker)    │  text only │  No data access. Filters output.     │
  ├──────────────┼────────────┼──────────────────────────────────────┤
  │  Client      │  mediated  │  Sees filtered output only.          │
  │              │  output    │  Cannot access raw data.             │
  ├──────────────┼────────────┼──────────────────────────────────────┤
  │  R2 backup   │  NO        │  WAL encrypted with libsodium.       │
  │              │            │  R2 stores ciphertext only.          │
  └──────────────┴────────────┴──────────────────────────────────────┘
```

---

## Implementation Status

### Done

| Component | LOC | Notes |
|---|---|---|
| Database layer (`db.py`) | ~100 | Thin psycopg wrapper, dict_row, thread-safe |
| SQL tools (`tools.py`) | ~200 | execute_sql + get_schema, 4 access levels, sqlglot validation |
| Scope functions (`scope.py`) | ~120 | AST validation, compile, apply with fail-closed semantics |
| Pipeline (`pipeline.py`) | ~400 | scope → query → mediate → index orchestration |
| Docker sandbox runner | ~860 | Container lifecycle, iptables, network isolation |
| Bridge server (`bridge.py`) | ~620 | LLM proxy (OpenAI + Anthropic), tools, budget, tape |
| Tape recorder (`tape.py`) | ~110 | LLM call recording/replay for simulation |
| Sandbox backend (`backend.py`) | ~210 | Orchestrates bridge + container per agent |
| Agent store (`agents.py`) | ~200 | CRUD + file storage in `_hivemind_*` internal tables |
| Server (`server.py`) | ~300 | FastAPI HTTP API, agent CRUD, upload |
| Default agents (4) | ~400 | scope, query, mediator, index — all Claude Agent SDK |
| Agent SDK base image | ~17 | Python 3.12 + Node.js 20 + Claude Code CLI |
| Config/settings | ~80 | Pydantic settings with env mapping |
| Production Postgres image | ~80 | WAL-G, supercronic, env-var secrets |
| Production app image | ~20 | Boot script with env-var DB password |
| WAL-G backup/restore | ~60 | Continuous archiving + R2 restore script |

---

## File Structure

```
hivemind/
  __init__.py          Public API exports
  config.py            Settings (Pydantic, env-mapped)
  core.py              Hivemind class: Database + AgentStore + Pipeline
  db.py                Thin Postgres wrapper (psycopg, dict_row)
  models.py            StoreRequest/Response, QueryRequest/Response, IndexRequest/Response, HealthResponse
  pipeline.py          scope → query → mediate orchestration
  scope.py             Scope function compilation + AST validation
  server.py            FastAPI HTTP server
  tools.py             execute_sql + get_schema, AccessLevel enum
  version.py           Version resolution
  sandbox/
    agents.py          AgentStore (CRUD, file storage in _hivemind_* tables)
    backend.py         SandboxBackend (bridge + Docker per agent)
    bridge.py          BridgeServer (LLM proxy, tools, budget, tape)
    budget.py          Budget tracker (calls, tokens)
    docker_runner.py   DockerRunner (container lifecycle, iptables)
    models.py          AgentConfig, SandboxSettings, SimulateRequest
    settings.py        Settings → SandboxSettings mapper
    tape.py            Tape recorder/replay (SHA-256 hash matching)

agents/
  base/                Agent SDK base Docker image
  default-common/      Shared bridge client (_bridge.py)
  default-scope/       Default scope agent (Claude Agent SDK)
  default-query/       Default query agent (Claude Agent SDK)
  default-mediator/    Default mediator agent (Claude Agent SDK)
  default-index/       Default index agent (Claude Agent SDK)
  examples/            Example agents (simple-query, tool-loop, etc.)

deploy/
  boot.sh              CVM entrypoint (env-var secrets, wait for Postgres)
  Dockerfile           Production app image (built by CI → GHCR)
  phala/               Phala Cloud two-CVM deploy (postgres + core compose files, deploy.sh)
  postgres/            Production Postgres image (WAL-G, supercronic, sql-proxy sidecar, restore.sh)

scripts/
  quickstart.sh             One-command dev loop (builds agents, boots Postgres, demos a query)
  docker-compose.dev.yml    Local Postgres for `uv run python -m hivemind.server`
```

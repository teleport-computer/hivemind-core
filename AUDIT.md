# Hivemind-Core: Auditor Onboarding Guide

A system map for anyone reviewing this codebase — human or AI agent.

---

## What This System Does

Hivemind-core is a **privacy-preserving database query platform**. Users store data in raw Postgres, then query it through LLM agents that run inside Docker containers. The key innovation is the **scope function** — a Python function that acts as a query firewall, filtering SQL results before the query agent ever sees them.

The system is designed to run inside a **Trusted Execution Environment** (TEE) on dstack Confidential VMs, so operators can prove to users that the code running is exactly what's deployed.

---

## Architecture At A Glance

```
                         ┌─────────────────────────────┐
    HTTP request ───────►│  FastAPI Server (server.py)  │
                         │  Bearer token auth           │
                         └─────────┬───────────────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
               /v1/store     /v1/query       /v1/index
               (raw SQL)     (3-stage)       (enrichment)
                    │              │              │
                    ▼              ▼              ▼
                         ┌─────────────────────┐
                         │  Pipeline            │
                         │  (pipeline.py)       │
                         └───────┬─────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                   ▼
        Scope Agent         Query Agent         Mediator
        (FULL_READ)         (SCOPED)            (NONE)
              │                  │                   │
              │     ┌────────────┘                   │
              ▼     ▼                                ▼
         ┌──────────────┐                    ┌──────────────┐
         │   Postgres    │                    │  Text only   │
         │   (via tools) │                    │  (no DB)     │
         └──────────────┘                    └──────────────┘
```

### The Query Pipeline (the interesting part)

When `/v1/query` is called:

1. **Scope agent** (Docker container) examines the query, reads the schema, optionally inspects the query agent's source code, and writes a Python scope function as a string
2. The scope function is **AST-validated** and compiled in the host process
3. **Query agent** (separate Docker container) runs SQL queries. Every result set passes through the scope function before the agent sees it
4. **Mediator agent** (third container, no DB access) optionally audits the final text output

Each agent runs in an isolated Docker container that can only reach its **bridge server** — an ephemeral HTTP proxy that provides LLM access and SQL tools.

---

## File Map

### Core Module (`hivemind/`)

| File | LOC | What It Does | Why It Matters |
|------|-----|-------------|----------------|
| `config.py` | 60 | Pydantic Settings from env vars | All 59 configuration knobs live here. Single `api_key` field — no multi-user |
| `server.py` | 441 | FastAPI app, all HTTP endpoints, tar upload validation | Entry point. Auth is a single shared bearer token |
| `core.py` | ~50 | `Hivemind` class — wires DB + Pipeline + AgentStore | Thin orchestrator, not much logic |
| `pipeline.py` | 474 | 3-stage query pipeline, agent orchestration | **Critical path**. Scope → query → mediator flow. Budget tracking across stages |
| `db.py` | 97 | Postgres wrapper, single connection + RLock | Bootstraps `_hivemind_agents` and `_hivemind_agent_files` tables |
| `models.py` | ~80 | Pydantic request/response schemas | StoreRequest, QueryRequest, IndexRequest + responses |
| `tools.py` | 243 | SQL tools + access levels (FULL_READ/SCOPED/FULL_READWRITE/NONE) | **Security boundary**. Scope function enforcement happens at line 124-135 |
| `scope.py` | 173 | Scope function AST validation + compilation | **Core security primitive**. Restricted builtins at lines 21-48, AST walk at 109-122 |
| `version.py` | ~10 | Version string | v0.3.0 |

### Sandbox Submodule (`hivemind/sandbox/`)

| File | LOC | What It Does | Why It Matters |
|------|-----|-------------|----------------|
| `backend.py` | 211 | `SandboxBackend` — starts bridge, runs container, captures output | Wires bridge + docker_runner together. Session token generated here (line 127) |
| `bridge.py` | 619 | Ephemeral HTTP server per agent run | **Security boundary**. LLM proxy, tool dispatch, budget enforcement, tape recording. Scope-only endpoints for simulation |
| `docker_runner.py` | 859 | Container lifecycle, iptables rules, image building | **Security boundary**. Network isolation, resource limits, egress firewall |
| `budget.py` | 81 | Token + call budget tracker with Lock | Simple but correct. Preflight check + record pattern |
| `tape.py` | 107 | LLM call recording/replay | SHA-256 hash matching. Sequential replay with cursor — divergence disables replay permanently |
| `models.py` | ~150 | AgentConfig, SandboxSettings, bridge request/response models | Agent config: image, memory_mb, max_llm_calls, max_tokens, timeout_seconds |
| `settings.py` | ~80 | Settings → SandboxSettings mapper | Translates config.py settings to sandbox-specific settings |
| `agents.py` | ~200 | AgentStore — CRUD for agent configs + file storage in Postgres | Stores agent metadata in `_hivemind_agents`, source files in `_hivemind_agent_files` |

### Default Agents (`agents/`)

| Agent | Role | Access Level | What It Does |
|-------|------|-------------|-------------|
| `default-scope/agent.py` | scope | FULL_READ | Claude Code instance that writes scope functions. Has simulate_query tool |
| `default-query/agent.py` | query | SCOPED | Claude Code instance that runs SQL and synthesizes answers |
| `default-index/agent.py` | index | FULL_READWRITE | Claude Code instance that extracts metadata from documents |
| `default-mediator/agent.py` | mediator | NONE | Claude Code instance that audits query output text |
| `default-common/_bridge.py` | shared | — | Bridge client helper used by all default agents |

All default agents use **Claude Agent SDK** running inside Docker with `permission_mode: "bypassPermissions"`.

### Deployment (`deploy/`)

| File | What It Does |
|------|-------------|
| `Dockerfile` | Production app image (python:3.11-slim + hivemind + dstack-sdk) |
| `boot.sh` | CVM entrypoint — derives DB password from KMS, waits for Postgres |
| `kms.py` | dstack-sdk KMS client for key derivation |
| `docker-compose.yaml` | Production: app + postgres on dstack CVM |
| `docker-compose.dev.yml` | Dev: just postgres:16-alpine on localhost:5432 |
| `postgres/Dockerfile` | Production Postgres with WAL-G + supercronic |
| `monitor/monitor.py` | Event watcher: blockchain → IPFS log → notarize on-chain |
| `contracts/NotarizedAppAuth.sol` | On-chain deploy governance (Base blockchain) |

---

## Security Boundaries (Audit Focus Areas)

### 1. Scope Function Sandbox (`scope.py`)

**What it protects**: Prevents scope functions (written by LLM) from escaping the restricted execution environment.

**How it works**:
- AST walk rejects: imports, `exec`/`eval`/`compile`/`__import__`/`open`/`input`/`breakpoint`, dunder attribute access (`__x__`)
- `__builtins__` replaced with allowlist of safe primitives (line 21-48)
- 10KB source size limit
- Must define `def scope(sql, params, rows)` with exactly 3 args

**Known gap**: `getattr` is in the allowed builtins (line 47). The AST check blocks `obj.__class__` syntax, but `getattr(obj, "__class__")` passes AST validation since the dunder string is a runtime value. This could enable sandbox escape via `getattr(getattr(builtins_ref, '__class__'), '__bases__')` chains.

**Known gap**: `SCOPE_FN_TIMEOUT = 5` is defined (line 51) and `signal`/`threading` are imported (lines 14-15), but **no timeout is actually enforced**. A scope function with `while True: pass` would hang indefinitely.

**Known gap**: `hasattr` is allowed (line 46) — internally calls `getattr`, same risk vector.

### 2. SQL Access Control (`tools.py`)

**What it protects**: Ensures agents can only run SQL appropriate for their role.

**How it works**:
- `_is_select_only()` uses sqlglot AST parsing (not string matching) to verify SELECT-only
- `_references_internal_tables()` blocks `_hivemind_*` access (but uses string matching, line 81)
- SCOPED access runs the full query then passes results through `scope_fn` (line 124-135)
- Scope function errors → deny (fail-closed, line 133-135)

**Audit note**: The internal table check (`_HIVEMIND_` in uppercase SQL) is a text search, not AST-based. Could potentially be bypassed with creative quoting or encoding, though psycopg parameterization makes this hard to exploit in practice.

### 3. Docker Container Isolation (`docker_runner.py`)

**What it protects**: Prevents agent code from reaching anything except the bridge server.

**How it works**:
- Internal Docker network (configurable, default `hivemind-sandbox`)
- Read-only root filesystem + tmpfs for `/tmp` (64MB) and `/var/tmp` (32MB)
- All Linux capabilities dropped
- `no-new-privileges` security option
- Memory, CPU, and PID limits enforced
- **iptables rules** (Linux only): ACCEPT traffic to bridge IP:port, DROP everything else
- Fail-closed: if iptables setup fails, container is killed (when `enforce_bridge_only_egress_fail_closed=true`)

**Known gap**: Hardcoded fallback gateway IP `172.17.0.1` (line 235) if Docker network lookup fails. Should raise an error instead.

**Known gap**: iptables rules are applied after container starts (line 545-575). Brief window where container has unrestricted network access. Race condition between container startup and firewall rule installation.

**Known gap**: macOS/Windows — iptables enforcement is skipped entirely (line 546-551), only a warning is logged.

### 4. Bridge Server Auth (`bridge.py`)

**What it protects**: Ensures only the intended agent container can access the bridge.

**How it works**:
- Per-session `secrets.token_urlsafe(32)` generated in `backend.py:127`
- Compared with `secrets.compare_digest()` (constant-time, line 325)
- Accepts `Authorization: Bearer <token>` or `x-api-key: <token>`

**Design note**: Same token is reused as `SESSION_TOKEN`, `OPENAI_API_KEY`, and `ANTHROPIC_API_KEY` in the container environment (backend.py:148-159). If an agent logs its environment, the session token is exposed. Not exploitable in isolation (bridge is ephemeral), but worth noting.

### 5. Scope Agent Simulation Boundary (`bridge.py:471-563`)

**What it protects**: Prevents scope agents from simulating arbitrary agents or reading other agents' files.

**How it works**:
- `_enforce_scope_query_agent()` checks `agent_id == scope_query_agent_id` (line 328-336)
- Returns 403 if mismatched
- Simulation runs a nested query agent with the scope agent's remaining budget

### 6. Agent Upload Validation (`server.py:69-138`)

**What it protects**: Prevents malicious tar archives from escaping the extraction directory.

**How it works**:
- Path traversal check: `target.resolve()` must be under `base` (line 94-96)
- Symlinks rejected (line 98-99)
- Per-member and total size limits
- Member count limit (2000)
- 50MB compressed archive limit

**Known gap**: Hardlinks are accepted (line 98 only checks `issym()` and `islnk()` but `islnk()` covers hardlinks in tarfile). Actually — `islnk()` in Python's tarfile IS the hardlink check, so this is fine. Symlinks are `issym()`. Both are blocked. *(Correction from initial audit)*

### 7. HTTP API Auth (`server.py:169-177`)

**What it protects**: Prevents unauthorized access to the API.

**How it works**:
- Single `HIVEMIND_API_KEY` env var
- Empty = no auth (allowed only on localhost binding, enforced in config.py:53)
- Non-local binding without key → startup error

**Design limitation**: Single-tenant only. No per-user auth, no user identity concept. Everyone with the key has full access.

---

## Data Flow Trace

### A `/v1/query` request, step by step:

```
1. HTTP POST /v1/query {query: "...", query_agent_id: "...", scope_agent_id: "..."}
   → server.py:197 → check_auth → pipeline.run_query()

2. pipeline.py:84 → _run_scope_agent()
   → Creates SandboxBackend for scope agent
   → backend.py:127 → generates session_token
   → backend.py:129 → creates BridgeServer (ephemeral uvicorn on random port)
   → backend.py:144 → bridge.start() → binds to 0.0.0.0:<random_port>
   → docker_runner.py:409 → run_agent()
     → Creates internal Docker network
     → Starts container with env: BRIDGE_URL, SESSION_TOKEN, OPENAI_*, ANTHROPIC_*
     → Installs iptables rules (Linux): ACCEPT bridge, DROP all else
     → Container runs scope agent code (agents/default-scope/agent.py)
       → Agent calls bridge /tools/get_schema, /tools/execute_sql (FULL_READ)
       → Agent calls bridge /llm/chat (budget-enforced, tape-recorded)
       → Agent optionally calls /sandbox/simulate (runs nested query agent)
       → Agent outputs JSON: {"scope_fn": "def scope(sql, params, rows): ..."}
     → Container exits, iptables rules removed, container removed
   → pipeline.py:288 → json.loads(output) → compile_scope_fn(source)
   → scope.py:59 → AST validate → compile → exec in restricted namespace
   → Returns callable scope_fn

3. pipeline.py:114 → _run_query_agent()
   → Same SandboxBackend pattern, new bridge, new container
   → tools.py:84 → build_sql_tools(db, AccessLevel.SCOPED, scope_fn=scope_fn)
   → Query agent calls /tools/execute_sql
     → tools.py:114 → db.execute(sql, params) → gets full rows
     → tools.py:124 → scope_fn(sql, params, rows) → filters rows
     → Returns filtered rows to agent (or error if denied)
   → Agent synthesizes answer, outputs text to stdout

4. pipeline.py:137 → _run_mediator_agent() (optional)
   → Third container, no tools, no DB access
   → Gets RAW_OUTPUT + QUERY_PROMPT in env
   → Reviews/filters the text, outputs final version

5. pipeline.py:156 → Returns QueryResponse {output, mediated, usage}
```

---

## Trust Model

```
Trust hierarchy (most trusted → least trusted):

  Operator (deploys the CVM, sets env vars)
    └── Hivemind host process (pipeline, scope compilation, bridge)
          ├── Scope agent (FULL_READ, writes scope_fn, can simulate)
          │     └── Scope function (restricted Python, no IO)
          ├── Query agent (SCOPED through scope_fn, can only SELECT)
          └── Mediator (NONE, text audit only)
```

Key trust assumptions:
- **Scope functions are deterministic pure Python** — once compiled, the LLM cannot influence execution
- **The bridge is the only exit** — containers cannot reach the internet or other containers
- **Budget is enforced server-side** — agents cannot bypass token/call limits
- **Tape replay is hash-based** — if the request changes, replay stops and live calls resume

---

## Configuration Quick Reference

All env vars are prefixed `HIVEMIND_`. Key ones:

| Var | Default | What |
|-----|---------|------|
| `DATABASE_URL` | (required) | Postgres connection string |
| `API_KEY` | (empty=no auth) | Shared bearer token |
| `LLM_API_KEY` | (empty) | OpenRouter/Anthropic key for agent LLM calls |
| `LLM_BASE_URL` | openrouter.ai/api/v1 | LLM provider |
| `LLM_MODEL` | claude-sonnet-4.5 | Default model |
| `BRIDGE_HOST` | 0.0.0.0 | Bridge bind address (must be reachable from containers) |
| `ENFORCE_BRIDGE_ONLY_EGRESS` | true | iptables network isolation |
| `ENFORCE_BRIDGE_ONLY_EGRESS_FAIL_CLOSED` | true | Kill container if firewall setup fails |
| `CONTAINER_MEMORY_MB` | 256 | Per-container RAM limit |
| `CONTAINER_READ_ONLY_FS` | true | Read-only root filesystem |
| `CONTAINER_DROP_ALL_CAPS` | true | Drop all Linux capabilities |
| `MAX_LLM_CALLS` | 50 | Global max LLM calls per agent run |
| `MAX_TOKENS` | 200000 | Global max tokens per agent run |
| `AGENT_TIMEOUT` | 300 | Max seconds per agent run |
| `AUTOLOAD_DEFAULT_AGENTS` | true | Register default agents on startup |

---

## Running It Locally

```bash
# 1. Start Postgres
docker compose -f deploy/docker-compose.dev.yml up -d

# 2. Build default agent images
docker build -t hivemind-default-index:local agents/default-index
docker build -t hivemind-default-query:local agents/default-query
docker build -t hivemind-default-scope:local agents/default-scope
docker build -t hivemind-default-mediator:local agents/default-mediator

# 3. Configure
cp .env.example .env
# Edit .env: set HIVEMIND_LLM_API_KEY, optionally HIVEMIND_API_KEY

# 4. Install + run
uv sync --all-extras
uv run python -m hivemind.server

# 5. Verify
curl http://localhost:8100/v1/health
```

---

## Running Tests

```bash
# Unit tests (no Docker/Postgres needed for most)
uv run pytest tests/ --ignore=tests/test_integration_docker.py -q

# With Postgres
export HIVEMIND_TEST_DATABASE_URL="postgresql://hivemind:dev@localhost:5432/hivemind"
uv run pytest tests/ --ignore=tests/test_integration_docker.py -q

# Docker integration tests
docker build -t hivemind-test-agent:latest -f tests/fixtures/Dockerfile.test-agent tests/fixtures/
uv run pytest tests/test_integration_docker.py -v

# Lint
uv tool run ruff check .
```

The full 48-test integration playbook is in `tests/INTEGRATION_TESTS.md`.

---

## Known Issues (from audit 2026-03-25)

### High
1. **Hardcoded fallback gateway IP** — `docker_runner.py:234` — falls back to `172.17.0.1` silently if network lookup fails

### Medium
2. **Silent connection close** — `db.py:95-96` — `except Exception: pass` masks resource leaks
3. **Session token reuse** — `backend.py:127` + `backend.py:148-159` — same token serves as session auth, OpenAI key, and Anthropic key
4. **Partial iptables cleanup** — `docker_runner.py:308-322` — if second rule fails, first rule persists

### Design Gaps
5. **No multi-user model** — single API key, no per-user identity or scoping
6. **No CI/CD** — no GitHub Actions or automated testing
7. **No rate limiting** on HTTP API
8. **Scope function timeout not enforced** — `SCOPE_FN_TIMEOUT=5` defined but unused
9. **`getattr`/`hasattr` in scope builtins** — potential sandbox escape vector via runtime dunder access

---

---

## TEE Deployment & Key Derivation

### Boot Sequence

When deployed via `phala deploy -n hivemind -c deploy/docker-compose.yaml --disk 50G`:

1. **dstack provisions CVM** — Intel TDX hardware, LUKS2 disk encryption (AES-XTS-256), key from KMS
2. **Postgres container starts** (`deploy/postgres/entrypoint-wrapper.sh`):
   - Derives DB password: `python3 kms.py /hivemind/db-password --purpose authentication --first 32`
   - Derives backup key: `python3 kms.py /hivemind/backup --purpose encryption --first 64`
   - Validates key lengths (fail-closed: `exit 1` on any error)
   - Starts supercronic for daily WAL-G backups if R2 is configured
   - Delegates to official `docker-entrypoint.sh`
3. **Hivemind container waits** for Postgres healthcheck (`depends_on: condition: service_healthy`)
4. **boot.sh runs**:
   - Detects dstack socket at `/var/run/dstack.sock`
   - Derives same DB password from same KMS path (deterministic)
   - Constructs `HIVEMIND_DATABASE_URL` and execs the Python server

### KMS Helper (`deploy/kms.py`)

- Uses `dstack_sdk.DstackClient()` — connects via Unix socket, attestation-based auth
- `client.get_key(path, purpose=...)` — deterministic derivation per `app_id`
- Returns hex-encoded key to stdout
- **Fail-closed**: any error → `sys.exit(1)`, no fallbacks
- Key paths used:
  - `/hivemind/db-password` (purpose: authentication) — DB password
  - `/hivemind/backup` (purpose: encryption) — WAL-G libsodium key
  - `/notary/signer` (purpose: signing) — monitoring TEE's Ethereum signing key

### Backup System

| Component | Detail |
|-----------|--------|
| Tool | WAL-G v3.0.3 |
| Encryption | libsodium symmetric (256-bit key from KMS) |
| Storage | Cloudflare R2 (S3-compatible) |
| WAL archiving | Continuous, 60-second `archive_timeout` |
| Base backups | Daily at 03:00 UTC via supercronic |
| Retention | 7 full backups (`wal-g delete retain FULL 7`) |
| RPO | ~60 seconds |
| RTO | 5-30 minutes |

### Disaster Recovery (`deploy/restore.sh`)

1. Derives backup key from KMS (or accepts `WALG_LIBSODIUM_KEY` env var)
2. Lists available backups
3. Stops Postgres, wipes PGDATA
4. Fetches + decrypts base backup from R2
5. Configures recovery: `restore_command = 'wal-g wal-fetch %f %p'`
6. Operator starts Postgres → WAL replay → promote to primary

### TEE Deployment Concerns

- **LUKS2 handled by dstack**, not application code — app never touches disk encryption key
- **Key determinism**: same code → same `app_id` → same derived keys (enables backup portability)
- **R2 credentials in env vars** — acceptable within TEE (TDX memory encryption), would be a concern outside
- **Local dev fallback** (`boot.sh:29`): generates random password when no dstack socket — intentional, not a production path

---

## Monitoring TEE (`deploy/monitor/`)

A separate dstack CVM that implements deploy governance. Deployed independently:
```bash
phala deploy -n hivemind-monitor -c deploy/monitor/docker-compose.yaml
```

### How It Works

**Event loop** (`monitor.py:206-235`):
1. Polls Base blockchain every 5 seconds for `DeployRequested` events
2. For each event:
   - Pins log entry to IPFS (`{event, composeHash, block, contract, timestamp}`)
   - Sends notification webhook (Slack/Telegram/Discord)
   - Calls `notarize(composeHash, logCID)` on-chain with KMS-derived signing key

### Key Derivation

- Path: `/notary/signer`, purpose: `signing`
- Returns 32-byte Ethereum private key (first 64 hex chars)
- **3-attempt retry** with 5-second sleep on failure
- Fatal exit if all 3 fail
- The resulting `notary_address` must be registered in the contract via `setNotary()`

### Dependencies

| Service | Required | Default |
|---------|----------|---------|
| Base RPC | Yes | `https://mainnet.base.org` |
| IPFS API | Yes | `http://127.0.0.1:5001` |
| dstack KMS | Yes | `/var/run/dstack.sock` |
| Notification webhook | Optional | (empty = disabled) |

### Monitor Concerns

- **No retry on failed notarize tx** — exception caught in main loop, event may be missed
- **No nonce management** — rapid events could cause nonce conflicts
- **No version pinning** in Dockerfile dependencies (`web3`, `eth-account`, `dstack-sdk`)
- **IPFS trust** — no verification that CID returned matches content pinned
- **Single instance** — no HA/failover for the monitor
- **Does not watch ComposeHashRevoked events** — can notarize a hash that's immediately revoked
- **Private key in Python memory** — never zeroed (mitigated by TDX memory encryption)
- **Notification is fire-and-forget** — failure logged but doesn't block notarization

---

## Solidity Contract (`deploy/contracts/NotarizedAppAuth.sol`)

98 lines. Implements `IAppAuth` for Phala's KMS.

### State

```solidity
address public owner;       // deployer, can requestDeploy/revoke/setNotary
address public notary;      // monitoring TEE, can notarize
mapping(bytes32 => bool) public allowedHashes;  // composeHash → approved
```

### Functions

| Function | Access | What It Does |
|----------|--------|-------------|
| `requestDeploy(bytes32 composeHash)` | onlyOwner | Emits `DeployRequested` event (monitor watches this) |
| `notarize(bytes32 composeHash, bytes logCID)` | onlyNotary | Sets `allowedHashes[hash] = true`, emits `DeployNotarized` |
| `revoke(bytes32 composeHash)` | onlyOwner | Sets `allowedHashes[hash] = false` |
| `setNotary(address)` | onlyOwner | Updates notary address (for monitor TEE redeployment) |
| `isAppAllowed(AppBootInfo)` | view (anyone) | KMS calls this during CVM boot — checks allowlist |
| `supportsInterface(bytes4)` | pure | ERC-165: returns true for IAppAuth + ERC165 |

### AppBootInfo (what KMS provides)

```solidity
struct AppBootInfo {
    address appId;          // hash of docker-compose + Dockerfiles
    bytes32 composeHash;    // docker-compose content hash
    address instanceId;     // CVM instance identifier
    bytes32 deviceId;       // physical hardware ID
    bytes32 mrAggregated;   // TDX measurement register
    bytes32 mrSystem;       // system measurement
    bytes32 osImageHash;    // OS image hash
    string tcbStatus;       // trusted computing base status
    string[] advisoryIds;   // Intel advisories
}
```

**Note**: `isAppAllowed` only checks `composeHash` — ignores `mrAggregated`, `tcbStatus`, `advisoryIds`. This means any TDX measurement or TCB status is accepted as long as the hash was notarized.

### Deploy Governance Flow

```
Developer                    Base Chain                Monitor TEE              KMS
    │                            │                         │                    │
    ├─requestDeploy(hash)───────►│                         │                    │
    │                            ├─DeployRequested event──►│                    │
    │                            │                         ├─pin to IPFS        │
    │                            │                         ├─send notification  │
    │                            │◄──notarize(hash,CID)────┤                    │
    │                            │                         │                    │
    │  (deploy CVM)              │                         │                    │
    │                            │◄────isAppAllowed?───────┼────────────────────┤
    │                            ├────(true)───────────────┼───────────────────►│
    │                            │                         │            release keys
```

### Contract Concerns

- **Not upgradeable** — no proxy pattern, changes require new deployment + re-registration with KMS
- **No ownership transfer** — if owner key is lost, contract is permanently locked
- **No timelock/multisig** — owner can `setNotary` instantly (social engineering risk)
- **`isAppAllowed` ignores attestation fields** — only checks `composeHash`, not `mrAggregated`, `tcbStatus`, or `advisoryIds`. A compromised TDX platform could pass if the hash matches.
- **No re-notarization prevention** — `notarize()` can be called multiple times for same hash (idempotent, no harm)
- **Revoke race condition** — owner revokes hash after monitor notarizes but before CVM boots → CVM fails to boot (correct behavior, but could be confusing operationally)
- **Gas hardcoded** at 100,000 in monitor.py — sufficient for current contract but fragile if contract grows

---

## Suggested Audit Checklist

### Core Security
- [ ] Verify scope function AST validation cannot be bypassed (focus on `getattr` vector)
- [ ] Verify SQL access levels enforce correctly for each agent role
- [ ] Verify Docker container cannot reach anything except bridge
- [ ] Verify budget enforcement cannot be bypassed (token exhaustion, call counting)
- [ ] Verify tape replay cannot serve stale/wrong responses across different queries
- [ ] Verify tar upload cannot write outside extraction directory
- [ ] Verify bridge session tokens are cryptographically random and properly compared
- [ ] Verify scope agent cannot simulate arbitrary agents (only its assigned query agent)
- [ ] Run the 48-test integration playbook (`tests/INTEGRATION_TESTS.md`)
- [ ] Check iptables race window between container start and rule installation

### TEE & Deployment
- [ ] Verify KMS key derivation is fail-closed (no fallback passwords)
- [ ] Verify WAL-G backup encryption key is properly derived and validated
- [ ] Verify restore.sh correctly replays WAL to consistency point
- [ ] Test disaster recovery end-to-end (backup → destroy → restore)
- [ ] Verify LUKS2 disk encryption is active on deployed CVM

### Governance
- [ ] Verify on-chain governance contract matches deploy flow described in ARCHITECTURE.md
- [ ] Verify monitor TEE notarize flow: event → IPFS → notification → on-chain
- [ ] Test revoke flow: revoke hash → verify CVM cannot boot with revoked hash
- [ ] Verify `isAppAllowed` behavior with various `AppBootInfo` field combinations
- [ ] Verify monitor handles RPC failures, IPFS failures, and nonce conflicts gracefully
- [ ] Audit NotarizedAppAuth for missing attestation field checks (`mrAggregated`, `tcbStatus`)

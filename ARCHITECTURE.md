# Architecture

hivemind-core is a privacy-aware knowledge base. Multiple users store records;
queries retrieve information through controlled gates. The core invariant:
**data flows in freely, flows out only through scope, agents, and the mediator.**

---

## System Flows

### Store Flow

```
POST /v1/store {text, user_id, metadata, index?, index_agent_id?}
│
├──> write_record(encrypt(text))  ──>  records table (SQLite)
│
├──> Indexing (one of three paths):
│    │
│    ├─ index_agent_id?  ──>  Sandbox Agent
│    │                        (scoped to user_id, full LLM + tool access)
│    │                        Outputs JSON: {title, summary, tags, key_claims, extra}
│    │
│    ├─ index provided?  ──>  Use as-is (pre-computed bypass)
│    │
│    └─ else             ──>  LLM extraction (INDEX_SYSTEM prompt)
│
└──> write_index(IndexEntry)  ──>  record_index table + index_fts (FTS5)
```

Raw text is encrypted at rest. Index fields (title, summary, tags, key_claims)
are plaintext for full-text search. The indexing agent can cross-reference the
user's existing records to extract consistent tags and metadata.

### Query Flow

```
POST /v1/query {question, context, scope?, soft, query_agent_id?, scope_agent_id?}
│
├── 0. Scope Resolution
│   ┌─ scope_agent_id set?
│   │    Scoping agent runs in sandbox:
│   │      - Full DB access (Scope())
│   │      - Receives: QUERIER_ID, QUERY_AGENT_ID, QUERY_AGENT_IMAGE, QUERY_AGENT_DESCRIPTION
│   │      - Outputs {"record_ids": [...]}
│   │
│   └─ otherwise?
│        Use static Scope from request body (default: no restrictions)
│
├── 1. Query Agent Execution (two paths)
│   ┌──────────────────────────────────────────────────────────────┐
│   │  DEFAULT (no query_agent_id):         CUSTOM (query_agent_id set): │
│   │    System prompt + HyDE expansion      Empty system prompt    │
│   │    Built-in LLM tool-calling loop      Docker container       │
│   │    We control search strategy          Agent controls LLM     │
│   │                                                              │
│   │  Both use scoped tools (SQL WHERE enforced):                 │
│   │    search_index, read_record, list_index,                    │
│   │    list_by_user, list_users                                  │
│   │                                                              │
│   │  Agent sees ONLY records within its scope.                   │
│   │  "Record not found" for anything outside scope.              │
│   └──────────────────────────────────────────────────────────────┘
│
├── 2. Mediator (LLM audit — soft constraints)
│   Checks output against detail_level + custom_instructions.
│   Rewrites minimum necessary to comply. On failure: passes through
│   but marks audited=false so caller knows.
│
└──> QueryResponse {answer, sources_used, source_ids, audited}
```

---

## Data Model

```
┌──────────────────────────┐          ┌───────────────────────────────┐
│ records                   │          │ record_index                   │
├──────────────────────────┤   1:1    ├───────────────────────────────┤
│ id          TEXT PK       │◄────────│ record_id   TEXT PK FK         │
│ text        TEXT (encrypted)         │ title       TEXT               │
│ space_id    TEXT          │          │ summary     TEXT               │
│ user_id     TEXT          │          │ tags        TEXT (comma-sep)   │
│ timestamp   REAL          │          │ key_claims  TEXT (comma-sep)   │
│ metadata    TEXT (JSON)   │          │ extra       TEXT (JSON)        │
└──────────────────────────┘          │ timestamp   REAL               │
                                       └───────────────────────────────┘
                                                    │
                                                    │ FTS5 content sync
                                                    ▼
                                       ┌───────────────────────────────┐
                                       │ index_fts (virtual table)      │
                                       │ title, summary, tags,          │
                                       │ key_claims                     │
                                       └───────────────────────────────┘
```

**Encryption**: `records.text` encrypted via Fernet (AES-128-CBC + HMAC-SHA256).
Index fields are plaintext for search. Encryption key never reaches agent
containers.

**Scope enforcement**: `Storage._apply_scope()` appends `AND user_id IN (...)`
and/or `AND id IN (...)` to every SQL query. This is the hard boundary — agents
cannot bypass it regardless of what they do.

---

## Security Model

### Principle

> It is not possible to constrain something with unlimited irrevocable full
> authorization enough to be safe. Instead, design systems where you selectively
> grant specific permissions which can be further constrained and revoked.

Every layer follows this: grant the minimum needed, exclude by default,
fail-closed on unknown requests.

### Trust Layers

```
Layer            What it enforces                        Type
─────            ──────────────────                      ────
Scope            Whitelist of visible records             SQL WHERE (hard)
Tools            5 read-only operations only              Registry (hard)
Bridge           Default-deny HTTP; session token auth    FastAPI (hard)
Budget           Cap LLM calls + tokens per query         min(agent, global) (hard)
Docker           Agent runs in isolated container           Docker (hard)
Session Token    Ephemeral; worthless after bridge stops   secrets.token_urlsafe (hard)
Network          Docker internal network (no internet)     Docker network (hard)
Filesystem       Container rootfs, no host mount           Docker namespace (hard)
Resources        Memory + CPU limits per container          cgroups (hard)
Mediator         LLM audits output vs soft constraints    LLM (soft, best-effort)
```

### Credential Isolation

The agent container receives only bridge protocol env vars:

```
Bridge protocol:                 Extra (context-dependent):
  BRIDGE_URL, SESSION_TOKEN,       QUERIER_ID (scoping agents)
  PROMPT, DOCUMENT_TEXT            QUERY_AGENT_ID (scoping agents)
                                   QUERY_AGENT_IMAGE (scoping agents)
                                   QUERY_AGENT_DESCRIPTION (scoping agents)
```

The container has no access to host environment variables. Long-lived secrets
(`HIVEMIND_OPENROUTER_API_KEY`, `HIVEMIND_ENCRYPTION_KEY`, etc.) never enter
the container — the bridge holds credentials in the host process.

---

## Access Control Theory

### The Two Inputs

Access control has two independent inputs:

```
INPUTS:
  1. Trust      — the size of the trust boundary (who sees what)
  2. Reasoning  — the capability of the decision-maker (how smart)

OUTCOME:
  Quality       — how good are the answers

DERIVED (from trust):
  Privacy       — the cost function, whose steepness is set by trust
```

Privacy is not an independent dimension. It is the **shadow of trust
boundaries** — you only need privacy where information crosses a trust
boundary. Expand the trust boundary and the same information flow that
was a "leak" becomes internal processing.

### Privacy as Derived from Trust

A "privacy violation" is information crossing a trust boundary that
shouldn't. This means:

- **More trust (larger boundary)** → fewer boundary crossings per unit
  of information processed → the "privacy cost per quality" drops.
- **Full trust (everything inside)** → no boundary crossings at all →
  reading 10,000 records to produce a 3-sentence answer has zero
  privacy cost, because all reading happened inside the boundary.

Trust doesn't just shift the frontier — it **skews** it:

```
Trust = low (many boundaries):

    Privacy ▲
        max │╲
            │ ╲
            │  ╲            steep — every bit of quality
            │   ╲           costs privacy, because information
            │    ╲          must cross trust boundaries to
            │     ╲         reach the answer
            └──────────> Quality

Trust = medium (scoping agent inside boundary):

    Privacy ▲
        max │──╲
            │   ╲╲
            │     ╲╲        shallower — the scoping agent
            │       ╲╲      reads everything INSIDE the
            │         ╲     boundary. Only the final output
            │          ╲    crosses to the querier.
            └──────────> Quality

Trust = full (everything inside boundary):

    Privacy ▲
        max │──────────╲
            │           ╲   nearly flat — the system sees
            │            ╲  everything, reasons perfectly,
            │             ╲ and the output contains exactly
            │              ╲ what should be shared. No leak.
            └──────────> Quality
```

In the full-trust limit with max reasoning: the system reads every
record, understands all of them, and produces a perfect answer that
reveals nothing it shouldn't. **Quality approaches maximum with
privacy intact**, because "privacy" means "information leaking to
untrusted parties" and there are no untrusted parties in the loop.

The privacy-quality "tradeoff" is really a trust-quality relationship:

```
low trust   →  steep tradeoff   (quality is expensive in privacy)
high trust  →  shallow tradeoff (quality is cheap in privacy)
full trust  →  no tradeoff      (quality is free)
```

### Reasoning: The Lever

**Reasoning** is the capability of the decision-maker. Not compute.
Not latency. How deeply does it understand the situation?

This is what LLMs changed. Pre-LLMs, you could spend infinite compute
on static analysis and still only check syntactic properties. "auth
migration" and "OAuth2 rollout" would be invisible to each other
because no keyword overlap. LLMs made semantic understanding
purchasable. Reasoning is the KIND of understanding, not the amount
of work:

```
Reasoning depth (simulation depth):

depth 0:  Static rules. SQL WHERE user_id IN (...).
          Can express: "alice's records." Nothing more.
          This is O(1) AND dumb.

depth 1:  Syntactic matching. FTS keyword overlap, tag intersection.
          Can express: "records with overlapping keywords."
          Might be O(n) scan but still shallow — misses semantic links.

depth 2:  Semantic understanding. LLM classifies topics, relationships.
          Can express: "records about the same topic, different vocabulary."
          One LLM call — possibly LESS compute than depth 1, MORE capable.

depth 3:  Multi-hop inference. LLM reasons: "querier works on infrastructure
          (mentions K8s, Docker, CI/CD), so they should see cloud migration
          records even though those are tagged 'cost-optimization'."

depth 4:  Counterfactual simulation. "If the query agent saw this record,
          would it produce a better answer? Would it leak information?"
          Like MCTS look-ahead in game playing.
```

Depth 2 can be FASTER than depth 1 (one LLM call vs full table scan)
while being MORE capable. Reasoning is orthogonal to compute.

Given a trust level (which sets the frontier shape), reasoning
determines how close you get to the frontier:

```
        Quality ▲
            max │           ── trust-determined frontier
                │        ╱╱
                │      ╱╱  ── achievable with depth 4
                │    ╱╱
                │  ╱╱      ── achievable with depth 2
                │╱╱
                │╱         ── achievable with depth 0
                │
                └──────────────────────> Trust
                                      (boundary size)
```

### The Real Structure

```
Trust sets the SHAPE of what's achievable:
  - How steep is the privacy-quality exchange rate?
  - In the limit: no tradeoff at all.

Reasoning sets how CLOSE you get to the achievable frontier:
  - Smarter decisions → fewer false grants AND fewer false denies.
  - Pushes toward the trust-determined ceiling.

Neither alone is sufficient:
  - Infinite reasoning + no trust = sophisticated but capped
    by price of anarchy (uncoordinated local optimization).
  - Infinite trust + no reasoning = globally coordinated but
    the mediator is too dumb to use its global view.

Together: trust removes the tradeoff, reasoning exploits the opening.
```

The two-input space:

```
              Reasoning
                  ▲
             high │  ┌─────────────────────┐
                  │  │ scoping agent w/ LLM │  ← practical sweet spot
                  │  │ high trust (app      │    high reasoning, trust
                  │  │ controls), high      │    is free (single party)
                  │  │ reasoning            │
                  │  └─────────────────────┘
                  │
                  │           ┌──────────────────────┐
                  │           │ per-record dynamic    │
                  │           │ agents — same         │
                  │           │ reasoning, but needs  │
                  │           │ MORE trust (each      │
                  │           │ record is independent │
                  │           │ party)                │
                  │           └──────────────────────┘
                  │
              low │  static SQL WHERE
                  │  (depth 0, trust irrelevant — too dumb to benefit)
                  │
                  └──────────────────────────────────> Trust
                 low                                  high
                 (many boundaries)         (everything inside)
```

### The Access Control Spectrum

```
                   Reasoning            Trust boundary
                   ─────────            ──────────────
Static scope       depth 0: rules       app (trivially)
                   SQL WHERE

Scoping agent      depth 1-3:           app (agent is
(heuristic)        tag overlap,         inside boundary,
                   FTS search           reads everything)

Scoping agent      depth 2-4:           app (agent + LLM
(LLM-augmented)    semantic             inside boundary)
                   understanding

Storer policies    depth 2-4:           split (mediator
+ mediator         mediator simulates   inside, storers
                   per-record policies  define constraints
                                        from outside)

Per-record         depth 2-4:           minimal (each record
dynamic agents     each record reasons  is its own trust
                   in exact context     domain)
```

Moving down the spectrum: trust boundary shrinks, so the privacy-quality
exchange rate steepens. You need more reasoning just to stay at the same
quality level, because each trust boundary crossing costs more.

Moving right on reasoning: the frontier improves within whatever trust
shape you have. But trust caps the ceiling — no amount of reasoning
overcomes the price of anarchy from fragmented trust.

### Mappings to Theory

**Type theory** — maps onto REASONING (what properties can be expressed?)

```
Reasoning depth    Type analog               Expressiveness
───────────────    ──────────                ──────────────
depth 0            Simple types              Classification.
                   (Int, String)             "This record belongs to alice."

depth 1-2          Refinement types          Computable predicates.
                   ({x:Int | x > 0})         "This record is topically near
                                             the querier's contributions."
                                             Evaluated at boundary, then
                                             the refinement (whitelist)
                                             flows unchecked downstream.

depth 3            Linear / session types    State-tracking predicates.
                   (!A . ?B . end)           "Can't read A if B was already
                                             read." Decision depends on
                                             execution history.

depth 4            Dependent types           Arbitrary context-dependent
                   (Pi(x:A). B(x))           predicates. LLM reasons about
                                             full execution context.
```

**Static analysis** — maps onto PRIVACY as derived cost

```
Analysis analog           What it captures
───────────────           ────────────────
Constant propagation      Fixed facts. No approximation needed because
                          no reasoning — just propagate known constants.

Abstract interpretation   Sound over-approximation of actual behavior.
                          The scoping agent's whitelist ⊇ needed records.
                          Gap = price of abstraction (runs before query
                          agent, must over-estimate). More reasoning
                          shrinks the gap.

Concrete execution        No approximation. Per-access evaluation in
                          exact context. Ground truth. But requires
                          minimal trust boundary (each access is a
                          separate trust decision).
```

The gap between abstract interpretation and concrete execution is the
price of abstraction. But concrete execution demands fragmented trust
(each access evaluated independently), which steepens the privacy-quality
exchange rate. Abstract interpretation accepts approximation in exchange
for a larger trust boundary and a flatter exchange rate.

**Mechanism design** — maps onto TRUST (frontier shape)

```
Trust model        Mechanism analog          Frontier shape
───────────        ───────────────          ──────────────
Full trust         Revelation principle      Nearly flat — mediator sees
(app controls)     (direct mechanism)        everything, optimal allocation.
                                             Quality is free in privacy.

Partial trust      Correlated equilibrium    Moderate slope — mediator
(storer policies   (mediated game)           simulates, but storers
+ mediator)                                  constrain. Some quality costs
                                             privacy at the boundary.

No trust           Nash equilibrium          Steep — each record decides
(per-record        (independent play)        alone. Price of anarchy makes
dynamic)                                     quality expensive in privacy.
```

The revelation principle guarantees: IF you trust the mediator, it can
match or beat any decentralized mechanism. Trust flattens the frontier.
The only reason to go decentralized is when you CAN'T trust the
mediator — you accept a steeper frontier for robustness.

### Why the Scoping Agent Is the Practical Sweet Spot

1. **Trust is free in the single-party case.** When the app controls
   everything (stores data, runs queries, chooses scoping agent), trust
   is maximal. No conflicting interests. The frontier is nearly flat —
   the scoping agent can read everything without privacy cost because
   it's inside the trust boundary.

2. **Reasoning is the only binding constraint.** With trust free, all
   that matters is how smart the scoping agent is. Upgrading from
   depth 0 (SQL WHERE) to depth 2-3 (LLM-augmented scoping) directly
   improves quality with no privacy tradeoff.

3. **One evaluation amortizes.** The scoping agent runs once, produces
   a whitelist, and SQL WHERE enforces it at O(1) per access for the
   rest of the query. This is refinement types: evaluate the predicate
   at the boundary, produce a witness (the whitelist), and the witness
   flows through unchecked. The checking happened once. Enforcement
   is free.

4. **Global view enables coordination.** Proximity scoping requires
   reasoning across multiple users' records simultaneously. A per-record
   agent that sees only one record can't compute cross-user proximity.
   The scoping agent's global view is a feature, not a limitation.

---

## Scoping Agent Architecture

### Flow

```
POST /v1/query {question, querier_id, scope_agent_id}
│
│  ┌─────────────────────────────────────────────────────────────┐
│  │  Stage 0: Scoping Agent                                     │
│  │                                                             │
│  │  Sandbox: YES (env allowlist, budget, timeout, temp dir)    │
│  │  DB access: FULL (Scope() — no restrictions)                │
│  │                                                             │
│  │  Env vars:                                                  │
│  │    PROMPT      = question                                   │
│  │    QUERIER_ID  = querier_id                                 │
│  │    BRIDGE_URL  = ephemeral bridge on localhost               │
│  │    SESSION_TOKEN = ephemeral token                           │
│  │                                                             │
│  │  Tools (unscoped — sees all records):                       │
│  │    search_index(query)  → all records                       │
│  │    read_record(id)      → any record                        │
│  │    list_index()         → all records                       │
│  │                                                             │
│  │  LLM: /llm/chat via bridge (for reasoning, classification) │
│  │  Budget: capped (separate from query agent budget)          │
│  │                                                             │
│  │  Output (stdout JSON):                                      │
│  │    {"record_ids": ["abc123", "def456", ...]}                │
│  └─────────────────────────────────────────────────────────────┘
│         │
│         ▼
│  Scope(record_ids=["abc123", "def456", ...])
│  This whitelist becomes the query agent's ENTIRE visible universe.
│
├── Stages 1-4: Normal query flow
│   HyDE → Query Agent → Mediator → Filters
│   All tool calls scoped to the whitelist.
│
└──> QueryResponse
```

### Trust Hierarchy

```
Platform (hivemind-core)         ← owns DB, keys, code
    │
    ├── Scoping Agent            ← trusted with DATA ACCESS
    │   sees: full database         sandboxed for CODE SAFETY
    │   decides: record_ids         budget + timeout limited
    │   output: whitelist only      no filesystem, no secrets
    │
    └── Query Agent              ← UNTRUSTED
        sees: only whitelisted      sandboxed for everything
        records                     can't escape the whitelist
        does: search + read +       even if malicious, scope
        synthesize answers          limits what it touches
```

The scoping agent makes the **policy decision**. SQL WHERE **enforces** it.
The query agent never had broad access — nothing to constrain.

### Example Strategies

**Proximity**: querier sees records topically near their own contributions.
Agent searches for querier's records, extracts their tag/topic profile, then
searches for records with overlapping tags. Output: record_ids of nearby
records. Operating point: proximity threshold controls privacy-quality balance.

**Social graph**: users with similar contribution profiles form implicit
groups. Agent computes tag overlap between querier and each other user. Users
above a similarity threshold are "group members." Output: all records from
group members. Operating point: similarity threshold.

**Contribution-weighted**: more contributions = wider scope radius. Agent
counts querier's records (N). Scope radius = f(N). Heavy contributors see
more; free riders see little. Operating point: the curve f(N).

**Topic-gated**: LLM classifies query topic, restricts to matching partition.
Agent uses /llm/chat to classify the query into topics, then searches for
records tagged with those topics. Operating point: topic matching strictness.

**Time-decay**: older records require stronger topical match. Agent computes
`threshold = f(record_age)`. Young records are broadly visible; old records
require strong tag overlap. Operating point: the decay function.

**Reciprocal**: symmetric visibility. If querier's records are proximate to
user X, user X sees querier and querier sees user X. Computed via proximity
graph with tunable hop depth. Operating point: hop count + proximity threshold.

### Storer-Defined Policies

When data producers define their own access policies, policies are stored as
structured metadata:

```
POST /v1/store {
    "text": "Q3 roadmap...",
    "user_id": "alice",
    "metadata": {"access_policy": "require_topic_overlap", "min_overlap": 0.3}
}
```

The scoping agent reads `metadata.access_policy` for each candidate and
applies rules. The policy is data (not executable code), evaluated by the
scoping agent in batch.

**Cost**: the scoping agent does NOT evaluate all N records. Search narrows
candidates first:

```
100K records in DB
    │ FTS5 search (by querier profile, broad)
    ▼
500 candidates
    │ evaluate per-record metadata policies
    ▼
200 pass
    │ output as whitelist
    ▼
Scope(record_ids=[200 ids])
    │ query agent searches WITHIN whitelist
    ▼
5-10 records actually read
```

Optimizations: group by policy type (4 unique policies = 4 evaluations, not
500); indexable policies translate to SQL WHERE; batch LLM evaluation for
policies requiring reasoning.

---

## Sandbox Architecture

### Components

```
┌──────────────────────────────────────────────────────────────────┐
│ Host Process (hivemind server)              TRUSTED               │
│                                                                  │
│  ┌────────────────────────────┐    ┌──────────────────────────┐  │
│  │ BridgeServer (ephemeral)    │    │ DockerRunner              │  │
│  │                            │    │                          │  │
│  │ GET  /health               │    │ Creates Docker container │  │
│  │ GET  /tools                │    │ on internal network.     │  │
│  │ POST /tools/{name}         │    │                          │  │
│  │ POST /llm/chat (passthru)  │    │ Captures container logs. │  │
│  │                            │    │ Enforces timeout (kill). │  │
│  │ Auth: session token        │    │ Removes container.       │  │
│  │ Budget: min(agent, global) │    │ Cleans up orphans.       │  │
│  └─────────────┬──────────────┘    └────────┬─────────────────┘  │
│                │ 0.0.0.0:PORT               │                    │
│                └─────────────┬──────────────┘                    │
└──────────────────────────────┼───────────────────────────────────┘
                               │
          Docker network (internal=True, no internet)
                               │
                    ┌──────────▼──────────┐
                    │ Agent Container      │    UNTRUSTED
                    │                      │
                    │ Env: BRIDGE_URL      │
                    │   SESSION_TOKEN      │
                    │   PROMPT             │
                    │   DOCUMENT_TEXT      │
                    │                      │
                    │ Isolation:           │
                    │   Own rootfs         │
                    │   PID namespace      │
                    │   Memory/CPU limits  │
                    │   No host filesystem │
                    │   No internet access │
                    │                      │
                    │ All I/O goes through │
                    │ the bridge. Stdout   │
                    │ is the final output. │
                    └──────────────────────┘
```

### Lifecycle

1. `SandboxBackend.run()` generates an ephemeral session token
2. `BridgeServer.start()` binds to a random port on `0.0.0.0`
3. `DockerRunner.run_agent()` creates a Docker container on the internal network
4. Agent communicates exclusively via bridge HTTP calls
5. Agent writes result to stdout (captured via container logs)
6. Container is removed (`force=True`) — always, even on error
7. `BridgeServer.stop()` tears down — session token is now worthless

### Budget Resolution

```
effective_max_calls  = min(agent.max_llm_calls,  global_max_llm_calls)
effective_max_tokens = min(agent.max_tokens,      global_max_tokens)
effective_timeout    = min(agent.timeout_seconds,  global_timeout_seconds)
```

The agent's self-declared limits are CAPPED by global limits. An agent
cannot grant itself a larger budget than the platform allows.

### Network Isolation

| Platform     | Mechanism                                    | Guarantee |
|-------------|----------------------------------------------|-----------|
| All (Docker) | Docker `internal=True` network — only bridge reachable | Hard      |

Each agent container runs on a Docker network with `internal=True`. The
container can reach the bridge server (via `host.docker.internal` on macOS
or the network gateway on Linux) but cannot reach the internet. This provides
hard network isolation on all platforms.

---

## Component Index

```
hivemind/
├── server.py           FastAPI app, REST endpoints, auth, CORS
├── core.py             Hivemind class — store(), query(), _resolve_scope(),
│                       _run_scoping_agent(), _run_indexing_agent()
├── models.py           Pydantic: Scope, StoreRequest, QueryRequest, IndexEntry
├── storage.py          SQLite + FTS5 + Fernet encryption, _apply_scope()
├── tools.py            Tool definitions: search_index, read_record, list_index
├── (filters removed — mediator handles all output constraints)
├── enclave.py          Query pipeline: run_query() orchestrates all 5 stages
├── prompts.py          All LLM prompts (query agent, mediator, index, HyDE)
├── indexing.py         generate_index() via LLM
├── config.py           Settings from env vars (HIVEMIND_*)
│
├── backends/
│   ├── __init__.py        create_backend()
│   ├── openrouter.py      Default: LLM tool-use loop via OpenRouter
│   └── claude_sdk.py      Alternative: Claude SDK backend
│
└── sandbox/
    ├── __init__.py        Exports SandboxBackend, DockerRunner, etc.
    ├── backend.py         SandboxBackend.run() — orchestrates bridge + Docker
    ├── bridge.py          BridgeServer — ephemeral HTTP server (tools + LLM passthrough)
    ├── docker_runner.py   DockerRunner — container lifecycle, internal network, cleanup
    ├── budget.py          Budget — token/call counting and limit enforcement
    ├── models.py          AgentConfig, SandboxSettings, bridge request/response
    └── agents.py          AgentStore — CRUD for registered agent configs (SQLite)
```

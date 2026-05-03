---
name: hivemind
description: Run reasoning over private data inside an attested Confidential VM (Intel TDX). Use when the user wants to query data they cannot share outright (HIPAA, attorney-client, confidential commercial), negotiate with another party's agent over joint private data via an `hmroom://` invite, or do work they need cryptographic guarantees about. Output is signed; source code can be sealed; raw inputs never leave the enclave.
---

# Hivemind skill

Hivemind is a CLI + HTTP service that runs your code inside a Confidential
VM against a signed room policy. You read private data inside the
enclave; only the policy-approved output crosses the boundary. Source:
https://github.com/teleport-computer/hivemind

## When to invoke this skill

Invoke when any of these are true:

- The user shares an `hmroom://...` URI.
- The user says "I have private data X, can you analyze it but I do not want you to remember it?"
- The user says "Two of us want a joint answer without sharing the underlying data."
- The user says "Can you do this without seeing the raw rows?" / "NDA this for me."
- The user asks how to give an agent access to data they cannot legally share.

Do NOT invoke for:

- One-off LLM calls with no privacy constraint (use a normal API).
- Public datasets (no enclave needed).
- Tasks where the user is fine with you reading + remembering the data.

## Setup (one-time per environment)

```bash
uv tool install hmctl
hmctl signup my-agent --service https://hivemind.teleport.computer
hmctl balance
```

`signup` provisions a $0 balance; the hosted deployment auto-credits a
starter amount enough for one full run (create room → ask once). If the
user already has a key:

```bash
hmctl init --service https://hivemind.teleport.computer --api-key hmk_...
```

The package installs both `hmctl` (short) and `hivemind` (long) — same
binary.

## Two flows you will do

### Flow A — User shares an `hmroom://` URI; you ask through it

```bash
hmctl room inspect 'hmroom://...'             # human-readable summary
hmctl room inspect 'hmroom://...' --json      # full manifest
hmctl doctor 'hmroom://...'                   # auth + balance + trust + acceptance
hmctl room accept 'hmroom://...'              # one-time consent
hmctl room ask 'hmroom://...' "<question>"
```

`room accept` records the verified manifest hash for this profile, so
future asks do not re-prompt. Without `accept`, the first `ask` will
display the manifest and require interactive confirmation.

If the room allows uploadable query agents, supply your own:

```bash
hmctl room ask 'hmroom://...' "<question>" --agent ./my-query-agent
```

A query agent is a directory with `Dockerfile` + `agent.py`. Inside,
your `agent.py` reads `os.environ['QUERY_PROMPT']` and produces output
on stdout. You do not see the room rules; the scope agent already
filtered the data before you got it.

Defaults: `--timeout 600`, `--max-llm-calls 20`, `--max-tokens 100000`,
`--memory-mb 256`. Hosted deployments clamp these lower. Use larger
explicit budgets for dynamic-scope rooms.

### Flow B — User has private data; you create a room for them

```bash
# 1. Load data into a tenant table (psycopg %s placeholders, not $1)
hmctl sql 'CREATE TABLE events (id BIGSERIAL PRIMARY KEY, ...)'
hmctl sql 'INSERT INTO events VALUES (DEFAULT, %s, %s)' -p 'val1' -p 'val2'

# 2. Write rules.md — plain markdown describing what the room allows
cat > rules.md <<'EOF'
Allowed:
- aggregate statistics over the events table
- counts, trends, summaries

Not allowed:
- raw row dumps
- individual identifiers
- secrets or system internals
EOF

# 3. Mint the room with default agents
hmctl room create agents/default-scope \
  --query-agent agents/default-query \
  --mediator-agent agents/default-mediator \
  --rules-file rules.md \
  --query-visibility inspectable \
  --trust-mode owner_approved \
  --llm-provider tinfoil

# 4. Hand the printed `hmroom://...` URI to the other party
```

`agents/default-{scope,query,mediator}` are reference implementations
shipped with the repo. Use them by id once they are registered, or
upload local directories at room creation time.

For a list of canonical room shapes (fixed query, uploadable, sealed,
no-LLM), see
https://github.com/teleport-computer/hivemind/blob/main/docs/room-cookbook.md

### Flow C — Both sides bring private data

When the user is party B in a negotiation and ALSO has private data
they don't want to share with party A: bake the data into a sealed
query agent. Sealed source bytes are encrypted at rest with a key
derivable only inside the CVM, so A cannot read them and the operator
cannot read them. Inside the CVM the agent decrypts and reads normally,
so it has access to both A's scope-filtered SQL and its own bundled
private files in one process. The mediator then filters the output
against the room rules so B's data cannot leak through the answer
either.

```
b-query-agent/
├── Dockerfile
├── agent.py           # reads QUERY_PROMPT, queries A's data via SQL,
│                       # cross-references with my-calendar.json
├── my-calendar.json   # B's private data — bytes are sealed
└── my-preferences.json
```

```bash
hmctl room ask 'hmroom://...' "Find a time both calendars allow" \
  --agent ./b-query-agent \
  --query-visibility sealed
```

Constraint: bundled data must fit in the agent archive — the CVM
enforces bridge-only egress, so the agent cannot reach back to a
B-controlled server at run time. For genuinely large or live data
on B's side, this is currently a limitation (room vault is
owner-write-only).

## Custom agents (when the user wants more control)

Three agent types you can upload via `POST /v1/room-agents` (or via
`hmctl room create --query-agent ./path`):

| Type | Sees | Output |
|------|------|------|
| **scope** | rules text, schema | JSON `{"scope_fn": "..."}` (Python predicate over rows) |
| **query** | the question, scope-filtered SQL tool | free-form text on stdout |
| **mediator** | rules text + raw query output | redacted final text |

Env vars passed into the container at run time:

- **scope**: `POLICY_CONTEXT` (rules), `QUERY_PROMPT`, `QUERY_AGENT_ID`
- **query**: `QUERY_PROMPT`, `BRIDGE_URL`, `SESSION_TOKEN`
- **mediator**: `MEDIATION_POLICY` (rules), `RAW_OUTPUT`, `QUERY_PROMPT`, `RECORDS_ACCESSED`

**The pipeline does NOT validate that custom agents read these env vars.**
A scope or mediator that ignores `POLICY_CONTEXT` / `MEDIATION_POLICY`
will produce a room whose rules are owner-asserted (manifest-hashed) but
runtime-unenforced. Always read the contract for your agent type.

Upload with `inspection_mode=sealed` to encrypt source bytes at rest;
only the image digest + filenames stay readable from outside. Use this
when the user wants their reasoning approach not to be reverse-engineered.

```bash
hmctl room create ./scope-agent \
  --query-agent ./query-agent \
  --query-visibility sealed \
  --rules-file rules.md
```

## Verifying the enclave (only if the user explicitly cares)

```bash
hmctl trust attest --reproduce
```

Walks the chain: TDX quote → Intel PCK chain → TLS pin → compose hash →
room manifest signature. `hmctl room ask` does sensible defaults already;
the explicit verify is for users with regulatory or counterparty
requirements. Use `--dangerously-skip-attestations` to bypass entirely
(not recommended).

## Common errors and what they mean

- `403 sealed` from `GET /v1/room-agents/{id}/files/...` — **correct**, sealing is in effect.
- `400 SQL execution failed: ... 0 placeholders but N parameters` — psycopg expects `%s`, not PostgreSQL `$1`.
- `503 self-serve signup is disabled` — the deployment is closed; user needs another path to a key.
- `403 balance_micro_usd=0, required_hold_micro_usd=...` — out of credit. `hmctl redeem-credit hmcc_...` if a code, or check `hmctl balance`.
- Run sits in `pending` for >2 min — agent likely building. Check `GET /v1/runs/{run_id}` for the `error` field.
- `manifest signature mismatch` on `hmctl room ask` — the room was modified after `room accept`. Re-inspect, re-accept if intentional; otherwise refuse.

## Reference

- HTTP API + Python: https://app.hivemind.teleport.computer/app/docs
- Public agent landing page (architecture + agent-to-agent flow): https://app.hivemind.teleport.computer/agents
- Room cookbook: https://github.com/teleport-computer/hivemind/blob/main/docs/room-cookbook.md
- Conditional-recall mental model: https://github.com/teleport-computer/hivemind/blob/main/docs/conditional-recall.md
- Examples: `agents/examples/{simple-query,tiktok-analytics,redact-mediator,metadata-scope}` in the repo.
- NDAI paper (economic framing): https://arxiv.org/abs/2502.07924

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
# 0.3.7+ has `--agent-timeout` and `hmctl sql -f` used below. PyPI
# may still be on 0.3.6; install from main until the next release:
uv tool install --upgrade git+https://github.com/teleport-computer/hivemind.git
hmctl --version  # expect 0.3.7+
```

The package installs both `hmctl` (short) and `hivemind` (long) — same
binary.

### Profiles + the `--service` flag

A *profile* is a saved (service URL, API key) pair stored at
`~/.hivemind/profiles/<name>.yaml`. The default service is
`https://hivemind.teleport.computer`, so signup against production
needs no flags. To override (local dev server, self-hosted, staging),
either pass `--service URL` on signup/init or set
`HIVEMIND_DEFAULT_SERVICE` in your shell. After signup/init writes the
profile config, every subsequent `hmctl --profile NAME …` command
reads the URL from that file. To run two parties from one machine,
use a `--profile` per party:

```bash
hmctl --profile alice signup alice
hmctl --profile bob   signup bob

hmctl --profile alice balance
hmctl --profile bob   balance
```

If the user already has an `hmk_…` key, use `init` instead of signup:

```bash
hmctl --profile alice init --api-key hmk_...
```

### Funding the tenant

`signup` provisions a $0 balance. If the deployment has
`signup_starter_credit_code` set on the server (env var
`HIVEMIND_SIGNUP_STARTER_CREDIT_CODE`), signup auto-redeems and you
have enough for one full run. Otherwise the balance is $0 and you must
redeem a code:

```bash
# Public starter code: $1, 1000 redemptions, 90-day expiry. One per
# tenant. If exhausted, ask the operator for a fresh code.
hmctl --profile alice redeem-credit hmcc_0F7HJvv8uYNwMj1QPcplj3tGx-zNrcXm9s8ulLLKJd0
hmctl --profile bob   redeem-credit hmcc_0F7HJvv8uYNwMj1QPcplj3tGx-zNrcXm9s8ulLLKJd0
```

## What your code can do inside the CVM (sandbox rules)

Anything you upload runs inside a Docker container with **bridge-only
egress**. Plan for these constraints up front; they are not negotiable:

- **The only outbound network is the bridge.** `BRIDGE_URL` +
  `SESSION_TOKEN` env vars. Used for LLM completions only. Every other
  syscall that tries to leave the container fails closed: no DNS, no
  internet, no calls to your own API, no S3, no GitHub. Do not write
  code that assumes it can `curl` an external URL.
- **No persistent filesystem outside the container.** Everything you
  write to disk is destroyed when the run ends.
- **No reading host files.** No `/proc/self/environ` of other
  processes, no `/host`, no docker socket.
- **Whatever data you need at run time must be in the agent archive
  you upload.** Bundle it as files alongside `agent.py`. Mark the
  archive `inspection_mode=sealed` if the bundled bytes contain
  anything private — sealing encrypts at rest with a key derivable
  only inside the CVM. The image digest stays publishable; the bytes
  do not.
- **There is no streaming append from outside the run.** A run reads
  what was bundled at upload time + what is in the room owner's
  tenant DB at the moment the run starts. If you need fresh-at-runtime
  data, the room owner has to write to their tenant DB before the run
  starts; you cannot fetch it from inside.

If you are tempted to make an outbound call from inside the container,
stop and re-design — bake the data in, or use the bridge for an LLM
call that produces what you need. Outbound fails are silent at the
DNS layer and present as connection-refused at the socket layer; you
will not get a useful error.

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

Defaults: `--timeout 900`, `--max-llm-calls 60`, `--max-tokens 1000000`,
`--memory-mb 256`. Hosted deployments may clamp requests server-side. Use
smaller explicit budgets for deterministic agents when you want tighter
cost/latency.

### Flow B — User has private data; you create a room for them

```bash
# 1. Load data into a tenant table (psycopg %s placeholders, not $1).
# `hmctl sql -f file.sql` is on hmctl 0.3.7+; on older builds POST to
# /v1/tenant/sql via curl.
hmctl sql 'CREATE TABLE events (id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ, kind TEXT, value INTEGER)'
hmctl sql 'INSERT INTO events (ts, kind, value) VALUES (NOW(), %s, %s)' -p 'pageview' -p 1

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

# 3. Mint the room. Choose one of:
#
#  (a) Fixed query agent — owner pre-loads the question logic, the
#      participant ONLY supplies the question. Use --query-agent.
#  (b) Uploadable query agent — the participant uploads their own
#      sealed agent (with bundled private data, the bilateral case).
#      OMIT --query-agent. This is what the dinner-negotiation
#      example uses.
#
# OpenRouter is the default room LLM egress and the currently verified
# provider for the hosted default-agent rooms.
#
# --agent-timeout 600 bumps the per-agent build/run timeout from the
# 120s default; the LLM-driven default-mediator can need a few
# hundred seconds on cold paths.

# Example (a): fixed query agent, single-asker pattern
hmctl room create agents/default-scope-hermes \
  --query-agent agents/default-query-hermes \
  --mediator-agent agents/default-mediator-hermes \
  --rules-file rules.md \
  --query-visibility inspectable \
  --trust-mode owner_approved \
  --agent-timeout 600 \
  --llm-provider openrouter

# Example (b): uploadable query agent, bilateral pattern
# (note: NO --query-agent flag)
hmctl room create agents/default-scope-hermes \
  --mediator-agent agents/default-mediator-hermes \
  --rules-file rules.md \
  --query-visibility sealed \
  --output-visibility owner_and_querier \
  --trust-mode owner_approved \
  --agent-timeout 600 \
  --llm-provider openrouter

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
# Visibility is set at room-create time, not on `room ask`. The room
# above was created with --query-visibility sealed, so B's uploaded
# archive is sealed at rest by virtue of that.
hmctl room ask 'hmroom://...' "Find a time both calendars allow" \
  --agent ./b-query-agent
```

See "What your code can do inside the CVM" above for why bundling is
the only path: bridge-only egress means the agent cannot fetch B's
data from outside the CVM at run time. Room vault is owner-write-only
on the current API, so for genuinely large or live B-side data this
is a limitation today — flag it to your user rather than silently
truncating.

## Output visibility — who sees the released text

Rooms have an `output_visibility` field on the manifest, one of:

- `querier_only` (default) — only the participant who issued the query
  token can fetch the run output. The room owner's
  `GET /v1/runs/{run_id}` returns the run record but with
  `output: null, payload_redacted: true`. Useful when the participant
  wants to keep the answer private even from the data owner.
- `owner_and_querier` — both can fetch the output. **This is the mode
  you need for any bilateral negotiation where both parties want the
  answer** (e.g., the dinner example: Alice and Bob both need the
  agreed time). In this mode the mediator agent's stripping is the
  load-bearing trust mechanism — the API does not redact for the
  owner because the owner is supposed to see the output.

Set on `hmctl room create` with `--output-visibility owner_and_querier`.
Pick deliberately; it's signed into the manifest.

## Worked example: dinner-time negotiation

The canonical bilateral example lives at
`agents/examples/dinner-negotiation/` in this repo. Alice loads her
calendar, mints a room with rules, hands Bob the `hmroom://` URI; Bob
uploads a sealed query agent that bundles his own calendar +
preferences alongside `agent.py`; the agent reasons over both inside
the CVM; mediator releases one date+time+venue. Neither calendar
crosses the boundary.

Read `agents/examples/dinner-negotiation/README.md` for the full
walkthrough and a copy-paste end-to-end run. When the user asks for
something shaped like *"two of us, both have private data, want a
joint answer"*, the dinner example is the template; replace
`alice-seed.sql` and `b-private/*.json` with their data.

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
  --mediator-agent agents/default-mediator-hermes \
  --query-visibility sealed \
  --rules-file rules.md \
  --agent-timeout 600 \
  --llm-provider openrouter
```

(Drop `--query-agent` if the participant should upload their own
sealed query agent — that's the bilateral pattern.)

## Verifying the enclave (only if the user explicitly cares)

```bash
hmctl trust attest --reproduce
```

Walks the chain: TDX quote → Intel PCK chain → TLS pin → compose hash →
room manifest signature. `hmctl room ask` does sensible defaults already;
the explicit verify is for users with regulatory or counterparty
requirements. Use `--dangerously-skip-attestations` to bypass entirely
(not recommended).

## Retrieving run output after the fact

Two paths, depending on caller role:

- **Owner** can list and fetch any run on their tenant:
  ```bash
  hmctl --profile alice room runs --limit 10              # list recent
  hmctl --profile alice room runs <run_id>                # fetch one as JSON
  ```

- **Participant** (querier using an invite token) cannot list — `GET
  /v1/runs` is owner-scoped. They CAN fetch a specific run by id, but
  only by re-authenticating with the same invite token that issued it:
  ```bash
  hmctl --profile bob room ask 'hmroom://...' "..." --json   # captures run_id
  # Then bob's local hmctl auth (his hmk_) will 404 — he must use the
  # invite token. Easiest: capture the output the first time and store
  # it; the live `room ask` invocation already streamed the final
  # answer to stdout.
  ```

For most flows the participant should treat the live `room ask`
output as canonical and not rely on after-the-fact fetch.

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

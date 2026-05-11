# Dinner-time negotiation — bilateral private data

Worked example of the **two-friend negotiation** pattern: Alice and Bob
each hold a private calendar, neither wants to share theirs with the
other, but both want a dinner time that works for them both. The whole
flow runs inside a Hivemind room. Only the agreed time and venue
suggestion crosses the enclave boundary — neither calendar is revealed.

This is the canonical bilateral example. If you are an LLM agent
reading the `hivemind` skill, this is what your output should look like
when your user asks "find a time both of us can do dinner without
sharing our calendars."

## Roles

- **Alice (room owner)** loads her calendar into a tenant table, writes
  rules, mints the room with the example's deterministic scope agent
  and the default mediator agent.
- **Bob (participant)** uploads a *sealed* query agent that bundles his
  own calendar and preferences alongside the agent code. Bob asks the
  question.

The CVM gives Bob's agent both:
1. SQL access to Alice's calendar, filtered by the scope agent's
   `scope_fn` (only `is_busy=false` rows, only `start_time` and
   `end_time` columns).
2. Read access to Bob's bundled private files (decryptable only inside
   the CVM because of `inspection_mode=sealed`).

Bob's agent computes the intersection, picks a venue, returns the
answer. The mediator strips anything that violates Alice's rules
before release.

## What's in this directory

```
dinner-negotiation/
├── README.md                       # this file
├── rules.md                        # Alice's room rules (markdown)
├── alice-seed.sql                  # creates calendar table + 14 days of synthetic data, NOW()-relative
├── scope-agent/
│   ├── Dockerfile                  # bases off hivemind-agent-base
│   └── agent.py                    # deterministic scope_fn — used INSTEAD of default-scope so the demo runs reliably
└── bob-query-agent/
    ├── Dockerfile                  # bases off hivemind-agent-base
    ├── agent.py                    # uses stdlib urllib (no pip install at build time)
    └── b-private/
        ├── my-calendar.json        # Bob's private calendar (sealed at upload)
        └── my-preferences.json     # Bob's private preferences (sealed at upload)
```

`b-private/` is the convention this example uses for bundled private
data; you can name it anything. What matters is that the whole
`bob-query-agent/` directory is uploaded with `inspection_mode=sealed`,
which encrypts the source bytes (including `b-private/` files) at
rest. Inside the CVM the container decrypts and reads them normally.

## Why a custom scope agent

`agents/default-scope/` is the production code path — it's an
LLM-driven scope agent that reads `POLICY_CONTEXT` and writes a
`scope_fn` it judges policy-compliant. It works for arbitrary rules,
but the LLM step is non-deterministic and on simple structural rules
like ours occasionally emits an over-restrictive function that returns
0 rows. The example ships its own deterministic scope agent (~30
lines, no LLM calls) so demo runs are reliable. For your own
production rooms with prose-shaped rules, prefer the default; for
demos and structural filters, prefer hand-rolled.

## Running it end-to-end

You need two distinct hmctl profiles — one for Alice, one for Bob.
Both can be self-serve signups against the public service. Whether
the starter credit auto-applies depends on the deployment's
`signup_starter_credit_code` setting; if it doesn't, redeem a credit
code manually (your operator publishes one).

### Prereqs

`hmctl` 0.3.7 has the `--agent-timeout` flag and the `hmctl sql`
subcommand this example uses. PyPI may still be on 0.3.6; install
from main until the next release:

```bash
uv tool install --upgrade git+https://github.com/teleport-computer/hivemind.git
hmctl --version  # expect 0.3.7+
```

(If the build complains about `git+...`, run `uv tool install hmctl`
to get whatever's on PyPI; you'll need to use the curl fallback for
seeding and skip the `--agent-timeout` flag.)

About `--profile` and `--service`: a profile is a saved (service
URL, API key) pair stored at `~/.hivemind/profiles/<name>.yaml`. The
default service is `https://hivemind.teleport.computer`, so no flag
is needed for production use. Override with `--service URL` on
signup/init or set `HIVEMIND_DEFAULT_SERVICE=...` in your shell to
target local dev or a self-hosted deployment.

### Alice — provision, seed, mint room

```bash
hmctl --profile alice signup alice
hmctl --profile alice balance

# If balance is $0 (the deployment hasn't enabled auto-credit yet),
# redeem the public starter code. This code has 1000 redemptions
# capped at $1 each; one redemption per tenant. If exhausted, ask
# the operator for a fresh code.
hmctl --profile alice redeem-credit hmcc_0F7HJvv8uYNwMj1QPcplj3tGx-zNrcXm9s8ulLLKJd0

# Seed Alice's calendar table.
# On hmctl 0.3.7+:
hmctl --profile alice sql -f agents/examples/dinner-negotiation/alice-seed.sql

# On older hmctl, send the SQL via curl (one statement at a time, or
# wrap the whole file in a single call):
#   curl -sS https://hivemind.teleport.computer/v1/tenant/sql \
#     -H "Authorization: Bearer $ALICE_API_KEY" \
#     -H 'Content-Type: application/json' \
#     -d "$(jq -Rs '{sql: .}' < agents/examples/dinner-negotiation/alice-seed.sql)"

# Create the room. CRUCIAL: do NOT pass --query-agent — that pins a fixed
# query agent and disables the participant's --agent upload, defeating
# the bilateral pattern.
#
# --output-visibility owner_and_querier: Alice wants the answer too.
# --query-visibility sealed: Bob's uploaded agent will be sealed at rest.
# --agent-timeout 600: bumps per-agent build/run timeout from 120s default;
#   the LLM-driven default mediator can need a couple hundred seconds.
# --llm-provider openrouter --model z-ai/glm-5: the currently verified
#   hosted default provider/model pair.
hmctl --profile alice room create agents/examples/dinner-negotiation/scope-agent \
  --mediator-agent agents/default-mediator-hermes \
  --rules-file agents/examples/dinner-negotiation/rules.md \
  --output-visibility owner_and_querier \
  --query-visibility sealed \
  --trust-mode owner_approved \
  --agent-timeout 600 \
  --llm-provider openrouter

# Alice gets back: hmroom://...
# She hands the URI to Bob (out of band).
```

### Bob — provision, inspect, ask with sealed agent + bundled data

```bash
hmctl --profile bob signup bob
hmctl --profile bob balance
# If balance is $0:
hmctl --profile bob redeem-credit hmcc_0F7HJvv8uYNwMj1QPcplj3tGx-zNrcXm9s8ulLLKJd0

ROOM='hmroom://...'  # paste the URI Alice shared
hmctl --profile bob room inspect "$ROOM"
hmctl --profile bob room accept "$ROOM"

# Ask. Supplies Bob's own sealed query agent with bundled private data.
# Note: there is no --query-visibility flag on `room ask` — visibility
# is set at upload-time (the agent's registered inspection_mode) or
# at room-create-time. Bob's bundled archive is sealed by virtue of
# the room being created with --query-visibility sealed.
hmctl --profile bob room ask "$ROOM" \
  "Find a Thursday or Friday evening this week that works for both of us, near Mission, no pasta." \
  --agent agents/examples/dinner-negotiation/bob-query-agent \
  --provider openrouter
```

The output should be a single short line, e.g.,
`"Thursday 7pm at Burma Superstar (Mission). Both calendars are free
and the venue avoids pasta."`. Nothing else: not Bob's other busy
slots, not Alice's other appointments, not the venues Bob doesn't
like. That's the mediator doing its job.

## What this verifies

- **Sealing**: Alice cannot read `b-private/my-calendar.json` via
  `GET /v1/room-agents/{bob_agent_id}/files/b-private/my-calendar.json`
  — server returns `403 sealed`. The image digest is publishable,
  the bytes are not.
- **Scope filtering**: Bob's agent calls `execute_sql` on Alice's
  `calendar` table; the scope_fn allows only `is_busy=false` rows and
  only `start_time`/`end_time` columns through. Even a `SELECT *`
  cannot pull `notes`/`location`/`attendees`.
- **Mediator stripping**: if Bob's agent (accidentally or
  maliciously) emits Alice's other appointments or its own bundled
  data verbatim, the mediator strips it before release.
- **Bridge-only egress**: the agent has `BRIDGE_URL` for LLM calls
  but cannot reach any other URL. agent.py uses stdlib `urllib`
  pointing at `BRIDGE_URL` only.

## Adapting this for your own use case

The pattern generalizes to any bilateral negotiation over private
state. Replace:

- `alice-seed.sql` → your data owner's tenant table.
- `rules.md` → the policy text you want both parties to agree on.
- `scope-agent/agent.py` → the structural filter for your rules.
  Or drop this and use `agents/default-scope` if your rules are
  prose-shaped and you can tolerate LLM variance.
- `bob-query-agent/b-private/*` → your participant's private data
  files, however shaped.
- `bob-query-agent/agent.py` → the reasoning logic. Most of it is
  reusable; the `combine_sources()` block is the part you tailor.

Common variants:

- **Pricing negotiation** — buyer and seller each have a reservation
  price, a counterparty agent finds the overlap and reports a
  midpoint, neither side learns the other's price.
- **Code review without sharing source** — owner contributes
  proprietary code; reviewer contributes review heuristics; output is
  a structured findings list, not the code.
- **Medical second opinion** — patient contributes a record (room
  vault); clinician's agent reads + reasons + emits diagnosis, no raw
  record leaks back.

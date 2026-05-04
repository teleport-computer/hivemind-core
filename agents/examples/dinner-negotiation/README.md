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
  rules, mints the room with the default scope and mediator agents.
- **Bob (participant)** uploads a *sealed* query agent that bundles his
  own calendar and preferences alongside the agent code. Bob asks the
  question.

The CVM gives Bob's agent both:
1. SQL access to Alice's calendar, filtered by Alice's scope agent
   (only available evening slots are visible — not the rest).
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
├── alice-seed.sql                  # creates Alice's calendar table + 2 weeks of synthetic data
└── bob-query-agent/
    ├── Dockerfile
    ├── requirements.txt
    ├── agent.py                    # the query agent — reads bundled JSON + SQLs Alice's calendar
    └── b-private/
        ├── my-calendar.json        # Bob's private calendar (sealed at upload)
        └── my-preferences.json     # Bob's private preferences (sealed at upload)
```

`b-private/` is the convention this example uses for bundled private
data; you can name it anything. What matters is that the whole
`bob-query-agent/` directory is uploaded with `inspection_mode=sealed`,
which encrypts the source bytes (including the `b-private/` files) at
rest. Inside the CVM the container decrypts and reads them normally.

## Running it end-to-end

You need two distinct hmctl profiles — one for Alice, one for Bob.
Both can be self-serve signups against the public service; the starter
credit covers one full run.

```bash
# 0. From the repo root (hivemind/), so paths below resolve correctly.

# 1. Provision Alice
hmctl --profile alice signup alice --service https://hivemind.teleport.computer
hmctl --profile alice balance

# 2. Seed Alice's calendar table
hmctl --profile alice sql -f agents/examples/dinner-negotiation/alice-seed.sql

# 3. Alice creates the room with default scope/mediator + her rules.
#    output_visibility=owner_and_querier because Alice wants the answer too.
hmctl --profile alice room create agents/default-scope \
  --query-agent agents/default-query \
  --mediator-agent agents/default-mediator \
  --rules-file agents/examples/dinner-negotiation/rules.md \
  --output-visibility owner_and_querier \
  --query-visibility sealed \
  --trust-mode owner_approved \
  --llm-provider tinfoil

# Alice gets back: hmroom://...
# She hands the URI to Bob (out of band).

# 4. Provision Bob
hmctl --profile bob signup bob --service https://hivemind.teleport.computer
hmctl --profile bob balance

# 5. Bob inspects + accepts the room
ROOM='hmroom://...'  # paste the URI Alice shared
hmctl --profile bob room inspect "$ROOM"
hmctl --profile bob room accept "$ROOM"

# 6. Bob asks, supplying his own sealed query agent (with bundled private data)
hmctl --profile bob room ask "$ROOM" \
  "Find a Thursday or Friday evening this week that works for both of us, near Mission, no pasta." \
  --agent agents/examples/dinner-negotiation/bob-query-agent
```

The output should look like a single line, e.g.,
`"Thursday 7pm at Burma Superstar (Mission)"`. Nothing else: not Bob's
calendar, not Alice's other appointments, not the venues Bob doesn't
like. That's the mediator doing its job.

## What this verifies

- **Sealing**: Alice cannot read `b-private/my-calendar.json` via
  `GET /v1/room-agents/{bob_agent_id}/files/b-private/my-calendar.json`
  — server returns `403 sealed`. The image digest is publishable,
  the bytes are not.
- **Scope filtering**: Bob's agent calls `execute_sql` on Alice's
  `calendar` table; only rows the scope agent's `scope_fn` allows are
  returned. Raw `SELECT * FROM calendar` returns only the policy-
  permitted subset.
- **Mediator stripping**: if Bob's agent (accidentally or
  maliciously) emits Alice's other appointments or its own bundled
  data verbatim, the mediator strips it before release.
- **Bridge-only egress**: the agent has `BRIDGE_URL` for LLM calls
  but cannot reach any other URL. Plan the agent's logic accordingly.

## Adapting this for your own use case

The pattern generalizes to any bilateral negotiation over private
state. Replace:

- `alice-seed.sql` → your data owner's tenant table.
- `rules.md` → the policy text you want both parties to agree on.
- `bob-query-agent/b-private/*` → your participant's private data
  files, however shaped.
- `bob-query-agent/agent.py` → the reasoning logic. Most of it is
  reusable; the `combine_sources()` function is the part you tailor.

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

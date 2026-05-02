# Hivemind

Hivemind runs **attested recall agreements** between mutually
distrusting parties. An owner defines the room rules and contributes private
data plus a scope agent; a participant verifies those rules and the running
enclave before asking through a fixed query agent or bringing their own. Only
the room-approved output leaves the enclave.

## Install

Requires Python 3.11+, `uv`, Docker for agent builds/runs, and Postgres for
local development.

For regular CLI users, install the CLI into uv's isolated tool environment:

```bash
uv tool install hmctl
hmctl --version
```

The package installs two equivalent commands: `hmctl` for the short public CLI
name, and `hivemind` for existing scripts.

To test an unreleased checkout directly from GitHub:

```bash
uv tool install --upgrade git+https://github.com/teleport-computer/hivemind-core.git
hmctl --version
```

To upgrade later:

```bash
uv tool upgrade hmctl
```

For repo development, use an editable install from the checkout:

```bash
uv tool install --editable .
hmctl --help
```

## Quick Use

Join an existing room:

```bash
hmctl profile use my-tenant
ROOM='hmroom://...'

hmctl room inspect "$ROOM"
hmctl doctor "$ROOM"
hmctl room inspect "$ROOM" --json | jq '.room.manifest'
hmctl room accept "$ROOM"
hmctl balance
hmctl -y room ask "$ROOM" "What changed this month?"
```

`room inspect` shows the signed room spec and live attestation summary; use
`--json` to inspect the full manifest. `doctor ROOM` checks the CLI version,
active profile, service auth, billing balance, room trust, and local room
acceptance in one place. `room accept` records the verified manifest hash for
this local profile; if you skip it, the first `room ask` prompts before sending
your question. `-y` does not accept room manifests.
`--dangerously-skip-attestations` bypasses both attestation checks and this
first-use manifest acceptance gate. `room ask` defaults to `--timeout 600`,
`--max-llm-calls 20`, and `--max-tokens 100000`.
Hosted deployments can clamp requests lower than what you ask for; the current
Phala deployment caps runtime at 900s, LLM calls at 100, and tokens at 1000000.

For invite-token room asks, the CLI bills the active `hmk_` tenant profile
automatically. The room token still controls what data can be read; the active
tenant credential controls whose credits pay for the scope/query/mediator run.
To use a different tenant, switch profiles or pass `--profile NAME` before the
command.

Create a fixed-query room and share the printed invite:

```bash
hmctl room create ./scope-agent \
  --name example-room \
  --query-agent ./query-agent \
  --mediator-agent ./mediator-agent \
  --scope-visibility inspectable \
  --query-visibility inspectable \
  --rules-file rules.md \
  --trust-mode owner_approved
```

The rules file is plain text; Markdown is conventional because humans read
and sign it. The same text is used as the scope/mediator policy unless
`--policy-file` is passed. Use YAML only if your own agents are written to
interpret YAML.

Connect a local profile to a deployed service:

```bash
hmctl init --service https://hivemind.example --api-key hmk_...
hmctl trust attest --reproduce
```

If the hosted service has self-serve signup enabled, users can create their
own tenant key with a `$0.00` starting balance. Admin-issued credit codes can
be redeemed later when you want to add prepaid credit:

```bash
hmctl --profile alice signup alice --service https://hivemind.example
hmctl redeem-credit 'hmcc_...'
```

Admins mint credit codes with tracked max uses and expiry:

```bash
hmctl admin credit-codes create --credit 3.00 --uses 1 --expires-in 7d
hmctl admin credit-codes list
hmctl admin billing accounts
hmctl admin billing ledger
hmctl admin tenants reset-key t_... --clear-seal --revoke-capabilities
```

Operator switches:

```bash
HIVEMIND_SELF_SERVE_SIGNUP_ENABLED=true
HIVEMIND_BILLING_ENFORCE_CREDITS=true
```

Credit codes are not signup codes: signup is open when enabled, and credit is
added only through `hmctl redeem-credit` or the credit-code redemption API.

For local development:

```bash
./scripts/quickstart.sh
hmctl init --service http://localhost:8100 --api-key hmk_...
```

For copy-paste room setups, see the [room cookbook](docs/room-cookbook.md).

## Evaluation

Active agent evaluation lives in [`eval/`](eval/). It is room-native and
starts with deterministic checks for leakage, output shape, latency, tool
calls, tokens, and cost. The old GAN-style benchmark is archived under
[`autoresearch/legacy_bench/`](autoresearch/legacy_bench/) for historical
context only; do not use it as the optimization target for new agents.

## Room Data Flow

```text
owner data + scope agent         participant question or query agent
          \                     /
           \                   /
            signed room manifest
                    |
             dstack CVM attestation
                    |
        scope agent builds the data boundary
                    |
          query agent receives scoped tools
                    |
          pinned mediator audits the answer
                    |
          signed, room-approved output
```

A signed room is the runtime agreement. Its manifest binds:

- the scope agent and whether its source is inspectable or sealed;
- the query mode: fixed query agent or participant-uploaded query agent;
- query-agent and mediator-agent visibility: `inspectable` or `sealed`;
- the pinned mediator agent when one is configured;
- output visibility;
- allowed LLM providers and artifact egress;
- deployment trust policy.

The service is designed for a dstack Confidential VM. Both parties verify the
live CVM attestation before presenting data, invite tokens, or agent code.

Important restart property: room data and room-uploaded sealed query agents
are encrypted under a per-room key wrapped to the owner key and invite token.
After a restart or backend update, private room material stays unreadable until
an owner or participant presents a valid room credential again.

## Threat Model

- Protects against: the counterparty reading raw data or agent source beyond
  the signed room policy; backend updates changing execution without observable
  attestation; a restarted backend reading sealed room material before a valid
  room credential is presented.
- Does not protect against: a party's own agent or allowed output revealing
  what that party is authorized to learn; bad room rules that the participant
  accepts; bugs in the trusted computing base, TEE, cryptography, or agent
  sandbox.
- You trust: dstack/TDX attestation, the measured CVM image accepted by the
  room trust policy, the owner signing key in the invite link, and the local
  client verification path.

## Why This Exists

Clean rooms are usually data-first and administrator-driven. Agent frameworks
usually assume one trust domain. Hivemind is for the gap between them:
two parties want an agent-mediated answer, neither party wants to reveal raw
material to the other, and both need to verify the computation before any
private input is read.

The design follows the conditional-recall framing from
[NDAI: Non-Disclosure Agreements for AI](https://arxiv.org/abs/2502.07924):
let an AI system use private context for a bounded purpose, then constrain what
can be recalled outside that purpose. See the
[mental model](docs/conditional-recall.md) for the room data flow and the
scope/query agent relationship.

## What An Agent Is

An agent upload is a local directory or `.tar.gz` archive with a `Dockerfile`
and source files. The simplest shape is:

```text
agent-dir/
|-- Dockerfile
`-- agent.py
```

A scope agent defines the room's data boundary. A query agent performs the
participant's task through scoped tools. Examples live in:

- `agents/default-scope/`
- `agents/default-query/`
- `agents/default-mediator/`
- `agents/examples/simple-query/`
- `agents/examples/tiktok-analytics/`

## Owner Flow

Canonical flow: create a signed room, add private data, and share the invite.

```bash
hmctl room create ./scope-agent \
  --mediator-agent ./mediator-agent \
  --rules-file rules.md

hmctl room add-data <room_id> --file dataset.md --meta source=dataset
hmctl room data <room_id>
```

The create command prints one `hmroom://...` invite link. That link contains
the room id, invite token, service URL, and owner signing public key.

Common room variants:

```bash
# Owner pre-loads the query logic; participant only supplies the question.
hmctl room create ./scope-agent \
  --query-agent ./query-agent \
  --mediator-agent ./mediator-agent \
  --rules-file rules.md

# Participant can upload their own query agent for this room.
hmctl room create ./scope-agent \
  --mediator-agent ./mediator-agent \
  --query-visibility sealed \
  --rules-file rules.md
```

Visibility modes:

- `inspectable`: participants can read extracted source files, pin digests,
  and see stored prompts for room runs.
- `sealed`: participants see metadata and digests, but source bytes are
  encrypted and are not served through the files API. Run prompts are not
  stored as plaintext; signed run attestations still include the prompt hash.

Dynamic scope/query/mediator rooms need an allowed LLM provider, usually the
default Tinfoil egress or an explicit `--llm-provider openrouter`. `--no-llm`
is only an egress-deny policy for pinned agents that do not call LLM endpoints.

## Participant Flow

Canonical flow: inspect the agreement, ask the question, and verify the
attested output.

```bash
hmctl room inspect 'hmroom://...'
hmctl room inspect 'hmroom://...' --json | jq '.room.manifest'
hmctl room accept 'hmroom://...'
hmctl room ask 'hmroom://...' "What changed this month?"
```

Bring a query agent to an uploadable room:

```bash
hmctl room ask 'hmroom://...' "What changed this month?" \
  --agent ./my-query-agent
```

`room accept` saves the verified manifest hash for the active local profile.
Without it, the first `room ask` displays the manifest summary and asks for
confirmation before sending the prompt. Every answer is checked against the
accepted room manifest hash and the live CVM run signer. The default behavior
is fail-closed when the run attestation is missing or does not match the room.

Ask defaults are intentionally small: `--timeout 600`,
`--max-llm-calls 20`, `--max-tokens 100000`, and `--memory-mb 256`.
For dynamic scope/query/mediator rooms, use larger explicit budgets when the
scope agent needs to inspect, simulate, and verify the query agent.

If the service has billing enabled, invite-token room asks are charged to the
active `hmk_` tenant profile. Use `hmctl profile use NAME` or pass
`--profile NAME` before the command to choose which tenant API key pays. The data
owner does not pay for participant queries unless the owner is the caller.

## Trust Policy

Rooms have one deployment trust policy:

- `operator_updates`: trust the operator's governance process to approve
  enclave upgrades.
- `pinned`: trust only the exact compose hashes accepted at room creation.
- `owner_approved`: trust the room owner to maintain this room's compose-hash
  allowlist.

Update a room trust allowlist without changing the invite link:

```bash
hmctl room trust <room_id> --mode owner_approved --approve-live
```

Bulk cleanup is dry-run first. Keep current invite links explicitly:

```bash
hmctl room prune --name-prefix watch-history- --keep room_...
hmctl -y room prune --name-prefix watch-history- --keep room_... --no-dry-run
```

Production HTTPS clients require DCAP quote verification and TLS pinning by
default. `hmctl trust attest --reproduce` walks the source chain from the
attested compose hash to the registered compose source and any deterministic
deploy render hints. The global `--dangerously-skip-attestations` flag is an
explicit bypass for tenants or operators who choose not to perform client-side
attestation for a command.

## Public API

The public API is room-first:

```text
Room lifecycle
POST   /v1/rooms
GET    /v1/rooms
GET    /v1/rooms/{room_id}
GET    /v1/rooms/{room_id}/attest
GET    /v1/rooms/{room_id}/key
POST   /v1/rooms/{room_id}/open
POST   /v1/rooms/{room_id}/trust
DELETE /v1/rooms/{room_id}

Room contents and execution
POST /v1/rooms/{room_id}/data
GET  /v1/rooms/{room_id}/data
POST /v1/rooms/{room_id}/runs
POST /v1/rooms/{room_id}/query-agents

Agent inspection
POST /v1/room-agents
GET  /v1/room-agents
GET  /v1/room-agents/{agent_id}
GET  /v1/room-agents/{agent_id}/attest
GET  /v1/room-agents/{agent_id}/files
GET  /v1/room-agents/{agent_id}/files/{path}

Observation
GET /v1/runs
GET /v1/runs/{run_id}
GET /v1/runs/{run_id}/artifacts/{filename}
GET /v1/attestation

Billing/admin
POST /v1/signup
GET  /v1/billing
POST /v1/billing/credit-codes/redeem
GET  /v1/admin/billing
GET  /v1/admin/billing/ledger
GET  /v1/admin/billing/{tenant_id}
POST /v1/admin/billing/{tenant_id}/credits
GET  /v1/admin/credit-codes
POST /v1/admin/credit-codes
POST /v1/admin/credit-codes/{code_id}/revoke
GET  /v1/admin/billing/prices
POST /v1/admin/billing/prices
```

Lower-level SQL, token, and generic agent endpoints are internal
implementation details. New clients should use the room API only.

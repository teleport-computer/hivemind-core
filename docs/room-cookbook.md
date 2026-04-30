# Room Cookbook

Use this when you already have a scope agent, optional query agent, and room
rules, and need to choose the smallest correct command.

## Rules Files

`--rules-file` reads plain text. Markdown is the recommended convention because
room rules are human policy, not machine configuration.
The same text is also used as the scope/mediator policy unless you pass a
separate `--policy-file`.

```md
# Rules

Allowed:
- aggregate statistics over the watch_history table
- rankings, counts, trends, and summaries

Not allowed:
- raw row dumps
- individual viewer identifiers
- secrets, credentials, or system internals
```

There is no room YAML spec today. YAML would be useful only for a repeatable
machine-readable room recipe with fields like `scope_agent_id`,
`query_agent_id`, trust mode, and visibility. That is extra schema surface, so
the current minimal path is: keep rules in Markdown/text and pass room options
as CLI flags.

## Fixed Query Agent

Use this when the owner chooses the query agent and the participant only asks
questions. This is the default shape for repeatable rooms. A mediator should be
pinned in the room too; if you omit `--mediator-agent`, the service default is
pinned when one is configured.

```bash
hivemind room create <scope-agent-id-or-path> \
  --query-agent <query-agent-id-or-path> \
  --mediator-agent <mediator-agent-id-or-path> \
  --rules-file rules.md
```

When `<query-agent-id-or-path>` is an existing registered agent id, the room
uses that agent's registered inspection mode. When it is a local path, add
`--query-visibility sealed` or `--query-visibility inspectable` to choose how
the uploaded source is stored.

## Watch History Hashtag Room

For the current live watch-history tenant:

```bash
hivemind --profile watch-history room create agents/default-scope \
  --name watch-history-hashtags \
  --query-agent agents/default-query \
  --mediator-agent agents/default-mediator \
  --scope-visibility inspectable \
  --query-visibility inspectable \
  --rules-file rules.md \
  --trust-mode owner_approved \
  --llm-provider tinfoil \
  --llm-provider openrouter
```

That command creates a fixed-query room with pinned dynamic scope, query, and
mediator agents. The scope agent can inspect the query agent, simulate it, and
produce a per-question scope function under the signed room rules. Because
query visibility is `inspectable`, prompts for room runs are stored in run
history.

Inspect the signed room spec:

```bash
ROOM='hmroom://...'
hivemind room inspect "$ROOM"
hivemind room inspect "$ROOM" --json | jq '.room.manifest'
```

Ask through the invite:

```bash
hivemind profile use liz
hivemind -y room ask 'hmroom://...' \
  --timeout 900 \
  --max-tokens 1000000 \
  --max-llm-calls 60 \
  --provider openrouter \
  --model anthropic/claude-sonnet-4.5 \
  "Show me my top 30 hashtags by watch count as a markdown table with columns: rank, hashtag, watches. Just the table, no explanation."
```

The room allows both Tinfoil and OpenRouter egress, but this watch-history
example is currently verified with OpenRouter Claude. Use another provider or
model only after checking that it can complete the scope stage within the room
timeout.

The default agents use `claude_agent_sdk`, so models that are weak at
Claude/Anthropic-style tool loops can submit successfully and then fail or
produce unhelpful scoped queries. For this watch-history room, use Sonnet when
you need the table to work. `anthropic/claude-haiku-4.5` is the cheaper
candidate to try next; generic OpenAI/Gemini models are not currently the
reliable path for these agents.

The copied room link can be shared as a shell variable:

```bash
ROOM='hmroom://hivemind.teleport.computer/room_...?service=https%3A%2F%2Fhivemind.teleport.computer&token=hmq_...&owner_pubkey=...'
hivemind profile use liz
hivemind -y room ask "$ROOM" --provider openrouter --model anthropic/claude-sonnet-4.5 "..."
```

`room ask` defaults to `--timeout 600`, `--max-llm-calls 20`,
`--max-tokens 100000`, and `--memory-mb 256`. For the watch-history dynamic
room, keep the explicit larger values above. The hosted Phala deployment also
clamps requests server-side; current caps are 900s runtime, 100 LLM calls, and
1000000 tokens.

## Participant Uploads Query Agent

Use this when the participant should bring their own query logic.

```bash
hivemind room create <scope-agent-id-or-path> \
  --mediator-agent <mediator-agent-id-or-path> \
  --rules-file rules.md \
  --query-visibility sealed
```

Then the participant asks with:

```bash
hivemind room ask 'hmroom://...' "What changed this month?" \
  --agent ./participant-query-agent
```

## Billing And Payer Credentials

Room invite tokens (`hmq_...`) authorize access to one room, but they are not
tenant billing credentials. When you query with the CLI, it attaches the
active tenant profile's `hmk_` key as the payer automatically:

```bash
hivemind profile use liz
hivemind room ask "$ROOM" "What changed this month?"
```

Use an explicit payer only when a different tenant should pay:

```bash
hivemind room ask "$ROOM" --payer-profile liz-billing "What changed this month?"
HIVEMIND_PAYER_API_KEY=hmk_... hivemind room ask "$ROOM" "What changed this month?"
```

The payer key is sent as `X-Hivemind-Payer-Key`; it does not change the room
authorization. Raw API clients using `hmq_` invite tokens must send the same
header so the service knows which tenant to charge. Admin billing commands:

```bash
hivemind admin billing grant t_... 25.00 --note "initial credit"
hivemind admin billing balance t_...
hivemind admin billing prices
```

## Visibility Choices

| Choice | Use When | Effect |
| --- | --- | --- |
| `--query-visibility inspectable` | the query code and run prompts should be auditable | query source is readable when stored that way; run prompts are stored in history |
| `--query-visibility sealed` | query source or prompts are sensitive | source is sealed; prompt plaintext is not stored, only `prompt_hash` |
| `--output-visibility querier_only` | participant's conclusions should stay private from the owner | owner sees audit metadata, not participant output/artifacts |
| `--output-visibility owner_and_querier` | the result is a shared report | owner and participant can read output/artifacts |

## Trust Choices

| Mode | Use When | Trusts |
| --- | --- | --- |
| `operator_updates` | normal hosted service flow | operator governance for approved CVM updates |
| `owner_approved` | room owner should control allowed CVM measurements | owner-maintained per-room compose allowlist |
| `pinned` | no upgrades should be accepted without recreating or updating the room | exact compose hashes in the room manifest |

`owner_approved` and `pinned` pin the live compose hash at room creation when
the service can report it.

## Egress Choices

Default room creation allows Tinfoil LLM egress. To allow OpenRouter instead:

```bash
hivemind room create <scope-agent-id-or-path> \
  --query-agent <query-agent-id-or-path> \
  --mediator-agent <mediator-agent-id-or-path> \
  --rules-file rules.md \
  --llm-provider openrouter
```

To allow both:

```bash
hivemind room create <scope-agent-id-or-path> \
  --query-agent <query-agent-id-or-path> \
  --mediator-agent <mediator-agent-id-or-path> \
  --rules-file rules.md \
  --llm-provider tinfoil \
  --llm-provider openrouter
```

For non-LLM rooms only, disable bridge LLM egress:

```bash
hivemind room create <scope-agent-id-or-path> \
  --query-agent <query-agent-id-or-path> \
  --rules-file rules.md \
  --no-llm
```

Use `--no-llm` only for rooms whose pinned agents are deterministic and never
call the bridge LLM endpoints. Natural-language scope/query/mediator rooms
should allow at least one LLM provider.

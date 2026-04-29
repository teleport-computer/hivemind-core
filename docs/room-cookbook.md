# Room Cookbook

Use this when you already have a scope agent, optional query agent, and room
rules, and need to choose the smallest correct command.

## Rules Files

`--rules-file` reads plain text. Markdown is the recommended convention because
room rules are human policy, not machine configuration.

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
questions. This is the default shape for repeatable rooms.

```bash
hivemind room create <scope-agent-id-or-path> \
  --query-agent <query-agent-id-or-path> \
  --rules-file rules.md
```

When `<query-agent-id-or-path>` is an existing registered agent id, the room
uses that agent's registered inspection mode. When it is a local path, add
`--query-visibility sealed` or `--query-visibility inspectable` to choose how
the uploaded source is stored.

## Watch History Hashtag Room

For the current live watch-history tenant:

```bash
hivemind --profile watch-history room create fae0070e6f1f \
  --name watch-history-hashtags \
  --query-agent 730f2d35c608 \
  --scope-visibility inspectable \
  --query-visibility inspectable \
  --rules-file rules.md \
  --trust-mode owner_approved \
  --llm-provider tinfoil \
  --llm-provider openrouter
```

That command creates a fixed-query room using the existing inspectable scope
and query agents, permits either Tinfoil or OpenRouter LLM egress, and prints
the `hmroom://...` invite link to share. Because query visibility is
`inspectable`, prompts for room runs are stored in run history.

Ask through the invite:

```bash
hivemind -y room ask 'hmroom://...' \
  --timeout 900 \
  --max-tokens 300000 \
  --max-llm-calls 50 \
  --provider openrouter \
  --model moonshotai/kimi-k2.6 \
  "Show me my top 30 hashtags by watch count as a markdown table with columns: rank, hashtag, watches. Just the table, no explanation."
```

Use Tinfoil instead when upstream inference should be TEE-attested:

```bash
hivemind -y room ask 'hmroom://...' \
  --timeout 900 \
  --max-tokens 300000 \
  --max-llm-calls 50 \
  --provider tinfoil \
  --model kimi-k2-6 \
  "Show me my top 30 hashtags by watch count as a markdown table with columns: rank, hashtag, watches. Just the table, no explanation."
```

## Participant Uploads Query Agent

Use this when the participant should bring their own query logic.

```bash
hivemind room create <scope-agent-id-or-path> \
  --rules-file rules.md \
  --query-visibility sealed
```

Then the participant asks with:

```bash
hivemind room ask 'hmroom://...' "What changed this month?" \
  --agent ./participant-query-agent
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
  --rules-file rules.md \
  --llm-provider openrouter
```

To allow both:

```bash
hivemind room create <scope-agent-id-or-path> \
  --query-agent <query-agent-id-or-path> \
  --rules-file rules.md \
  --llm-provider tinfoil \
  --llm-provider openrouter
```

To disable external LLM calls:

```bash
hivemind room create <scope-agent-id-or-path> \
  --rules-file rules.md \
  --no-llm
```

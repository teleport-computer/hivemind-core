# Examples

## Create An Uploadable Room

```bash
hmctl init --service http://localhost:8100 --api-key hmk_owner

hmctl room create ./agents/my-scope \
  --rules-file rules.md \
  --query-visibility sealed
```

The output includes:

```text
Room: room_...
Invite: hmroom://...
```

## Add Room Data

```bash
hmctl room add-data room_... --file company_docs.md --meta source=docs
hmctl room add-data room_... "private note" --meta source=note
hmctl room data room_...
```

## Participant Inspection And Ask

```bash
hmctl room inspect 'hmroom://...'
hmctl room inspect 'hmroom://...' --json | jq '.room.manifest'
hmctl room ask 'hmroom://...' "What changed this month?"
```

`room ask` defaults to `--timeout 900`, `--max-llm-calls 60`,
`--max-tokens 1000000`, and `--memory-mb 256`. Use explicit smaller budgets
for deterministic agents when you want a tighter cost/latency envelope.

With a participant-owned query agent:

```bash
hmctl room ask 'hmroom://...' \
  "What changed this month?" \
  --agent ./participant-query-agent
```

## Fixed Query Agent Room

```bash
hmctl room create ./agents/my-scope \
  --query-agent ./agents/fixed-query \
  --query-visibility inspectable \
  --rules-file rules.md
```

Participants can ask questions but cannot upload replacement query code.

If the scope and query agents are already registered, the command is shorter:

```bash
hmctl room create scope_agent_id \
  --query-agent query_agent_id \
  --rules-file rules.md
```

For the current live watch-history tenant:

```bash
hmctl --profile watch-history room create agents/default-scope-hermes \
  --name watch-history-hashtags \
  --query-agent agents/default-query-hermes \
  --mediator-agent agents/default-mediator-hermes \
  --scope-visibility inspectable \
  --query-visibility inspectable \
  --rules-file rules.md \
  --trust-mode owner_approved \
  --llm-provider openrouter
```

## Non-LLM Room Egress Deny

```bash
hmctl room create ./agents/my-scope \
  --rules-file rules.md \
  --no-llm
```

The sandbox bridge rejects all LLM calls. The final room output is still allowed.
Use this only for pinned agents that do not call LLM endpoints.

## Owner-Approved Deployment Updates

```bash
hmctl room trust room_... --mode owner_approved --approve-live
```

Existing invite links keep working because recipients verify the updated room
manifest against the owner public key embedded in the original link.

## Direct HTTP

Create a room:

```bash
curl -X POST "$BASE/v1/rooms" \
  -H "Authorization: Bearer $OWNER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "demo",
    "rules": "Only answer aggregate questions.",
    "scope_agent_id": "scope123",
    "query_mode": "uploadable",
    "query_visibility": "sealed",
    "output_visibility": "querier_only",
    "egress": {"llm_providers": ["openrouter"], "allow_artifacts": true},
    "trust": {"mode": "operator_updates"}
  }'
```

Add data:

```bash
curl -X POST "$BASE/v1/rooms/$ROOM_ID/data" \
  -H "Authorization: Bearer $OWNER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "private document", "metadata": {"source": "demo"}}'
```

Run:

```bash
curl -X POST "$BASE/v1/rooms/$ROOM_ID/runs" \
  -H "Authorization: Bearer $INVITE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "Summarize the private document."}'
```

Poll:

```bash
curl "$BASE/v1/runs/$RUN_ID" -H "Authorization: Bearer $INVITE_TOKEN"
```

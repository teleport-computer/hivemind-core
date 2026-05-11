# Integration Test Checklist

The public integration surface is room-first.

## Smoke

- `GET /v1/healthz` returns `200`.
- `GET /v1/attestation` returns a bundle or a clear not-ready reason.
- `hmctl init --service ... --api-key ...` saves a profile.
- `hmctl trust attest` verifies or fails with an actionable reason.

## Room Agents

- `POST /v1/room-agents` uploads a scope agent archive.
- `GET /v1/room-agents` lists the new agent.
- `GET /v1/room-agents/{id}/attest` returns config, source digests, image digest,
  and live CVM attestation.
- Sealed agents list file paths but do not serve plaintext file bodies.

## Room Lifecycle

- `POST /v1/rooms` creates a signed manifest and `hmroom://` link.
- `GET /v1/rooms/{id}/attest` returns a room envelope that verifies against the
  owner public key from the invite link.
- `POST /v1/rooms/{id}/data` stores encrypted room data.
- `GET /v1/rooms/{id}/data` lists owner-visible room data after the owner opens
  the room key.
- `POST /v1/rooms/{id}/trust` re-signs the room and keeps the same invite link
  valid.

## Runs

- `POST /v1/rooms/{id}/runs` starts a fixed-query room run.
- `POST /v1/rooms/{id}/query-agents` uploads and runs a participant query agent
  when the room is uploadable.
- `GET /v1/runs/{run_id}` eventually reaches `completed` or `failed`.
- Completed runs include a signed attestation body with the room id and room
  manifest hash.
- `querier_only` hides output and artifacts from the owner for participant runs.
- `llm_providers=[]` makes bridge LLM calls fail closed.
- `allow_artifacts=false` hides artifact upload egress.

## CLI

- `hmctl room create ./scope-agent --rules-file rules.md --allowed-table data`
  uploads a local scope agent and prints an invite link.
- `hmctl room inspect 'hmroom://...'` verifies the room envelope.
- `hmctl room add-data <room> --file data.md` writes room data.
- `hmctl room ask 'hmroom://...' "question"` verifies the run attestation.
- `hmctl room ask 'hmroom://...' "question" --agent ./query-agent` uses the
  room query-agent upload path.

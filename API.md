# API

The public API is centered on signed rooms: attested recall agreements between
an owner and a participant.

Use an owner key (`hmk_...`) to create rooms, upload room agents, add room data,
and update room trust. Use an invite token from an `hmroom://` link to inspect,
open, and run inside that room.

## Signup And Billing

### `POST /v1/signup`

Disabled unless the operator sets `HIVEMIND_SELF_SERVE_SIGNUP_ENABLED=true`.
Creates a tenant owner key with `$0.00` starting balance. Signup does not use
credit codes; users redeem credit codes separately after signup.

```json
{
  "name": "alice"
}
```

Response includes the plaintext `hmk_...` key once:

```json
{
  "tenant_id": "t_...",
  "name": "alice",
  "api_key": "hmk_...",
  "starter_credit_micro_usd": 0,
  "balance_micro_usd": 0
}
```

### `GET /v1/billing`

Owner-only. Returns the authenticated tenant's current balance and recent
ledger entries.

### `POST /v1/billing/credit-codes/redeem`

Owner-only. Redeems an admin-minted credit code into an existing tenant.
Credit codes are not signup codes and are never required to create the tenant.

```json
{
  "credit_code": "hmcc_..."
}
```

### Admin Billing

Admin billing routes require `Authorization: Bearer <HIVEMIND_ADMIN_KEY>`.

- `POST /v1/admin/credit-codes`: create a tracked credit code and return the
  plaintext code once.
- `GET /v1/admin/credit-codes`: list credit-code status without plaintext
  codes.
- `POST /v1/admin/credit-codes/{code_id}/revoke`: revoke an unused or active
  credit code.
- `GET /v1/admin/billing`: show every tenant's current balance, total credited,
  and total spent.
- `GET /v1/admin/billing/ledger`: show recent ledger entries across tenants.
- `GET /v1/admin/billing/{tenant_id}`: show one tenant's balance and ledger.
- `POST /v1/admin/billing/{tenant_id}/credits`: grant or debit tenant credit.
- `GET /v1/admin/billing/prices`, `POST /v1/admin/billing/prices`: inspect and
  configure provider/model prices.

Credit enforcement uses the existing `HIVEMIND_BILLING_ENFORCE_CREDITS=true`
switch, so operators should configure model prices before enabling it.

## Admin Tenants

Admin routes require `Authorization: Bearer <HIVEMIND_ADMIN_KEY>`.

- `POST /v1/admin/tenants`: create a tenant and return a one-time `hmk_...`.
- `GET /v1/admin/tenants`: list tenants.
- `DELETE /v1/admin/tenants/{tenant_id}`: delete a tenant.
- `POST /v1/admin/tenants/{tenant_id}/reset-key`: reset a tenant key, with
  optional seal clearing and capability revocation.
- `POST /v1/admin/tenants/register`: adopt an existing tenant database.
- `POST /v1/admin/rename-database`: rename a tenant database through the SQL
  proxy admin channel.
- `POST /v1/admin/migrate-to-roles`: run the SQL-proxy role migration.
- `POST /v1/admin/agents/sweep-broken`: dry-run or delete agent registrations
  whose Docker images are missing after a redeploy.
- `GET /v1/admin/llm-probe`: probe configured LLM connectivity from the CVM.

## System And Tenant Identity

### `GET /v1/healthz`

Unauthenticated liveness probe. Returns `{"ok": true}` when the FastAPI
process is serving requests.

### `GET /v1/health`

Authenticated service check. Returns service version and table count for a
valid owner/admin key. The CLI `doctor` command uses this for profile and
version checks.

### `POST /v1/tenant/rotate-key`

Owner-only. Rotates the current tenant's `hmk_...` API key and returns the
plaintext replacement once.

### `POST /v1/tenant/sql`

Owner-only. Run a SQL statement against the tenant database. SELECTs return
rows; INSERT/UPDATE/DELETE/DDL are committed and return `rowcount`. Internal
tables (`_hivemind_*`, `_billing_*`, `_credit_*`, `_tenants`) and the
introspection schemas (`information_schema`, `pg_catalog`) are blocked at the
SQL parser. Used by the website's database browser and the bootstrap scripts
to seed tables before binding them to a room via `allowed_tables`.

```json
{ "sql": "CREATE TABLE notes (id SERIAL, body TEXT)", "params": [] }
```

```json
{ "rows": [], "rowcount": 0 }
```

### Tenant Capability Tokens

Owner-only. List and revoke `hmq_...` capability tokens. Tokens are minted
implicitly when the owner creates a room — `POST /v1/rooms` returns the
plaintext token once, and the same lifecycle endpoints below let the owner
audit and revoke individual invites without tearing down the whole room.

- `GET /v1/tenant/tokens` — list non-revoked tokens for the calling tenant.
  Returns `{ "tokens": [{ "token_id", "kind", "label", "constraints",
  "created_at", "revoked_at" }, ...] }`. No plaintext is ever returned.
- `DELETE /v1/tenant/tokens/{token_id}` — revoke a single token by its
  `token_id` prefix. Returns 404 if no matching live token exists.

### `GET /v1/whoami`

Returns the authenticated caller role and tenant/capability identity.

### `GET /v1/admin/schema`

Owner/query caller schema introspection for the active tenant database.

### Compose Pins

Owner compose-hash allowlist APIs used by trust tooling:

- `POST /v1/tenants/compose-pin`
- `GET /v1/tenants/compose-pin`
- `GET /v1/tenants/compose-pin/list`
- `DELETE /v1/tenants/compose-pin/{pin_id}`

## Rooms

### `POST /v1/rooms`

Create a signed room manifest and invite token.

```json
{
  "name": "diligence",
  "rules": "Only answer aggregate questions.",
  "policy": "Optional scope-agent policy text.",
  "scope_agent_id": "abc123",
  "query_mode": "uploadable",
  "query_agent_id": null,
  "query_visibility": "sealed",
  "output_visibility": "querier_only",
  "egress": {
    "llm_providers": ["openrouter"],
    "allow_artifacts": true
  },
  "trust": {
    "mode": "operator_updates",
    "allowed_composes": []
  }
}
```

Response includes:

```json
{
  "room_id": "room_...",
  "room": {"manifest": {}, "manifest_hash": "..."},
  "token": "hmq_...",
  "token_id": "...",
  "link": "hmroom://..."
}
```

### `GET /v1/rooms`

Owner-only. Lists rooms for the authenticated tenant.

### `GET /v1/rooms/{room_id}`

Returns owner-visible room metadata.

### `GET /v1/rooms/{room_id}/attest`

Returns the signed room envelope, scope-agent attestation, fixed query-agent
attestation when present, and the live CVM attestation bundle. Clients should
verify the room envelope against the owner public key embedded in the invite
link before presenting private data or agent code.

### `POST /v1/rooms/{room_id}/open`

Presents the current bearer and opens the room key in process memory. This is
also performed by room data writes and room runs when needed.

### `GET /v1/rooms/{room_id}/key`

Invite-token route used by clients to receive the wrapped room key after
verification.

### `POST /v1/rooms/{room_id}/data`

Owner-only. Adds encrypted room data.

```json
{
  "text": "private document text",
  "metadata": {"source": "dataset"}
}
```

### `GET /v1/rooms/{room_id}/data`

Owner-only. Lists owner-visible room data after opening the room key.

### `DELETE /v1/rooms/{room_id}`

Owner-only. Deletes a room registration.

### `POST /v1/rooms/{room_id}/runs`

Run the room's fixed query agent or a previously uploaded query agent allowed by
the manifest. If the room query visibility is `inspectable`, the plaintext
prompt is stored with the run history. If query visibility is `sealed`, only
the signed run attestation's prompt hash is retained.

```json
{
  "query": "What changed this month?",
  "query_agent_id": "optional-for-uploadable-rooms",
  "model": "optional",
  "provider": "openrouter"
}
```

Response:

```json
{
  "run_id": "...",
  "query_agent_id": "...",
  "scope_agent_id": "...",
  "room_id": "room_...",
  "status": "pending"
}
```

Poll `GET /v1/runs/{run_id}`.

### `POST /v1/rooms/{room_id}/query-agents`

Upload and run a participant query agent in an uploadable room. Multipart form:

- `archive`: `.tar.gz` containing the Dockerfile and agent source.
- `name`
- `prompt`
- optional `model`, `provider`, `memory_mb`, `max_llm_calls`, `max_tokens`,
  `timeout_seconds`

The server applies the room query-agent visibility, egress allowlist, policy,
output visibility, and run attestation binding.

### `POST /v1/rooms/{room_id}/trust`

Owner-only. Re-signs the same room with an updated deployment trust policy.

```json
{
  "mode": "owner_approved",
  "allowed_composes": ["abc..."],
  "append_live": true
}
```

## Room Agents

### `POST /v1/room-agents`

Owner-only. Upload a reusable scope, query, or mediator agent.

Multipart form:

- `archive`
- `name`
- `agent_type`: `scope`, `query`, or `mediator`
- `inspection_mode`: `full` or `sealed`
- `private_paths`: JSON list of archive paths excluded from public source digest

`sealed` agents cannot be read through the files API. Their source remains
available to internal rebuild and digest paths inside the CVM.

### `GET /v1/room-agents`

List room agents visible to the caller.

### `GET /v1/room-agents/{agent_id}`

Return one room agent's metadata.

### `GET /v1/room-agents/{agent_id}/attest`

Returns agent config, source digests, image digest, inspection mode, and live CVM
attestation.

### `GET /v1/room-agents/{agent_id}/files`

List extracted source file paths and sizes. Sealed agents list paths but do not
serve plaintext file bodies.

### `GET /v1/room-agents/{agent_id}/files/{path}`

Fetch one inspectable source file. Sealed agents reject plaintext file reads.

### `DELETE /v1/room-agents/{agent_id}`

Owner-only. Deletes a reusable room agent registration.

## Runs

### `GET /v1/runs/{run_id}`

Returns run status, output when visible to the caller, artifacts when enabled,
and the CVM-signed run attestation envelope.

The signed body includes the room id, room manifest hash, output visibility,
allowed LLM providers, artifact setting, and output hash.

### `GET /v1/runs`

List recent runs visible to the caller.

### `GET /v1/runs/{run_id}/artifacts/{filename}`

Fetch a visible artifact for a run. Rooms allow artifact egress by default;
owners can disable it at room creation with `allow_artifacts=false` /
`--no-artifacts`.

## Attestation

### `GET /v1/attestation`

Public dstack attestation bundle. Clients use this to verify the live CVM before
presenting room data, invite tokens, or agent code.

# Changelog

All notable user-facing changes should be recorded here.

This project does not yet promise stable semver compatibility for every API;
the public room APIs and the `hmctl` CLI are the intended compatibility
surface.

## Unreleased

- **Breaking — internal endpoints retired or renamed.** Five `/v1/_internal/*`
  routes that were never part of the supported surface were deleted
  (`scope-attest`, `query/run/submit`, `agents/register-image`, `agents/submit`,
  `index`). The remaining two were promoted to documented public routes,
  with one POST verb dropped:
  - `/v1/_internal/store` → `POST /v1/tenant/sql`
  - `/v1/_internal/tokens` GET/DELETE → `GET /v1/tenant/tokens` and
    `DELETE /v1/tenant/tokens/{token_id}`. The `POST /v1/_internal/tokens`
    mint endpoint was deleted — `POST /v1/rooms` is the supported way to
    issue a capability token, and it returns the plaintext token once. The
    `scripts/smoke_capability_tokens.py` lifecycle script was removed with
    it.
- **Breaking — index agent surface removed.** The old standalone index
  request/response path, query-run index columns, default index agents, index
  image builds, and `agent_type=index` upload path are gone. Room execution is
  scope → query → mediator.
- **Breaking — unrestricted room mode removed.** New rooms always sign an
  explicit `allowed_tables` list, `hmctl room create` sends that list (empty by
  default), and room runs reject old manifests that omit it instead of falling
  back to unrestricted SQL access. Operational room endpoints also reject
  obsolete manifests, and `hmctl room prune --legacy-only` bulk-revokes any
  active legacy invites without deleting tenant data.
- Fixed room table allowlist enforcement so valid aggregate SQL using CTEs,
  subqueries, or lateral function aliases is checked against the underlying
  base tables instead of rejecting the derived alias name.
- Changed scope-Hermes host verification to generic useful-row smoke checks
  instead of prompt-keyword aggregate fixtures, avoiding benchmark-shaped
  verifier policies while still catching destructive empty transforms.
- Replaced the scope-Hermes `AIAgent` wrapper with a bounded bridge loop so
  scope design has explicit tool-call caps and no iteration-exhaustion summary
  path.
- Hardened query-Hermes runs with generic retries for timeout/progress-log
  responses instead of returning unfinished work as final answers, lower default
  SQL/tool caps for latency, and final-output sanitization for invisible format
  controls that can corrupt numeric tables.
- Moved expensive scope-Hermes simulation/source-inspection tools behind
  explicit opt-in environment flags so ordinary scoped analytical runs stay on
  the fast schema/SQL path.
- Changed the hosted Hermes default model from `z-ai/glm-5` to
  `moonshotai/kimi-k2.6` and removed the scope-Hermes hardcoded synthetic
  summary-row gate that could fail otherwise valid analytical runs.
- Added repository hygiene docs: contributing guide, security policy, changelog,
  and GitHub issue templates.
- Added README badges and a shorter top-level capability summary.

## 0.3.6 - 2026-05-02

- Published the public CLI package as `hmctl` on PyPI.
- Kept `hivemind` as a backwards-compatible CLI alias.
- Added self-serve signup with zero starting credit.
- Added admin-minted credit codes for tenant top-ups.
- Added tenant balance and admin billing commands.
- Added `hmctl doctor` for profile, service, billing, attestation, and room
  checks.
- Added admin tenant key reset and clean-start repair workflow.
- Standardized docs on room-native APIs and current artifact paths.

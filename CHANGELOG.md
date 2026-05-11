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
- **Breaking — dedicated index run stage removed.** The old standalone index
  request/response path and query-run index columns are gone from the supported
  room execution pipeline. Reusable index agent contexts still exist for agent
  registry/build coverage, and uploaded agent metadata still accepts
  `agent_type=index`.
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

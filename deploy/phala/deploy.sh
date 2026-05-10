#!/bin/bash
set -euo pipefail

# deploy.sh — safe Phala CVM redeploy cycle for hivemind-core + hivemind-pg.
#
# Root cause this solves (observed 2026-04-25): `phala deploy --cvm-id`
# REPLACES sealed environment variables with whatever is in the -e file.
# If a key referenced by compose is missing from -e, the container boot
# fails with `ERR_INTERPOLATION services.<svc>.environment.<VAR>:
# required variable <VAR> is missing a value`. The gateway then serves
# the empty-body → curl HTTP 000 symptom we've hit 3 times now.
#
# Guardrails:
#  - pre-check: every `${VAR:?...}` in the compose file MUST exist in -e
#  - post-deploy: always push sealed envs again + restart
#  - health poll with real timeout + serial-logs dump on failure (no
#    silent hang, no "seemed to work but actually didn't")
#
# Usage:
#   deploy/phala/deploy.sh core       # redeploy core only
#   deploy/phala/deploy.sh postgres   # redeploy postgres only
#   deploy/phala/deploy.sh all        # both (default)
#
# Env:
#   IMAGE_TAG        override tag pin baked into compose (optional)
#   HEALTH_TIMEOUT   seconds to wait for healthy (default 600)
#   PHALA_*_TIMEOUT  seconds for bounded Phala CLI operations

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

ENV_FILE="${SCRIPT_DIR}/.env"
# 600s isn't tight: a fresh prod9 deploy has to (a) derive the
# enclave-TLS cert via dstack-KMS, (b) run ACME DNS-01 through Cloudflare
# (CAA + TXT propagation), (c) wait for gateway routes to become live.
# Observed ~5–8 minutes end-to-end on dstack-pha-prod9.
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-600}"
PHALA_DEPLOY_TIMEOUT="${PHALA_DEPLOY_TIMEOUT:-420}"
PHALA_ENV_UPDATE_TIMEOUT="${PHALA_ENV_UPDATE_TIMEOUT:-90}"
PHALA_RESTART_TIMEOUT="${PHALA_RESTART_TIMEOUT:-420}"
PHALA_QUERY_TIMEOUT="${PHALA_QUERY_TIMEOUT:-45}"
PHALA_START_TIMEOUT="${PHALA_START_TIMEOUT:-120}"
PHALA_TIMEOUT_KILL_AFTER="${PHALA_TIMEOUT_KILL_AFTER:-30s}"

# CVM names are env-overridable so a fresh-cluster first-deploy can
# use a temporary name (e.g. `hivemind-core-prod10`) without colliding
# with the in-flight one in the workspace. Subsequent CICD redeploys
# leave these unset → use the canonical name.
CORE_NAME="${CORE_NAME:-hivemind-core}"
CORE_COMPOSE="${SCRIPT_DIR}/docker-compose.core.yaml"
CORE_HEALTH_PATH="/v1/attestation"

PG_NAME="${PG_NAME:-hivemind-pg}"
PG_COMPOSE="${SCRIPT_DIR}/docker-compose.postgres.yaml"
PG_HEALTH_PATH="/health"

# `NODE_ID` switches the deploy_and_seal helper from update-mode
# (`phala deploy --cvm-id <name>`) to create-mode (`phala deploy -n
# <name> --node-id <id>`). Use it ONLY for the first-time prod9
# migration: setting it on a name that already exists in the workspace
# returns "name already taken" and aborts. Prod9 node-id is 18, prod5 26
# (run `phala nodes list --json` to confirm).
NODE_ID="${NODE_ID:-}"

# ── Helpers ──

log()  { printf "\033[0;36m[deploy]\033[0m %s\n" "$*"; }
warn() { printf "\033[0;33m[deploy]\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[0;31m[deploy ERROR]\033[0m %s\n" "$*" >&2; exit 1; }

phala_timeout() {
    local seconds="$1"
    shift
    timeout --foreground --kill-after="${PHALA_TIMEOUT_KILL_AFTER}" "${seconds}" "$@"
}

retry_env_update() {
    local name="$1"
    local env_file="$2"
    local output=""
    local status=0

    for i in $(seq 1 18); do
        if output=$(phala_timeout "${PHALA_ENV_UPDATE_TIMEOUT}" \
                phala envs update --cvm-id "${name}" -e "${env_file}" 2>&1); then
            [ -n "${output}" ] && printf "%s\n" "${output}"
            return 0
        fi
        status=$?
        if [ "${status}" -eq 124 ]; then
            warn "env re-seal command timed out after ${PHALA_ENV_UPDATE_TIMEOUT}s; retry ${i}/18 in 20s"
            sleep 20
            continue
        fi
        if printf "%s\n" "${output}" | grep -qi "Another operation is already in progress"; then
            warn "env re-seal blocked by another CVM operation; retry ${i}/18 in 20s"
            sleep 20
            continue
        fi
        printf "%s\n" "${output}" >&2
        return 1
    done

    printf "%s\n" "${output}" >&2
    die "env re-seal on ${name} did not complete after retries"
}

# Extract every `${VAR:?...}` reference from a compose file — these
# are the hard-required envs. Default-fallback forms `${VAR:-...}`
# are intentionally excluded.
required_vars() {
    local compose="$1"
    grep -Eho '\$\{[A-Z_][A-Z0-9_]*:\?' "${compose}" \
        | sed -E 's/^\$\{([A-Z_][A-Z0-9_]*):\?.*/\1/' \
        | sort -u
}

# Variables present in the env file (supports KEY=value + export KEY=value).
env_vars() {
    local env_file="$1"
    grep -E '^[[:space:]]*(export[[:space:]]+)?[A-Z_][A-Z0-9_]*=' "${env_file}" \
        | sed -E 's/^[[:space:]]*(export[[:space:]]+)?([A-Z_][A-Z0-9_]*)=.*/\2/' \
        | sort -u
}

# Bail out BEFORE touching the CVM if the env file is missing any
# hard-required compose var. This is what catches the "silently
# dropped admin key" failure mode.
precheck_env() {
    local compose="$1"
    local env_file="$2"

    [ -f "${compose}" ]  || die "compose file not found: ${compose}"
    [ -f "${env_file}" ] || die "env file not found: ${env_file}"

    local missing
    missing=$(comm -23 <(required_vars "${compose}") <(env_vars "${env_file}"))
    if [ -n "${missing}" ]; then
        warn "env file is MISSING these hard-required vars:"
        printf "  %s\n" ${missing} >&2
        die "fix ${env_file} before redeploying (aborted before any CVM changes)"
    fi
    log "pre-check OK: ${env_file} satisfies every \${VAR:?...} in ${compose}"
}

# phala deploy (creates/updates), then explicit envs update, then restart.
# The envs-update step is what makes this robust: `phala deploy --cvm-id`
# drops any var not in -e; we re-seal the complete set afterwards to
# make double-sure nothing was lost in translation.
#
# Two modes:
#   - update (default): NODE_ID empty → `phala deploy --cvm-id <name>`
#     touches an existing CVM in place. Cannot migrate clusters.
#   - create:           NODE_ID set    → `phala deploy -n <name>
#     --node-id <id>` provisions a brand new CVM on the chosen node.
#     Used once when migrating prod5 → prod9 (the gateway routing pattern
#     dstack-ingress relies on only works on prod9). After the new CVM is
#     up, leave NODE_ID unset for subsequent redeploys so they update
#     in place via --cvm-id. The post-deploy seal+restart still uses
#     --cvm-id <name> in both modes (same name resolves to the new CVM
#     because the create just registered it).
deploy_and_seal() {
    local name="$1"
    local compose="$2"
    local env_file="$3"

    # `phala deploy --wait` polls for status=running and exits non-zero
    # at its hardcoded 300s timeout. On prod9 a fresh deploy routinely
    # takes 6–9 min (enclave-TLS cert derivation + ACME DNS-01 + gateway
    # route propagation), so the CLI times out even when the deploy is
    # progressing fine. We treat the CLI exit non-zero as advisory and
    # rely on `wait_healthy` (HEALTH_TIMEOUT=600) as the real
    # correctness check — same pattern we already apply to `phala cvms
    # restart` below. Without this, GH Actions exits before the
    # on-chain approval step can run, even on a successful deploy.
    if [ -n "${NODE_ID}" ]; then
        log "creating ${name} on node-id=${NODE_ID} (compose=${compose})"
        if ! phala_timeout "${PHALA_DEPLOY_TIMEOUT}" \
                phala deploy -n "${name}" --node-id "${NODE_ID}" \
                -c "${compose}" -e "${env_file}" --wait; then
            warn "phala deploy --wait returned non-zero or timed out after ${PHALA_DEPLOY_TIMEOUT}s; continuing — wait_healthy is the real correctness check"
        fi
        # In create-mode `phala deploy` already seals every variable from
        # the -e file into the new CVM's encrypted env channel — running
        # `phala envs update` immediately afterward fails with "Another
        # operation is already in progress" because the CVM is still
        # provisioning. Skip the re-seal + restart entirely; they only
        # exist to compensate for a known bug in the in-place update path
        # where `--cvm-id` drops vars not listed in -e.
        log "create-mode: env vars already sealed by phala deploy (skipping re-seal)"
        return 0
    fi

    log "updating ${name} in place (compose=${compose})"
    if ! phala_timeout "${PHALA_DEPLOY_TIMEOUT}" \
            phala deploy --cvm-id "${name}" -c "${compose}" -e "${env_file}" --wait; then
        warn "phala deploy --wait returned non-zero or timed out after ${PHALA_DEPLOY_TIMEOUT}s; continuing — wait_healthy is the real correctness check"
        local s
        s=$(phala_timeout "${PHALA_QUERY_TIMEOUT}" \
            phala cvms get --cvm-id "${name}" --json 2>/dev/null \
            | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status",""))') || s=""
        log "post-deploy status: ${s}"
        if [ "${s}" = "stopped" ]; then
            warn "CVM left in stopped state — issuing explicit start"
            phala_timeout "${PHALA_START_TIMEOUT}" \
                phala cvms start --cvm-id "${name}" >/dev/null 2>&1 \
                || warn "phala cvms start also returned non-zero; wait_healthy will verify"
        fi
    fi

    log "re-sealing env vars on ${name}"
    retry_env_update "${name}" "${env_file}"

    # `phala cvms restart` polls for status=running and exits non-zero
    # at its hardcoded 300s timeout. On prod9 with enclave-TLS + ACME
    # DNS-01, a stop/start cycle routinely takes longer than that
    # (observed 6+ min on run 24931388444). Worse, when restart times
    # out it sometimes leaves the CVM in `stopped` state — so we have
    # to (a) treat the CLI timeout as advisory and (b) explicitly call
    # `phala cvms start` if the CVM didn't actually come back up. The
    # post-deploy `wait_healthy` poll is the real correctness check.
    # Without this guard the workflow exited before the on-chain
    # approval step could run, even though the deploy itself succeeded.
    log "restarting ${name} to pick up re-sealed envs"
    if ! phala_timeout "${PHALA_RESTART_TIMEOUT}" \
            phala cvms restart --cvm-id "${name}" >/dev/null 2>&1; then
        warn "phala cvms restart returned non-zero or timed out after ${PHALA_RESTART_TIMEOUT}s; checking state"
        local s
        s=$(phala_timeout "${PHALA_QUERY_TIMEOUT}" \
            phala cvms get --cvm-id "${name}" --json 2>/dev/null \
            | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status",""))') || s=""
        log "post-restart status: ${s}"
        if [ "${s}" = "stopped" ]; then
            warn "CVM left in stopped state — issuing explicit start"
            phala_timeout "${PHALA_START_TIMEOUT}" \
                phala cvms start --cvm-id "${name}" >/dev/null 2>&1 \
                || warn "phala cvms start also returned non-zero; wait_healthy will verify"
        fi
    fi
}

# Resolve service URL (looks up CVM app_id + builds the gateway URL).
# If `tls_passthrough=1`, use the `-<port>s.` suffix — Phala's gateway
# convention for TCP-passthrough routes, required when the container
# terminates TLS itself (HIVEMIND_ENCLAVE_TLS=1).
#
# Gateway hostname is derived from the CVM's own `gateway.base_domain`
# (returned by `phala cvms get --json`) rather than hardcoded — this
# is what lets the script handle a mixed-cluster workspace correctly
# (e.g. hivemind-pg on prod5, hivemind-core on prod9 during migration).
service_url() {
    local name="$1"
    local port="$2"
    local tls_passthrough="${3:-0}"
    local meta app_id base_domain
    meta=$(phala cvms get --cvm-id "${name}" --json 2>/dev/null) \
        || die "could not fetch CVM metadata for ${name}"
    app_id=$(printf '%s' "${meta}" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin).get("app_id",""))')
    base_domain=$(printf '%s' "${meta}" \
        | python3 -c 'import sys,json; print((json.load(sys.stdin).get("gateway") or {}).get("base_domain",""))')
    [ -n "${app_id}" ]     || die "could not resolve app_id for ${name}"
    [ -n "${base_domain}" ] || die "could not resolve gateway base_domain for ${name}"
    local suffix=""
    [ "${tls_passthrough}" = "1" ] && suffix="s"
    echo "https://${app_id}-${port}${suffix}.${base_domain}"
}

# Sync HIVEMIND_PINNING_GATEWAY in the env file from the CVM's actual
# gateway.base_domain. Closes the drift loop where a stale env override
# (or a fresh relay clone with a stale .env) causes hivemind-core's
# attestation bundle to advertise a `tls.pinning_url` that points at
# the wrong cluster — symptoms: CLI fails Tier-3 cert pin verification,
# `hmctl trust attest` errors with "Cannot reach <friendly>" on SSL EOF
# while the friendly URL is actually fine. (Real incident, 2026-04-26.)
#
# Two modes:
#  - update (CVM exists): authoritative truth = the running CVM's
#    `gateway.base_domain`. We rewrite the env file to match. Idempotent.
#  - create (NODE_ID set): no CVM yet to query. We trust whatever the
#    operator put in env (or the compose default). After the create
#    completes, the next deploy will auto-correct via update mode.
sync_pinning_gateway() {
    local name="$1"
    local env_file="$2"

    if [ -n "${NODE_ID}" ]; then
        log "sync_pinning_gateway: create-mode (NODE_ID=${NODE_ID}) — skipping (no CVM to query yet)"
        return 0
    fi

    local meta base_domain
    meta=$(phala cvms get --cvm-id "${name}" --json 2>/dev/null || true)
    if [ -z "${meta}" ]; then
        log "sync_pinning_gateway: ${name} not found in workspace — skipping (first deploy?)"
        return 0
    fi
    base_domain=$(printf '%s' "${meta}" \
        | python3 -c 'import sys,json; print((json.load(sys.stdin).get("gateway") or {}).get("base_domain",""))')
    if [ -z "${base_domain}" ]; then
        warn "sync_pinning_gateway: ${name} has no gateway.base_domain in metadata — skipping"
        return 0
    fi

    local current
    current=$(grep -E '^HIVEMIND_PINNING_GATEWAY=' "${env_file}" 2>/dev/null | head -1 | sed -E 's|^HIVEMIND_PINNING_GATEWAY=||' || true)

    if [ "${current}" = "${base_domain}" ]; then
        log "sync_pinning_gateway: env already matches CVM gateway (${base_domain})"
        return 0
    fi

    if [ -z "${current}" ]; then
        log "sync_pinning_gateway: appending HIVEMIND_PINNING_GATEWAY=${base_domain} (was unset)"
        echo "HIVEMIND_PINNING_GATEWAY=${base_domain}" >> "${env_file}"
    else
        warn "sync_pinning_gateway: rewriting HIVEMIND_PINNING_GATEWAY (${current} → ${base_domain}) to match live CVM"
        sed -i -E "s|^HIVEMIND_PINNING_GATEWAY=.*|HIVEMIND_PINNING_GATEWAY=${base_domain}|" "${env_file}"
    fi
}

# Keep production env files aligned when the project default model changes.
# We only rewrite values that were previous hivemind defaults. A different
# value is treated as an intentional operator override and left alone.
sync_default_llm_model() {
    local env_file="$1"
    local desired="z-ai/glm-5"
    local current
    current=$(grep -E '^HIVEMIND_LLM_MODEL=' "${env_file}" 2>/dev/null | head -1 | sed -E 's|^HIVEMIND_LLM_MODEL=||' || true)

    case "${current}" in
        "")
            log "sync_default_llm_model: appending HIVEMIND_LLM_MODEL=${desired} (was unset)"
            echo "HIVEMIND_LLM_MODEL=${desired}" >> "${env_file}"
            ;;
        "anthropic/claude-sonnet-4.5"|"moonshotai/kimi-k2.6")
            warn "sync_default_llm_model: rewriting old default HIVEMIND_LLM_MODEL (${current} -> ${desired})"
            sed -i -E "s|^HIVEMIND_LLM_MODEL=.*|HIVEMIND_LLM_MODEL=${desired}|" "${env_file}"
            ;;
        "${desired}")
            log "sync_default_llm_model: env already uses ${desired}"
            ;;
        *)
            log "sync_default_llm_model: preserving explicit operator model override (${current})"
            ;;
    esac
}

sync_env_value() {
    local env_file="$1"
    local key="$2"
    local desired="$3"
    local current
    current=$(grep -E "^${key}=" "${env_file}" 2>/dev/null | head -1 | sed -E "s|^${key}=||" || true)

    if [ "${current}" = "${desired}" ]; then
        log "sync_env_value: ${key} already ${desired}"
        return 0
    fi
    if [ -z "${current}" ]; then
        log "sync_env_value: appending ${key}=${desired} (was unset)"
        echo "${key}=${desired}" >> "${env_file}"
        return 0
    fi
    warn "sync_env_value: rewriting ${key} (${current} -> ${desired})"
    sed -i -E "s|^${key}=.*|${key}=${desired}|" "${env_file}"
}

sync_self_serve_billing_policy() {
    local env_file="$1"
    sync_env_value "${env_file}" HIVEMIND_SELF_SERVE_SIGNUP_ENABLED true
    sync_env_value "${env_file}" HIVEMIND_BILLING_ENFORCE_CREDITS true
}

sync_budget_policy() {
    local env_file="$1"
    sync_env_value "${env_file}" HIVEMIND_DEFAULT_QUERY_MAX_TOKENS 1000000
    sync_env_value "${env_file}" HIVEMIND_MAX_TOKENS 100000000
}

sync_hermes_default_agents() {
    local env_file="$1"
    local image_tag="${IMAGE_SHA:-latest}"
    local image_prefix="ghcr.io/teleport-computer"

    sync_env_value "${env_file}" HIVEMIND_AUTOLOAD_DEFAULT_AGENTS true
    sync_env_value "${env_file}" HIVEMIND_BUNDLED_AGENTS_DIR /app/agents
    sync_env_value "${env_file}" HIVEMIND_DEFAULT_INDEX_AGENT \
        default-index-hermes
    sync_env_value "${env_file}" HIVEMIND_DEFAULT_SCOPE_AGENT \
        default-scope-hermes
    sync_env_value "${env_file}" HIVEMIND_DEFAULT_QUERY_AGENT \
        default-query-hermes
    sync_env_value "${env_file}" HIVEMIND_DEFAULT_MEDIATOR_AGENT \
        default-mediator-hermes
    sync_env_value "${env_file}" HIVEMIND_DEFAULT_INDEX_HERMES_IMAGE \
        "${image_prefix}/hivemind-default-index-hermes:${image_tag}"
    sync_env_value "${env_file}" HIVEMIND_DEFAULT_SCOPE_HERMES_IMAGE \
        "${image_prefix}/hivemind-default-scope-hermes:${image_tag}"
    sync_env_value "${env_file}" HIVEMIND_DEFAULT_QUERY_HERMES_IMAGE \
        "${image_prefix}/hivemind-default-query-hermes:${image_tag}"
    sync_env_value "${env_file}" HIVEMIND_DEFAULT_MEDIATOR_HERMES_IMAGE \
        "${image_prefix}/hivemind-default-mediator-hermes:${image_tag}"
}

is_truthy() {
    local value
    value="$(printf "%s" "${1:-}" | tr '[:upper:]' '[:lower:]')"
    case "${value}" in
        1|true|yes|on) return 0 ;;
        [1-9]*) return 0 ;;
        *) return 1 ;;
    esac
}

env_file_has_key() {
    local key="$1"
    local env_file="${2:-${ENV_FILE}}"
    grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "${env_file}" 2>/dev/null
}

env_file_value() {
    local key="$1"
    local env_file="${2:-${ENV_FILE}}"
    python3 - "${env_file}" "${key}" <<'PY'
import re
import shlex
import sys

env_file, key = sys.argv[1], sys.argv[2]
pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=\s*(.*)$")
found = False
value = ""
try:
    with open(env_file, encoding="utf-8") as fh:
        for raw_line in fh:
            if re.match(r"^\s*#", raw_line):
                continue
            match = pattern.match(raw_line.rstrip("\n"))
            if not match:
                continue
            raw_value = match.group(1).strip()
            try:
                parts = shlex.split(raw_value, comments=True, posix=True)
                value = parts[0] if parts else ""
            except ValueError:
                value = raw_value.split("#", 1)[0].strip().strip("'\"")
            found = True
except FileNotFoundError:
    pass
if found:
    print(value)
PY
}

compose_tls_default() {
    local compose="$1"
    python3 - "${compose}" <<'PY'
import re
import shlex
import sys

compose = sys.argv[1]
pattern = re.compile(r"^\s*HIVEMIND_ENCLAVE_TLS\s*:\s*(.*?)\s*(?:#.*)?$")
try:
    with open(compose, encoding="utf-8") as fh:
        for line in fh:
            match = pattern.match(line.rstrip("\n"))
            if not match:
                continue
            raw_value = match.group(1).strip()
            env_default = re.match(
                r"^\$\{HIVEMIND_ENCLAVE_TLS:-([^}]+)\}$",
                raw_value,
            )
            if env_default:
                raw_value = env_default.group(1)
            try:
                parts = shlex.split(raw_value, comments=False, posix=True)
                value = parts[0] if parts else ""
            except ValueError:
                value = raw_value.strip("'\"")
            print(value.strip())
            break
except FileNotFoundError:
    pass
PY
}

# Does this core compose have enclave TLS enabled (default or override)?
core_tls_enabled() {
    local compose="$1"
    local value
    # Explicit env-file value wins over the compose fallback. This matters
    # because `HIVEMIND_ENCLAVE_TLS=0` is a deliberate prod9 setting, not a
    # truthy non-empty string.
    if env_file_has_key HIVEMIND_ENCLAVE_TLS "${ENV_FILE}"; then
        value="$(env_file_value HIVEMIND_ENCLAVE_TLS "${ENV_FILE}")"
        is_truthy "${value}" && return 0
        return 1
    fi
    value="$(compose_tls_default "${compose}")"
    is_truthy "${value}" && return 0
    return 1
}

# Poll URL until HTTP 200 or timeout. On timeout, dump serial logs so
# the operator can read the exact boot failure (usually the compose
# interpolation error we're guarding against).
wait_healthy() {
    local name="$1"
    local url="$2"
    local timeout="${3:-${HEALTH_TIMEOUT}}"

    log "waiting for ${name} → ${url} (timeout ${timeout}s)"
    local started=$(date +%s)
    local code
    while : ; do
        # -k keeps this health probe transport-agnostic: it tolerates either
        # gateway TLS on -8100 or a self-signed enclave cert on a passthrough
        # route. CLI trust, DCAP, and on-chain approval are the security gate.
        code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 8 "${url}" 2>/dev/null || echo "000")
        if [ "${code}" = "200" ]; then
            local elapsed=$(( $(date +%s) - started ))
            log "${name} healthy after ${elapsed}s"
            return 0
        fi
        if [ $(( $(date +%s) - started )) -ge "${timeout}" ]; then
            warn "${name} not healthy after ${timeout}s (last code: ${code})"
            warn "── serial logs (last 40 lines) ──"
            # `phala cvms serial-logs` has been observed to hang for many
            # minutes when the gateway is misbehaving — wrap it in a hard
            # kill so the failure path doesn't stretch the SSH session
            # past GH Actions' idle timeout (caught us once on 24919620715).
            timeout 30 phala cvms serial-logs --cvm-id "${name}" 2>/dev/null \
                | tail -40 >&2 || warn "(serial-logs timed out after 30s)"
            die "${name} failed to become healthy — see serial logs above"
        fi
        sleep 5
    done
}

# ── Per-service entry points ──

deploy_core() {
    sync_pinning_gateway "${CORE_NAME}" "${ENV_FILE}"
    sync_default_llm_model "${ENV_FILE}"
    sync_self_serve_billing_policy "${ENV_FILE}"
    sync_budget_policy "${ENV_FILE}"
    sync_hermes_default_agents "${ENV_FILE}"
    precheck_env  "${CORE_COMPOSE}" "${ENV_FILE}"
    deploy_and_seal "${CORE_NAME}"  "${CORE_COMPOSE}" "${ENV_FILE}"
    local url tls=0
    if core_tls_enabled "${CORE_COMPOSE}"; then
        tls=1
        log "HIVEMIND_ENCLAVE_TLS enabled — health will poll -8100s. (TCP passthrough)"
    fi
    url=$(service_url "${CORE_NAME}" 8100 "${tls}")
    wait_healthy "${CORE_NAME}" "${url}${CORE_HEALTH_PATH}"
}

deploy_postgres() {
    precheck_env  "${PG_COMPOSE}" "${ENV_FILE}"
    deploy_and_seal "${PG_NAME}"  "${PG_COMPOSE}" "${ENV_FILE}"
    local url
    url=$(service_url "${PG_NAME}" 8080)
    wait_healthy "${PG_NAME}" "${url}${PG_HEALTH_PATH}"
}

# ── CLI ──

TARGETS=("${@:-all}")
[ "${#TARGETS[@]}" -eq 0 ] && TARGETS=(all)
[ "${TARGETS[0]}" = "all" ] && TARGETS=(postgres core)

log "targets: ${TARGETS[*]}"
log "env file: ${ENV_FILE}"

for t in "${TARGETS[@]}"; do
    case "${t}" in
        core)     deploy_core ;;
        postgres) deploy_postgres ;;
        *) die "unknown target '${t}' — expected one of: core postgres all" ;;
    esac
done

log "deployment complete"

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
#   HEALTH_TIMEOUT   seconds to wait for healthy (default 300)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

ENV_FILE="${SCRIPT_DIR}/.env"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"

CORE_NAME="hivemind-core"
CORE_COMPOSE="${SCRIPT_DIR}/docker-compose.core.yaml"
CORE_HEALTH_PATH="/v1/attestation"

PG_NAME="hivemind-pg"
PG_COMPOSE="${SCRIPT_DIR}/docker-compose.postgres.yaml"
PG_HEALTH_PATH="/health"

# ── Helpers ──

log()  { printf "\033[0;36m[deploy]\033[0m %s\n" "$*"; }
warn() { printf "\033[0;33m[deploy]\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[0;31m[deploy ERROR]\033[0m %s\n" "$*" >&2; exit 1; }

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
deploy_and_seal() {
    local name="$1"
    local compose="$2"
    local env_file="$3"

    log "deploying ${name} (compose=${compose})"
    phala deploy --cvm-id "${name}" -c "${compose}" -e "${env_file}" --wait

    log "re-sealing env vars on ${name}"
    phala envs update --cvm-id "${name}" -e "${env_file}"

    log "restarting ${name} to pick up re-sealed envs"
    phala cvms restart --cvm-id "${name}" >/dev/null
}

# Resolve service URL (looks up CVM app_id + builds the gateway URL).
# If `tls_passthrough=1`, use the `-<port>s.` suffix — Phala's gateway
# convention for TCP-passthrough routes, required when the container
# terminates TLS itself (HIVEMIND_ENCLAVE_TLS=1).
service_url() {
    local name="$1"
    local port="$2"
    local tls_passthrough="${3:-0}"
    local app_id
    app_id=$(phala cvms list 2>/dev/null \
        | awk -v n="${name}" '$2==n { print $1; exit }')
    [ -n "${app_id}" ] || die "could not resolve app_id for ${name}"
    local suffix=""
    [ "${tls_passthrough}" = "1" ] && suffix="s"
    echo "https://${app_id}-${port}${suffix}.dstack-pha-prod5.phala.network"
}

# Does this core compose have enclave TLS enabled (default or override)?
core_tls_enabled() {
    local compose="$1"
    # Enabled if compose sets a truthy default for HIVEMIND_ENCLAVE_TLS
    # (e.g. `${HIVEMIND_ENCLAVE_TLS:-1}`) OR the env file overrides it.
    grep -qE '^[[:space:]]*HIVEMIND_ENCLAVE_TLS:[[:space:]]*\$\{HIVEMIND_ENCLAVE_TLS:-[^}]+\}' "${compose}" && return 0
    grep -qE '^[[:space:]]*HIVEMIND_ENCLAVE_TLS=[1-9]' "${ENV_FILE}" 2>/dev/null && return 0
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
        code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "${url}" 2>/dev/null || echo "000")
        if [ "${code}" = "200" ]; then
            local elapsed=$(( $(date +%s) - started ))
            log "${name} healthy after ${elapsed}s"
            return 0
        fi
        if [ $(( $(date +%s) - started )) -ge "${timeout}" ]; then
            warn "${name} not healthy after ${timeout}s (last code: ${code})"
            warn "── serial logs (last 40 lines) ──"
            phala cvms serial-logs --cvm-id "${name}" 2>/dev/null | tail -40 >&2 || true
            die "${name} failed to become healthy — see serial logs above"
        fi
        sleep 5
    done
}

# ── Per-service entry points ──

deploy_core() {
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

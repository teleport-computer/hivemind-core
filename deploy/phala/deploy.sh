#!/bin/bash
set -euo pipefail

# deploy.sh — Resolve image digests and deploy to Phala Cloud
#
# Resolves the latest digest for each image, pins docker-compose files,
# then runs phala deploy to update the CVM.
#
# Usage:
#   ./deploy/phala/deploy.sh                    # deploy all CVMs
#   ./deploy/phala/deploy.sh core               # deploy only core
#   ./deploy/phala/deploy.sh scope index        # deploy scope and index
#
# Prerequisites:
#   - phala CLI authenticated (phala login)
#   - GHCR_TOKEN env var set (PAT with read:packages scope)
#     OR already logged into ghcr.io via docker login

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

REGISTRY="ghcr.io"
NAMESPACE="account-link"
TAG="${IMAGE_TAG:-latest}"

# CVM app IDs (update these after first deploy)
CVM_CORE="37d4e4242a99cde0b9066dd81f854cb09e164f38"
CVM_POSTGRES="2181af2d134123a46613f62a0311dd1f5af984be"
CVM_SCOPE="2808148521da8034770fecb39f556d76a7948b2f"
CVM_INDEX="573e7dca64e67f874e25ffa4dca2add5754d8ca7"
CVM_MEDIATOR="849dfb7f617c14050123e7c44644702e50f2b99d"

# ── Digest resolution ──

resolve_digest() {
    local image="$1"
    local tag="${2:-$TAG}"
    local full="${REGISTRY}/${NAMESPACE}/${image}"

    # Try docker manifest inspect (works if logged in to ghcr.io)
    local digest
    digest=$(docker manifest inspect "${full}:${tag}" 2>/dev/null \
        | python3 -c "
import json, sys
m = json.load(sys.stdin)
# OCI index: find linux/amd64 manifest digest
if 'manifests' in m:
    for mf in m['manifests']:
        p = mf.get('platform', {})
        if p.get('os') == 'linux' and p.get('architecture') == 'amd64':
            print(mf['digest']); break
    else:
        # fallback: first manifest
        print(m['manifests'][0]['digest'])
elif 'config' in m:
    # single-arch manifest — use the index digest
    print('${tag}')
" 2>/dev/null) || true

    if [ -z "$digest" ]; then
        echo "WARNING: Could not resolve digest for ${full}:${tag}, using tag only" >&2
        echo "${full}:${tag}"
    else
        echo "${full}:${tag}@${digest}"
    fi
}

# ── Pin image in compose file ──

pin_compose() {
    local compose_file="$1"
    local image_name="$2"
    local pinned_ref="$3"

    # Create a temp copy with pinned image reference
    local tmp="${compose_file}.pinned"
    sed "s|${REGISTRY}/${NAMESPACE}/${image_name}:${TAG}|${pinned_ref}|g" \
        "${compose_file}" > "${tmp}"
    echo "${tmp}"
}

# ── Deploy functions ──

deploy_service() {
    local name="$1"
    local cvm_id="$2"
    local compose_file="$3"
    local image_name="$4"
    local env_flag="${5:-}"

    echo ""
    echo "━━━ Deploying ${name} ━━━"

    # Resolve digest
    echo "  Resolving digest for ${image_name}:${TAG}..."
    local pinned
    pinned=$(resolve_digest "${image_name}" "${TAG}")
    echo "  Pinned: ${pinned}"

    # Pin compose file
    local pinned_compose
    pinned_compose=$(pin_compose "${compose_file}" "${image_name}" "${pinned}")

    echo "  Compose diff:"
    diff "${compose_file}" "${pinned_compose}" || true

    # Deploy
    local cmd=(phala deploy --cvm-id "${cvm_id}" -c "${pinned_compose}")
    if [ -n "${env_flag}" ]; then
        cmd+=(-e "${env_flag}")
    fi

    echo "  Running: ${cmd[*]}"
    "${cmd[@]}"

    # Cleanup temp file
    rm -f "${pinned_compose}"
    echo "  ✅ ${name} deployed"
}

deploy_postgres() {
    # Postgres compose has two images: db + sql-proxy
    local compose="${SCRIPT_DIR}/docker-compose.postgres.yaml"

    echo ""
    echo "━━━ Deploying postgres ━━━"

    local pg_pinned sql_pinned
    echo "  Resolving digests..."
    pg_pinned=$(resolve_digest "hivemind-postgres" "${TAG}")
    sql_pinned=$(resolve_digest "hivemind-sql-proxy" "${TAG}")
    echo "  postgres: ${pg_pinned}"
    echo "  sql-proxy: ${sql_pinned}"

    local tmp="${compose}.pinned"
    sed \
        -e "s|${REGISTRY}/${NAMESPACE}/hivemind-postgres:${TAG}|${pg_pinned}|g" \
        -e "s|${REGISTRY}/${NAMESPACE}/hivemind-sql-proxy:${TAG}|${sql_pinned}|g" \
        "${compose}" > "${tmp}"

    echo "  Compose diff:"
    diff "${compose}" "${tmp}" || true

    phala deploy --cvm-id "${CVM_POSTGRES}" -c "${tmp}" -e "${SCRIPT_DIR}/.env.postgres"
    rm -f "${tmp}"
    echo "  ✅ postgres deployed"
}

deploy_core() {
    deploy_service "core" "${CVM_CORE}" \
        "${SCRIPT_DIR}/docker-compose.core.yaml" \
        "hivemind-core" \
        "${SCRIPT_DIR}/.env.core"
}

deploy_scope() {
    deploy_service "scope" "${CVM_SCOPE}" \
        "${SCRIPT_DIR}/docker-compose.scope.yaml" \
        "hivemind-scope"
}

deploy_index() {
    deploy_service "index" "${CVM_INDEX}" \
        "${SCRIPT_DIR}/docker-compose.index.yaml" \
        "hivemind-index"
}

deploy_mediator() {
    deploy_service "mediator" "${CVM_MEDIATOR}" \
        "${SCRIPT_DIR}/docker-compose.mediator.yaml" \
        "hivemind-mediator"
}

# ── Verify health ──

check_health() {
    local name="$1"
    local url="$2"

    local status
    status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "${url}" 2>/dev/null) || status="000"
    if [ "${status}" = "200" ]; then
        echo "  ✅ ${name}: healthy (${url})"
    else
        echo "  ⚠️  ${name}: HTTP ${status} (${url})"
    fi
}

verify_all() {
    echo ""
    echo "━━━ Health checks ━━━"
    check_health "postgres/sql-proxy" "https://${CVM_POSTGRES}-8080.dstack-pha-prod5.phala.network/health"
    check_health "scope"    "https://${CVM_SCOPE}-8080.dstack-pha-prod5.phala.network/health"
    check_health "index"    "https://${CVM_INDEX}-8080.dstack-pha-prod5.phala.network/health"
    check_health "mediator" "https://${CVM_MEDIATOR}-8080.dstack-pha-prod5.phala.network/health"
    check_health "core"     "https://${CVM_CORE}-8100.dstack-pha-prod5.phala.network/v1/health"
}

# ── Entry point ──

TARGETS=("${@:-all}")
if [ "${#TARGETS[@]}" -eq 0 ] || [ "${TARGETS[0]}" = "all" ]; then
    TARGETS=(postgres scope index mediator core)
fi

echo "🚀 Deploying: ${TARGETS[*]}"
echo "   Tag: ${TAG}"

for target in "${TARGETS[@]}"; do
    case "${target}" in
        core)      deploy_core ;;
        postgres)  deploy_postgres ;;
        scope)     deploy_scope ;;
        index)     deploy_index ;;
        mediator)  deploy_mediator ;;
        *)
            echo "Unknown target: ${target}"
            echo "Valid targets: core, postgres, scope, index, mediator, all"
            exit 1
            ;;
    esac
done

# Wait a bit for CVMs to restart, then verify
echo ""
echo "Waiting 30s for CVMs to restart..."
sleep 30
verify_all

echo ""
echo "🎉 Deployment complete!"

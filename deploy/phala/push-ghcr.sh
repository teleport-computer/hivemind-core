#!/bin/bash
set -euo pipefail

# push-ghcr.sh — Build and push all hivemind images to GHCR
#
# Usage:
#   ./deploy/phala/push-ghcr.sh              # push all images
#   ./deploy/phala/push-ghcr.sh core         # push only hivemind-core
#   ./deploy/phala/push-ghcr.sh postgres     # push only postgres
#   ./deploy/phala/push-ghcr.sh agents       # push all persistent agent images
#   ./deploy/phala/push-ghcr.sh scope        # push only scope agent
#
# Prerequisites:
#   export GHCR_TOKEN=ghp_xxx
#   docker login ghcr.io -u zzh --password-stdin <<< "$GHCR_TOKEN"

REGISTRY="ghcr.io/zzh"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TAG="${IMAGE_TAG:-latest}"

# --- Base SDK image (required by agent builds) ---

build_base_sdk() {
    echo "==> Building agent SDK base image..."
    docker build \
        -t "hivemind-agent-sdk-base:latest" \
        -f "${REPO_ROOT}/agents/base/Dockerfile.agent-sdk" \
        "${REPO_ROOT}/agents/base"
}

# --- Core services ---

push_core() {
    echo "==> Building hivemind-core..."
    docker build \
        -t "${REGISTRY}/hivemind-core:${TAG}" \
        -f "${REPO_ROOT}/deploy/Dockerfile" \
        "${REPO_ROOT}"
    echo "==> Pushing hivemind-core:${TAG}..."
    docker push "${REGISTRY}/hivemind-core:${TAG}"
    echo "    Done: ${REGISTRY}/hivemind-core:${TAG}"
}

push_postgres() {
    echo "==> Building hivemind-postgres..."
    docker build \
        -t "${REGISTRY}/hivemind-postgres:${TAG}" \
        -f "${REPO_ROOT}/deploy/postgres/Dockerfile" \
        "${REPO_ROOT}/deploy"
    echo "==> Pushing hivemind-postgres:${TAG}..."
    docker push "${REGISTRY}/hivemind-postgres:${TAG}"
    echo "    Done: ${REGISTRY}/hivemind-postgres:${TAG}"
}

push_sql_proxy() {
    echo "==> Building hivemind-sql-proxy..."
    docker build \
        -t "${REGISTRY}/hivemind-sql-proxy:${TAG}" \
        -f "${REPO_ROOT}/deploy/postgres/Dockerfile.sql-proxy" \
        "${REPO_ROOT}/deploy"
    echo "==> Pushing hivemind-sql-proxy:${TAG}..."
    docker push "${REGISTRY}/hivemind-sql-proxy:${TAG}"
    echo "    Done: ${REGISTRY}/hivemind-sql-proxy:${TAG}"
}

# --- Persistent agent images ---

push_agent() {
    local role="$1"
    local agent_dir="default-${role}"
    local image_name="hivemind-${role}"

    echo "==> Building ${image_name} (persistent agent)..."
    docker build \
        --build-arg "AGENT=${agent_dir}" \
        -t "${REGISTRY}/${image_name}:${TAG}" \
        -f "${REPO_ROOT}/deploy/phala/Dockerfile.persistent-agent" \
        "${REPO_ROOT}"
    echo "==> Pushing ${image_name}:${TAG}..."
    docker push "${REGISTRY}/${image_name}:${TAG}"
    echo "    Done: ${REGISTRY}/${image_name}:${TAG}"
}

push_query_base() {
    echo "==> Building hivemind-query-base..."
    docker build \
        -t "${REGISTRY}/hivemind-query-base:${TAG}" \
        -f "${REPO_ROOT}/deploy/phala/Dockerfile.query-base" \
        "${REPO_ROOT}"
    echo "==> Pushing hivemind-query-base:${TAG}..."
    docker push "${REGISTRY}/hivemind-query-base:${TAG}"
    echo "    Done: ${REGISTRY}/hivemind-query-base:${TAG}"
}

push_all_agents() {
    build_base_sdk
    push_agent scope
    push_agent index
    push_agent mediator
    push_query_base
}

# --- Entry point ---

TARGET="${1:-all}"

case "$TARGET" in
    core)      push_core ;;
    sql-proxy) push_sql_proxy ;;
    postgres)  push_postgres ;;
    base-sdk)  build_base_sdk ;;
    scope)      build_base_sdk; push_agent scope ;;
    index)      build_base_sdk; push_agent index ;;
    mediator)   build_base_sdk; push_agent mediator ;;
    query-base) build_base_sdk; push_query_base ;;
    agents)     push_all_agents ;;
    all)
        build_base_sdk
        push_core
        push_postgres
        push_sql_proxy
        push_agent scope
        push_agent index
        push_agent mediator
        push_query_base
        ;;
    *)
        echo "Usage: $0 [core|postgres|sql-proxy|base-sdk|scope|index|mediator|agents|all]"
        exit 1
        ;;
esac

echo ""
echo "==> Done. Deploy to Phala:"
echo "    1. phala deploy -n hivemind-pg       -c deploy/phala/docker-compose.postgres.yaml"
echo "    2. phala deploy -n hivemind-scope    -c deploy/phala/docker-compose.scope.yaml"
echo "    3. phala deploy -n hivemind-index    -c deploy/phala/docker-compose.index.yaml"
echo "    4. phala deploy -n hivemind-mediator -c deploy/phala/docker-compose.mediator.yaml"
echo "    5. phala deploy -n hivemind-core     -c deploy/phala/docker-compose.core.yaml"
echo ""
echo "    Set HIVEMIND_PHALA_SCOPE_URL / INDEX_URL / MEDIATOR_URL to each CVM's public URL"

#!/bin/bash
set -euo pipefail

# entrypoint-wrapper.sh — runs before the standard postgres entrypoint.
#
# Derives keys from dstack KMS:
#   1. DB password (shared with hivemind container — both derive from same KMS path)
#   2. WAL-G backup encryption key
# Then hands off to the official postgres Docker entrypoint.

DSTACK_SOCK="${DSTACK_SOCKET:-/var/run/dstack.sock}"

kms_get_key() {
    local path="$1"
    local purpose="${2:-encryption}"
    curl -sf --unix-socket "$DSTACK_SOCK" \
        -X POST "http://dstack/GetKey" \
        -H "Content-Type: application/json" \
        -d "{\"path\": \"${path}\", \"purpose\": \"${purpose}\"}" \
        | python3 -c "import sys, json; print(json.load(sys.stdin)['key'])"
}

if [ -S "$DSTACK_SOCK" ]; then
    # --- Derive DB password from KMS ---
    echo "[pg-init] Deriving DB password from dstack KMS..."
    export POSTGRES_PASSWORD=$(kms_get_key "/hivemind/db-password" "authentication" | head -c 32)
    echo "[pg-init] DB password derived"

    # --- Derive backup encryption key from KMS ---
    echo "[pg-init] Deriving backup key from dstack KMS..."
    RAW_KEY=$(kms_get_key "/hivemind/backup" "encryption")
    # Take first 64 hex chars = 32 bytes for libsodium secretbox key
    export WALG_LIBSODIUM_KEY="${RAW_KEY:0:64}"
    echo "[pg-init] Backup encryption key derived (${#WALG_LIBSODIUM_KEY} hex chars)"
else
    echo "[pg-init] No dstack socket at $DSTACK_SOCK — using env vars directly"
    echo "[pg-init] (This is normal for local development)"
fi

# --- Start supercronic for daily base backups ---
if [ -n "${WALG_S3_PREFIX:-}" ] && [ -n "${WALG_LIBSODIUM_KEY:-}" ]; then
    echo "[pg-init] Starting backup cron (daily base backup at 03:00 UTC)..."
    supercronic /etc/crontab &
fi

# --- Hand off to official postgres entrypoint ---
exec docker-entrypoint.sh "$@"

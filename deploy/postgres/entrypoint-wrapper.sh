#!/bin/bash
set -euo pipefail

# entrypoint-wrapper.sh — runs before the standard postgres entrypoint.
#
# Derives keys from dstack KMS via dstack-sdk:
#   1. DB password (shared with hivemind container — both derive from same KMS path)
#   2. WAL-G backup encryption key
# Then hands off to the official postgres Docker entrypoint.

DSTACK_SOCK="${DSTACK_SOCKET:-/var/run/dstack.sock}"
KMS_HELPER="/usr/local/bin/kms.py"

if [ -S "$DSTACK_SOCK" ]; then
    # --- Derive DB password from KMS ---
    echo "[pg-init] Deriving DB password from dstack KMS..."
    export POSTGRES_PASSWORD=$(python3 "$KMS_HELPER" /hivemind/db-password --purpose authentication --first 32)
    if [ -z "$POSTGRES_PASSWORD" ]; then
        echo "[pg-init] FATAL: KMS returned empty DB password"
        exit 1
    fi
    echo "[pg-init] DB password derived"

    # --- Derive backup encryption key from KMS ---
    echo "[pg-init] Deriving backup key from dstack KMS..."
    export WALG_LIBSODIUM_KEY=$(python3 "$KMS_HELPER" /hivemind/backup --purpose encryption --first 64)
    if [ ${#WALG_LIBSODIUM_KEY} -ne 64 ]; then
        echo "[pg-init] FATAL: Backup key must be 64 hex chars, got ${#WALG_LIBSODIUM_KEY}"
        exit 1
    fi
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

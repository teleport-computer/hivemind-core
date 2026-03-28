#!/bin/bash
set -euo pipefail

# entrypoint-wrapper.sh — runs before the standard postgres entrypoint.
#
# Responsibilities:
#   1. Validate POSTGRES_PASSWORD is set (via env var or docker-compose)
#   2. Validate WAL-G backup config if R2/S3 is configured
#   3. Start backup cron if WAL-G is fully configured
#   4. Hand off to the official postgres Docker entrypoint
#
# KMS integration temporarily disabled — all secrets come from environment.
# To re-enable KMS, see git history for the dstack-sdk derivation block.

# --- Validate DB password ---
if [ -z "${POSTGRES_PASSWORD:-}" ]; then
    echo "[pg-init] FATAL: POSTGRES_PASSWORD not set. Provide it via environment."
    exit 1
fi

# --- Validate WAL-G backup config ---
if [ -n "${WALG_S3_PREFIX:-}" ]; then
    if [ -n "${WALG_LIBSODIUM_KEY:-}" ]; then
        if [ ${#WALG_LIBSODIUM_KEY} -ne 64 ]; then
            echo "[pg-init] FATAL: WALG_LIBSODIUM_KEY must be 64 hex chars, got ${#WALG_LIBSODIUM_KEY}"
            exit 1
        fi
        echo "[pg-init] WAL-G backup configured (encrypted, S3 prefix: ${WALG_S3_PREFIX})"
    else
        echo "[pg-init] WAL-G backup configured (unencrypted, S3 prefix: ${WALG_S3_PREFIX})"
    fi
else
    echo "[pg-init] WAL-G backup not configured (no WALG_S3_PREFIX)"
fi

# --- Start cron for daily base backups ---
if [ -n "${WALG_S3_PREFIX:-}" ]; then
    echo "[pg-init] Starting backup cron (daily base backup at 03:00 UTC)..."
    # Export env vars so cron jobs can access them
    printenv | grep -E '^(WALG_|AWS_|PGDATA|POSTGRES_)' > /etc/environment.walg
    sed -i '1i BASH_ENV=/etc/environment.walg' /etc/cron.d/walg-backup
    cron
fi

# --- Hand off to official postgres entrypoint ---
exec docker-entrypoint.sh "$@"

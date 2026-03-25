#!/bin/bash
set -euo pipefail

# boot.sh — CVM boot script for the hivemind application container.
#
# Runs as the entrypoint for the hivemind service. Responsibilities:
#   1. Derive DB password from dstack KMS (deterministic, shared with db container)
#   2. Wait for Postgres to be ready
#   3. Start hivemind-core

DSTACK_SOCK="${DSTACK_SOCKET:-/var/run/dstack.sock}"
KMS_HELPER="/app/deploy/kms.py"

# --- Derive DB password from KMS ---
# Both db and hivemind containers derive the same password from KMS,
# so no pre-shared secret is needed.
if [ -z "${DB_PASS:-}" ]; then
    if [ -S "$DSTACK_SOCK" ]; then
        echo "[boot] Deriving DB password from dstack KMS..."
        DB_PASS=$(python3 "$KMS_HELPER" /hivemind/db-password --purpose authentication --first 32)
        export DB_PASS
        if [ -z "$DB_PASS" ]; then
            echo "[boot] FATAL: KMS returned empty DB password"
            exit 1
        fi
        echo "[boot] DB password derived from KMS"
    else
        echo "[boot] No dstack socket — using random DB_PASS (local dev)"
        export DB_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    fi
fi

export HIVEMIND_DATABASE_URL="postgresql://hivemind:${DB_PASS}@db:5432/hivemind"

# --- Wait for Postgres ---
echo "[boot] Waiting for Postgres..."
for i in $(seq 1 60); do
    if pg_isready -h db -U hivemind -q 2>/dev/null; then
        echo "[boot] Postgres is ready"
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "[boot] ERROR: Postgres not ready after 60s"
        exit 1
    fi
    sleep 1
done

# --- Start hivemind ---
echo "[boot] Starting hivemind-core..."
exec python3 -m hivemind.server

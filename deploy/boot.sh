#!/bin/bash
set -euo pipefail

# boot.sh — Boot script for the hivemind application container.
#
# Runs as the entrypoint for the hivemind service. Responsibilities:
#   1. If DATABASE_URL is already set (Phala mode / HTTP proxy), use it as-is
#   2. Otherwise: validate or generate DB_PASS, build postgres DSN, wait for DB
#   3. Start hivemind-core

if [ -n "${HIVEMIND_DATABASE_URL:-}" ]; then
    # DATABASE_URL already provided (e.g. SQL proxy URL in Phala mode) — skip
    # postgres-specific setup and start directly.
    echo "[boot] HIVEMIND_DATABASE_URL already set, skipping local postgres wait"
else
    # --- Local / Docker Compose mode: connect to postgres sidecar ---
    if [ -z "${DB_PASS:-}" ]; then
        echo "[boot] WARNING: No DB_PASS set — generating random password (local dev only)"
        export DB_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
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
fi

# --- Start hivemind ---
echo "[boot] Starting hivemind-core..."
exec python3 -m hivemind.server

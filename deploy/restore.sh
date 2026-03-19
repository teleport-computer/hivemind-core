#!/bin/bash
set -euo pipefail

# restore.sh — Restore a hivemind Postgres database from R2 backup.
#
# Usage (inside a CVM with dstack socket):
#   ./restore.sh
#
# Usage (local, with key provided):
#   WALG_LIBSODIUM_KEY=<hex> \
#   WALG_S3_PREFIX=s3://hivemind-backups \
#   AWS_ENDPOINT=https://xxx.r2.cloudflarestorage.com \
#   AWS_ACCESS_KEY_ID=... \
#   AWS_SECRET_ACCESS_KEY=... \
#   ./restore.sh
#
# What this does:
#   1. Derives the WAL-G decryption key from dstack KMS (if available)
#   2. Lists available backups
#   3. Fetches the latest base backup
#   4. Replays WAL segments to reach consistency
#   5. Starts Postgres in recovery mode

PGDATA="${PGDATA:-/var/lib/postgresql/data}"
DSTACK_SOCK="${DSTACK_SOCKET:-/var/run/dstack.sock}"
BACKUP_NAME="${1:-LATEST}"

# --- Derive decryption key ---
if [ -z "${WALG_LIBSODIUM_KEY:-}" ]; then
    if [ -S "$DSTACK_SOCK" ]; then
        echo "[restore] Deriving backup key from dstack KMS..."
        RAW_KEY=$(curl -sf --unix-socket "$DSTACK_SOCK" \
            -X POST "http://dstack/GetKey" \
            -H "Content-Type: application/json" \
            -d '{"path": "/hivemind/backup", "purpose": "encryption"}' \
            | python3 -c "import sys, json; print(json.load(sys.stdin)['key'])")
        export WALG_LIBSODIUM_KEY="${RAW_KEY:0:64}"
        echo "[restore] Key derived (${#WALG_LIBSODIUM_KEY} hex chars)"
    else
        echo "[restore] ERROR: No WALG_LIBSODIUM_KEY set and no dstack socket at $DSTACK_SOCK"
        exit 1
    fi
fi

# --- Validate R2 config ---
if [ -z "${WALG_S3_PREFIX:-}" ]; then
    echo "[restore] ERROR: WALG_S3_PREFIX not set"
    exit 1
fi

export AWS_S3_FORCE_PATH_STYLE="true"
export AWS_REGION="${AWS_REGION:-auto}"

# --- List available backups ---
echo "[restore] Available backups:"
wal-g backup-list

# --- Stop Postgres if running ---
if pg_isready -q 2>/dev/null; then
    echo "[restore] Stopping Postgres..."
    pg_ctl -D "$PGDATA" stop -m fast || true
    sleep 2
fi

# --- Clean PGDATA ---
echo "[restore] Cleaning $PGDATA..."
rm -rf "${PGDATA:?}"/*

# --- Fetch base backup ---
echo "[restore] Fetching backup: $BACKUP_NAME"
wal-g backup-fetch "$PGDATA" "$BACKUP_NAME"

# --- Configure recovery ---
touch "$PGDATA/recovery.signal"
cat >> "$PGDATA/postgresql.conf" <<EOF

# --- Recovery configuration (added by restore.sh) ---
restore_command = 'wal-g wal-fetch %f %p'
recovery_target_action = 'promote'
EOF

echo "[restore] Recovery configured. Start Postgres to replay WAL:"
echo "  pg_ctl -D $PGDATA start"
echo ""
echo "Or if running in Docker, just start the container normally."

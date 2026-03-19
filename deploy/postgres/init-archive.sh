#!/bin/bash
set -euo pipefail

# init-archive.sh — runs on first Postgres init (via docker-entrypoint-initdb.d/).
#
# Configures continuous WAL archiving via wal-g if R2/S3 is configured.
# This script runs as the postgres user inside the container.

if [ -z "${WALG_S3_PREFIX:-}" ]; then
    echo "[init-archive] WALG_S3_PREFIX not set — WAL archiving disabled"
    exit 0
fi

echo "[init-archive] Configuring WAL archiving..."

# Enable WAL archiving in postgresql.conf
cat >> "$PGDATA/postgresql.conf" <<EOF

# --- WAL-G continuous archiving ---
archive_mode = on
archive_command = 'wal-g wal-push %p'
archive_timeout = 60
wal_level = replica
EOF

echo "[init-archive] WAL archiving configured (archive_timeout=60s)"

# Take an initial base backup after postgres fully starts
# (this runs in the background, waiting for postgres to be ready)
(
    sleep 10  # wait for postgres to finish starting
    echo "[init-archive] Taking initial base backup..."
    wal-g backup-push "$PGDATA" 2>&1 | head -5
    echo "[init-archive] Initial base backup complete"
) &

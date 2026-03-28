#!/bin/bash
set -euo pipefail

# backup-push.sh — called by cron daily to push a base backup.

# Source WAL-G env vars (exported by entrypoint-wrapper.sh for cron)
if [ -f /etc/environment.walg ]; then
    set -a
    . /etc/environment.walg
    set +a
fi

: "${PGDATA:=/var/lib/postgresql/data}"

echo "[backup] Starting daily base backup at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

wal-g backup-push "$PGDATA"

# Retain last 7 base backups, delete older ones and unreferenced WAL
wal-g delete retain FULL 7 --confirm

echo "[backup] Daily base backup complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

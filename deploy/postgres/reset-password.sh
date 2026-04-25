#!/bin/bash
# reset-password.sh — align hivemind DB user password to POSTGRES_PASSWORD env.
#
# Postgres initializes pg_authid on first boot using POSTGRES_PASSWORD; after
# that the value in the env is ignored. If the deploy env drifts from what
# was initialized (e.g. rotated secret, lost original .env), the sql-proxy
# sidecar can't auth. This runs in the background after postgres is ready
# and idempotently ALTER USERs the hivemind role to match the current env.
#
# Connects as the `postgres` superuser via unix socket peer auth (default in
# postgres:16's pg_hba.conf), which doesn't require a password.

set -uo pipefail

log() { echo "[reset-pw] $*"; }

if [ -z "${POSTGRES_PASSWORD:-}" ]; then
    log "POSTGRES_PASSWORD not set, nothing to do"
    exit 0
fi

DB_USER="${POSTGRES_USER:-postgres}"
if [ "$DB_USER" = "postgres" ]; then
    log "POSTGRES_USER is 'postgres' — skip (default role alignment handled by docker-entrypoint)"
    exit 0
fi

# Wait up to 2min for postgres to be ready on the unix socket
for _ in $(seq 1 60); do
    if su postgres -c "pg_isready -h /var/run/postgresql" >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

if ! su postgres -c "pg_isready -h /var/run/postgresql" >/dev/null 2>&1; then
    log "postgres never became ready, giving up"
    exit 1
fi

# ALTER USER is idempotent. Connect as the DB_USER role (not 'postgres' — the
# 'postgres' role doesn't exist when initdb ran with POSTGRES_USER=hivemind).
# Local unix-socket auth is `trust` by default in postgres:16's pg_hba.conf,
# so no password is needed for the socket connection itself.
PSQL_OUT=$(su postgres -c "psql -h /var/run/postgresql -U \"${DB_USER}\" -d postgres -v ON_ERROR_STOP=1 -c \"ALTER USER \\\"${DB_USER}\\\" WITH PASSWORD '${POSTGRES_PASSWORD}';\"" 2>&1) \
    && log "aligned password for role '${DB_USER}'" \
    || log "ALTER USER failed for role '${DB_USER}': ${PSQL_OUT}"

#!/usr/bin/env bash
set -euo pipefail

# import-data.sh — Import data into Phala-deployed PostgreSQL via the SQL proxy.
#
# Usage:
#   # Import a SQL dump (.sql file)
#   ./import-data.sh sql  dump.sql
#
#   # Import a CSV file into a table
#   ./import-data.sh csv  users  users.csv
#
#   # Pipe SQL from pg_dump
#   pg_dump -h localhost -U myuser mydb --data-only --inserts | ./import-data.sh sql -
#
# Environment:
#   SQL_PROXY_URL  — e.g. https://<cvm_id>-8080.app.phala.network  (required)
#   SQL_PROXY_KEY  — shared secret                                   (required)
#   TENANT_DB      — target tenant DB, e.g. tenant_t_abc123          (optional,
#                    omit to target the maintenance DB for raw imports)

: "${SQL_PROXY_URL:?Set SQL_PROXY_URL to the proxy endpoint}"
: "${SQL_PROXY_KEY:?Set SQL_PROXY_KEY}"

TENANT_HEADER=()
if [ -n "${TENANT_DB:-}" ]; then
  TENANT_HEADER=(-H "X-Tenant-DB: ${TENANT_DB}")
fi

CMD="${1:?Usage: $0 <sql|csv> ...}"
shift

case "$CMD" in
  sql)
    FILE="${1:?Usage: $0 sql <file.sql or ->}"
    if [ "$FILE" = "-" ]; then
      DATA="$(cat)"
    else
      DATA="$(cat "$FILE")"
    fi
    echo "[import] Sending SQL dump ($(echo "$DATA" | wc -c | tr -d ' ') bytes)${TENANT_DB:+ to '$TENANT_DB'}..."
    curl -sf -X POST "${SQL_PROXY_URL}/import/sql" \
      -H "X-Proxy-Key: ${SQL_PROXY_KEY}" \
      "${TENANT_HEADER[@]}" \
      -H "Content-Type: text/plain" \
      --data-binary "$DATA" | python3 -m json.tool
    ;;

  csv)
    TABLE="${1:?Usage: $0 csv <table> <file.csv> [delimiter]}"
    FILE="${2:?Usage: $0 csv <table> <file.csv> [delimiter]}"
    DELIM="${3:-,}"
    CSV_DATA="$(cat "$FILE")"
    PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'table': sys.argv[1],
    'data': sys.argv[2],
    'delimiter': sys.argv[3],
    'header': True,
}))
" "$TABLE" "$CSV_DATA" "$DELIM")
    echo "[import] Importing CSV into '$TABLE' ($(echo "$CSV_DATA" | wc -l | tr -d ' ') lines)${TENANT_DB:+ of '$TENANT_DB'}..."
    curl -sf -X POST "${SQL_PROXY_URL}/import/csv" \
      -H "X-Proxy-Key: ${SQL_PROXY_KEY}" \
      "${TENANT_HEADER[@]}" \
      -H "Content-Type: application/json" \
      -d "$PAYLOAD" | python3 -m json.tool
    ;;

  *)
    echo "Usage: $0 <sql|csv> ..."
    echo "  $0 sql <file.sql>              Import SQL dump"
    echo "  $0 csv <table> <file.csv>      Import CSV into table"
    exit 1
    ;;
esac

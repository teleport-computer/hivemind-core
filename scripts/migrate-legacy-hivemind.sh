#!/usr/bin/env bash
set -euo pipefail

# migrate-legacy-hivemind.sh — one-shot migration for pre-multi-tenant DBs.
#
# What it does:
#   1. Generates a fresh tenant_id (t_<hex>)
#   2. Renames the old single-tenant database (default: "hivemind")
#      to the new multi-tenant naming (tenant_<tenant_id>)
#      via POST /v1/admin/rename-database
#   3. Registers it as a tenant via POST /v1/admin/tenants/register
#      (with the matching tenant_id so names stay aligned)
#   4. Prints the tenant's one-time API key
#
# What you still have to do yourself:
#   - Immediately run `hivemind init` + `hivemind rotate-key` as the tenant.
#     The admin saw the plaintext key; rotating cuts them out of the loop.
#
# Env:
#   CORE_URL=https://<core_cvm_id>-8100.app.phala.network   (required)
#   HIVEMIND_ADMIN_KEY=...                                   (required)
#   LEGACY_DB=hivemind                                       (optional, default "hivemind")
#   TENANT_NAME="migrated-legacy"                            (optional)
#
# Exit codes: 0=ok, 2=arg/env error, 3=HTTP error

: "${CORE_URL:?CORE_URL required (e.g. https://<cvm_id>-8100.app.phala.network)}"
: "${HIVEMIND_ADMIN_KEY:?HIVEMIND_ADMIN_KEY required}"
LEGACY_DB="${LEGACY_DB:-hivemind}"
TENANT_NAME="${TENANT_NAME:-migrated-legacy}"

say() { printf "\033[1;36m[migrate]\033[0m %s\n" "$*"; }
die() { printf "\033[1;31m[migrate]\033[0m %s\n" "$*" >&2; exit 3; }

command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 2; }
command -v curl    >/dev/null || { echo "curl not found"    >&2; exit 2; }

TENANT_ID="t_$(python3 -c 'import secrets; print(secrets.token_hex(6))')"
NEW_DB="tenant_${TENANT_ID}"

say "Legacy DB:  ${LEGACY_DB}"
say "New DB:     ${NEW_DB}"
say "Tenant ID:  ${TENANT_ID}"

# --- 1. Rename the database on the Postgres cluster ---
say "Renaming database via ${CORE_URL}/v1/admin/rename-database"
RENAME_RESP="$(curl -fsS -X POST "${CORE_URL}/v1/admin/rename-database" \
    -H "Authorization: Bearer ${HIVEMIND_ADMIN_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"old_name\":\"${LEGACY_DB}\",\"new_name\":\"${NEW_DB}\"}" \
    )" || die "rename failed (is the DB in use? check: no clients connected to ${LEGACY_DB})"
echo "  → $RENAME_RESP"

# --- 2. Register the renamed DB as a tenant ---
say "Registering tenant"
REG_RESP="$(curl -fsS -X POST "${CORE_URL}/v1/admin/tenants/register" \
    -H "Authorization: Bearer ${HIVEMIND_ADMIN_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"${TENANT_NAME}\",\"db_name\":\"${NEW_DB}\",\"tenant_id\":\"${TENANT_ID}\"}" \
    )" || die "register failed"

API_KEY="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['api_key'])" "$REG_RESP")"

say "Done."
echo ""
echo "Tenant:   ${TENANT_ID}  (${TENANT_NAME})"
echo "Database: ${NEW_DB}"
echo ""
echo "API key (this is the ONLY time you will see it):"
echo "  ${API_KEY}"
echo ""
echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║  NEXT STEP — rotate the key immediately (admin still knows it):   ║"
echo "║                                                                   ║"
echo "║    hivemind init --service ${CORE_URL} \\"
echo "║                  --api-key ${API_KEY}"
echo "║    hivemind rotate-key                                            ║"
echo "║                                                                   ║"
echo "║  After rotation, only the TEE knows anything that maps to this    ║"
echo "║  tenant's data. The admin is cut out of the loop.                 ║"
echo "╚═══════════════════════════════════════════════════════════════════╝"

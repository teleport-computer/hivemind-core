#!/usr/bin/env bash
set -euo pipefail

# quickstart.sh — zero to first successful query.
#
# What it does:
#   1. Scaffold .env (prompts for LLM key if missing)
#   2. Build the agent base image + all four default agent images in parallel
#   3. Start Postgres via docker-compose
#   4. uv sync
#   5. Launch hivemind.server in the background
#   6. hivemind init → load a row → scope → query → print the answer
#   7. Print next-step cheat-sheet + shutdown command
#
# Prereqs: docker, uv (astral.sh/uv).
# Env:     HIVEMIND_LLM_API_KEY (prompted if .env missing)
#
# Usage:   ./scripts/quickstart.sh [--no-demo]   # skip the end-to-end query

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

DO_DEMO=1
for arg in "$@"; do
    case "$arg" in
        --no-demo) DO_DEMO=0 ;;
        *) echo "unknown arg: $arg"; exit 2 ;;
    esac
done

say() { printf "\033[1;36m[quickstart]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[quickstart]\033[0m %s\n" "$*" >&2; }
die() { printf "\033[1;31m[quickstart]\033[0m %s\n" "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker not found — install Docker Desktop first"
command -v uv >/dev/null || die "uv not found — install from https://astral.sh/uv"

# --- 1. Scaffold .env ---
if [ ! -f .env ]; then
    say "No .env found — creating one from .env.example"
    cp .env.example .env
    if [ -t 0 ] && [ -z "${HIVEMIND_LLM_API_KEY:-}" ]; then
        printf "Enter your LLM API key (OpenRouter by default, or anything with HIVEMIND_LLM_BASE_URL): "
        read -r LLM_KEY
        if [ -n "$LLM_KEY" ]; then
            # Replace the empty HIVEMIND_LLM_API_KEY= line
            python3 -c "
import pathlib
p = pathlib.Path('.env')
text = p.read_text()
text = text.replace('HIVEMIND_LLM_API_KEY=\n', f'HIVEMIND_LLM_API_KEY=$LLM_KEY\n')
p.write_text(text)
"
            say ".env written with LLM key"
        else
            warn ".env created without an LLM key — set HIVEMIND_LLM_API_KEY before running queries"
        fi
    fi
fi

# Load .env so the server sub-process inherits the vars
set -a
# shellcheck disable=SC1091
source .env
set +a

if [ -z "${HIVEMIND_LLM_API_KEY:-}" ] && [ "$DO_DEMO" -eq 1 ]; then
    warn "HIVEMIND_LLM_API_KEY not set — demo will be skipped"
    DO_DEMO=0
fi

# Point at local postgres for this session
export HIVEMIND_DATABASE_URL="${HIVEMIND_DATABASE_URL:-postgresql://hivemind:dev@localhost:5432/hivemind}"

# --- 2. Build all images in parallel ---
say "Building hivemind-agent-base + 4 default agent images (parallel)..."
BUILD_LOG="/tmp/hivemind-quickstart-build.log"
: > "$BUILD_LOG"

# Base must finish first (the four defaults FROM it)
docker build -t hivemind-agent-base -f agents/base/Dockerfile agents/base/ >>"$BUILD_LOG" 2>&1 \
    || { tail -40 "$BUILD_LOG"; die "base image build failed"; }

pids=()
for agent in default-index default-query default-scope default-mediator; do
    (
        docker build -t "hivemind-${agent}:local" "agents/${agent}/" >>"$BUILD_LOG" 2>&1
    ) &
    pids+=($!)
done

fail=0
for pid in "${pids[@]}"; do
    wait "$pid" || fail=1
done
if [ "$fail" -ne 0 ]; then
    tail -60 "$BUILD_LOG"
    die "one or more default-agent image builds failed (full log: $BUILD_LOG)"
fi
say "All 5 images built (log: $BUILD_LOG)"

# --- 3. Start Postgres ---
say "Starting Postgres..."
docker compose -f deploy/docker-compose.dev.yml up -d >/dev/null
for i in $(seq 1 30); do
    if docker compose -f deploy/docker-compose.dev.yml exec -T postgres \
        pg_isready -U hivemind -q 2>/dev/null; then
        break
    fi
    sleep 1
    [ "$i" -eq 30 ] && die "postgres did not become ready in 30s"
done

# --- 4. uv sync ---
say "uv sync..."
uv sync --quiet

# --- 5. Launch server ---
SERVER_LOG="/tmp/hivemind-server.log"
say "Launching hivemind.server → http://localhost:8100 (log: $SERVER_LOG)"
uv run python -m hivemind.server >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

cleanup() {
    if kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Wait for /v1/health
for i in $(seq 1 30); do
    if curl -fsS http://localhost:8100/v1/health >/dev/null 2>&1; then
        break
    fi
    sleep 1
    [ "$i" -eq 30 ] && { tail -40 "$SERVER_LOG"; die "server did not come up in 30s"; }
done
say "Server is up (pid $SERVER_PID)"

# --- 6. End-to-end demo ---
if [ "$DO_DEMO" -eq 1 ]; then
    say "Running end-to-end demo (hivemind init → load row → scope → query)"

    AUTH_HEADER=()
    if [ -n "${HIVEMIND_API_KEY:-}" ]; then
        AUTH_HEADER=(-H "Authorization: Bearer ${HIVEMIND_API_KEY}")
    fi

    uv run hivemind init \
        --service http://localhost:8100 \
        ${HIVEMIND_API_KEY:+--api-key "$HIVEMIND_API_KEY"} >/dev/null

    # Create a trivial table + row so the scope/query agents have something real.
    curl -fsS -X POST http://localhost:8100/v1/store \
        "${AUTH_HEADER[@]}" \
        -H "Content-Type: application/json" \
        -d '{"sql": "CREATE TABLE IF NOT EXISTS demo_notes (id SERIAL PRIMARY KEY, content TEXT)", "params": []}' \
        >/dev/null
    curl -fsS -X POST http://localhost:8100/v1/store \
        "${AUTH_HEADER[@]}" \
        -H "Content-Type: application/json" \
        -d '{"sql": "INSERT INTO demo_notes (content) VALUES (%s), (%s), (%s)", "params": ["sprint retro","design review","team lunch"]}' \
        >/dev/null
    say "Seeded demo_notes with 3 rows"

    say "Registering scope policy (takes ~30s — scope agent runs in Docker)"
    uv run hivemind scope "Allow aggregate counts only. Never expose individual row content." >/dev/null

    say "Running query (takes ~30s — query + mediator agents run in Docker)"
    QUERY_OUT="$(uv run hivemind query 'How many rows are in demo_notes?' 2>&1 || true)"
    echo "────────────────────────────────────────────────────────"
    echo "$QUERY_OUT"
    echo "────────────────────────────────────────────────────────"
fi

printf '\n\033[1;32m✓\033[0m hivemind is running at http://localhost:8100\n'
printf '  Server log: %s\n' "$SERVER_LOG"
printf '  Server pid: %s\n\n' "$SERVER_PID"
cat <<EOF
Next steps:
  uv run hivemind agents                 # list registered agents
  uv run hivemind runs                   # list recent runs
  uv run hivemind query "<question>"     # ask another question
  uv run hivemind run ./my-agent         # upload your own agent

Install hivemind as a top-level command (no 'uv run' prefix):
  uv tool install --editable .

Shut down:
  kill $SERVER_PID
  docker compose -f deploy/docker-compose.dev.yml down

EOF

# Detach the server from our trap so it keeps running after this script exits.
trap - EXIT
disown "$SERVER_PID" 2>/dev/null || true

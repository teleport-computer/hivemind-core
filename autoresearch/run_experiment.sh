#!/usr/bin/env bash
# autoresearch/run_experiment.sh — deterministic experiment runner
#
# Usage:
#   bash autoresearch/run_experiment.sh <label> [rounds]
#
# What it does:
#   1. Rebuild the default-scope docker image.
#   2. Verify the local server is up (start it if not).
#   3. Run the GAN bench against http://localhost:8100.
#   4. Parse the overall grade + defense/utility from the log.
#   5. Parse valid_rate from the server logs (counts scope_agent_failed
#      errors vs total query invocations).
#   6. Save the JSON to autoresearch/experiments/<ts>-<label>.json
#   7. Append one row to autoresearch/results.tsv
#
# Environment:
#   HIVEMIND_SERVER_URL — override server URL (default http://localhost:8100)
#   HIVEMIND_SCENARIO   — run a single scenario only (default: all 6)
#   HIVEMIND_SKIP_BUILD=1 — skip docker rebuild (use existing image)

set -euo pipefail

LABEL="${1:-unnamed}"
ROUNDS="${2:-1}"
URL="${HIVEMIND_SERVER_URL:-http://localhost:8100}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
EXPERIMENTS_DIR="$REPO_ROOT/autoresearch/experiments"
RESULTS_TSV="$REPO_ROOT/autoresearch/results.tsv"
mkdir -p "$EXPERIMENTS_DIR"

LOG_FILE="/tmp/autoresearch-${TS}-${LABEL}.log"
SERVER_LOG="/tmp/hivemind-server.log"

echo "[autoresearch] label=${LABEL} rounds=${ROUNDS}"
echo "[autoresearch] timestamp=${TS}"
echo "[autoresearch] log=${LOG_FILE}"

# 1. Rebuild image unless skipped
if [[ "${HIVEMIND_SKIP_BUILD:-0}" != "1" ]]; then
  echo "[autoresearch] Building hivemind-default-scope:local..."
  docker build -t hivemind-default-scope:local agents/default-scope \
    > /tmp/docker-build.log 2>&1 || {
    echo "[autoresearch] ERROR: docker build failed. See /tmp/docker-build.log"
    tail -20 /tmp/docker-build.log
    exit 1
  }
fi

# 2. Ensure server is up
if ! curl -s "$URL/v1/health" > /dev/null 2>&1; then
  echo "[autoresearch] Server not reachable at $URL — attempting to start..."
  nohup uv run python -m hivemind.server > "$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  echo "$SERVER_PID" > /tmp/hivemind-server.pid
  # Wait up to 30s for /v1/health
  for i in $(seq 1 30); do
    if curl -s "$URL/v1/health" > /dev/null 2>&1; then
      echo "[autoresearch] Server up after ${i}s"
      break
    fi
    sleep 1
  done
  if ! curl -s "$URL/v1/health" > /dev/null 2>&1; then
    echo "[autoresearch] ERROR: server didn't come up. See $SERVER_LOG"
    exit 1
  fi
fi

# Truncate server log so we can measure this run's valid_rate
: > "$SERVER_LOG" 2>/dev/null || true

# 3. Run the bench
BENCH_ARGS=(run --url "$URL" --rounds "$ROUNDS")
if [[ -n "${HIVEMIND_SCENARIO:-}" ]]; then
  BENCH_ARGS+=(--scenario "$HIVEMIND_SCENARIO")
fi

echo "[autoresearch] Running bench: uv run python -m bench.cli ${BENCH_ARGS[*]}"
set +e
uv run python -m bench.cli "${BENCH_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
BENCH_EXIT=$?
set -e

# 4. Parse overall grade / defense / utility from the log
DEFENSE="$(grep -E '^\s+OVERALL' "$LOG_FILE" | awk '{print $2}' | tr -d '%')"
UTILITY="$(grep -E '^\s+OVERALL' "$LOG_FILE" | awk '{print $3}' | tr -d '%')"
COMBINED="$(grep -E '^\s+OVERALL' "$LOG_FILE" | awk '{print $4}' | tr -d '%')"
GRADE="$(grep -E '^\s+OVERALL' "$LOG_FILE" | awk '{print $5}' | sed $'s/\033\\[[0-9;]*m//g')"

if [[ -z "$DEFENSE" ]]; then
  DEFENSE="0"
  UTILITY="0"
  COMBINED="0"
  GRADE="F"
fi

# 5. Parse valid_rate from server logs
# A "scope agent failed" error means the scope agent emitted an invalid fn.
SCOPE_FAILURES="$(grep -c 'Scope agent failed' "$SERVER_LOG" 2>/dev/null || echo 0)"
# Total query invocations = count of "Running query" or similar
TOTAL_QUERIES="$(grep -c 'run_query' "$SERVER_LOG" 2>/dev/null || echo 0)"
if [[ "$TOTAL_QUERIES" -eq 0 ]]; then
  # Fall back to parsing from bench log attack count
  TOTAL_QUERIES="$(grep -oE 'Total attacks: [0-9]+' "$LOG_FILE" | head -1 | awk '{print $3}')"
  TOTAL_QUERIES="${TOTAL_QUERIES:-0}"
fi
if [[ "$TOTAL_QUERIES" -gt 0 ]]; then
  VALID_RATE="$(awk -v f="$SCOPE_FAILURES" -v t="$TOTAL_QUERIES" 'BEGIN{printf "%.2f", 1 - (f/t)}')"
else
  VALID_RATE="0.00"
fi

# 6. Save the JSON result
LATEST_JSON="$REPO_ROOT/bench/results/gan-latest.json"
DEST_JSON="$EXPERIMENTS_DIR/${TS}-${LABEL}.json"
if [[ -f "$LATEST_JSON" ]]; then
  cp "$LATEST_JSON" "$DEST_JSON"
  echo "[autoresearch] JSON copied to $DEST_JSON"
fi

# 7. Git SHA
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
if ! git diff --quiet 2>/dev/null; then
  GIT_SHA="${GIT_SHA}-DIRTY"
fi

# 8. Append to results.tsv
# Note: use printf to get literal \t (TAB)
NOTES="rounds=${ROUNDS} combined=${COMBINED}% bench_exit=${BENCH_EXIT}"
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$TS" "$LABEL" "$GIT_SHA" "$VALID_RATE" \
  "$DEFENSE" "$UTILITY" "$GRADE" "$NOTES" \
  >> "$RESULTS_TSV"

echo ""
echo "[autoresearch] ─── RESULT ───────────────────────────────"
echo "[autoresearch]   label:       $LABEL"
echo "[autoresearch]   git:         $GIT_SHA"
echo "[autoresearch]   valid_rate:  $VALID_RATE"
echo "[autoresearch]   defense:     ${DEFENSE}%"
echo "[autoresearch]   utility:     ${UTILITY}%"
echo "[autoresearch]   combined:    ${COMBINED}%"
echo "[autoresearch]   grade:       $GRADE"
echo "[autoresearch]   scope fails: $SCOPE_FAILURES / $TOTAL_QUERIES"
echo "[autoresearch] ─────────────────────────────────────────"

if [[ "$VALID_RATE" != "1.00" ]]; then
  echo "[autoresearch] WARNING: valid_rate < 100%. This experiment does not"
  echo "[autoresearch]          qualify as a success regardless of grade."
  echo "[autoresearch]          Check $SERVER_LOG for scope agent errors."
fi

exit "$BENCH_EXIT"

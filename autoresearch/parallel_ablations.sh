#!/bin/bash
# Parallel ablation runner — STRICTLY ADDITIVE.
#
# Design:
#   - Does NOT kill or touch any existing hivemind.server processes.
#     Existing runners on port 8100 (iter36-no-simulate) keep running.
#   - Each experiment gets a dedicated port (8101, 8102, 8103, ...).
#   - Each port gets a fresh server with its own env toggles + trace dir.
#   - All experiments write to autoresearch/results.tsv — sortable by
#     timestamp; no cross-experiment state.
#   - Pre-flight check dumps what's currently running so you can see
#     the existing state before this script adds to it.
#
# Usage on t3.large server:
#   cd /home/ubuntu/hivemind-core
#   bash autoresearch/parallel_ablations.sh preflight      # just dump state
#   bash autoresearch/parallel_ablations.sh launch iter37  # start iter37 only
#   bash autoresearch/parallel_ablations.sh launch iter42  # start iter42 only
#   bash autoresearch/parallel_ablations.sh launch iter43  # start iter43 only
#   bash autoresearch/parallel_ablations.sh launch-all     # parallel all three
#
# To add a new experiment: append to the EXPERIMENTS table below.

set -u

LOGDIR=/tmp/overnight
REPO=/home/ubuntu/hivemind-core
cd "$REPO"

mkdir -p "$LOGDIR" /tmp/traces

log() { echo "[parallel $(date -u +%FT%TZ)] $*" | tee -a "$LOGDIR/parallel.log"; }

api_key=$(grep ^HIVEMIND_API_KEY "$REPO/.env" | cut -d= -f2)

# ─────────────────────────────────────────────────────────────────────
# EXPERIMENT REGISTRY
# Each row: name|port|model|extra_env|scope_agent|needs_scope_image
# - needs_scope_image=yes → build scope-claw image before launching
# Bump agent_timeout to 900s for the simulate-heavy iter43 — the new
# prompt budgets 2-5 simulate calls (~60s each) plus 14-turn budget,
# which can exceed the default 300s.
# ─────────────────────────────────────────────────────────────────────
declare -A EXP_PORT
declare -A EXP_MODEL
declare -A EXP_ENV
declare -A EXP_SCOPE
declare -A EXP_NEEDS_IMG

register_exp() {
    local name="$1"; local port="$2"; local model="$3"
    local extra_env="$4"; local scope_agent="$5"; local needs_img="$6"
    EXP_PORT[$name]="$port"
    EXP_MODEL[$name]="$model"
    EXP_ENV[$name]="$extra_env"
    EXP_SCOPE[$name]="$scope_agent"
    EXP_NEEDS_IMG[$name]="$needs_img"
}

# iter37: isolate sem-lift contribution on Haiku.
register_exp "iter37-no-semlift-parallel" 8101 \
    "anthropic/claude-haiku-4.5" \
    "HIVEMIND_DISABLE_SEMLIFT=true" "" "no"

# iter42: whole-agent claw-code runtime on Kimi.
register_exp "iter42-kimi-scope-claw-parallel" 8102 \
    "moonshotai/kimi-k2" \
    "" "default-scope-claw" "yes"

# iter43: new simulate-as-frontier-search prompt in a SEPARATE scope
# agent image (default-scope-sim) so the default-scope image used by
# iter36/iter37 is untouched. Haiku baseline to compare against iter29
# (same model, old prompt). Needs longer agent_timeout because iterative
# simulate can take 7–10 minutes.
register_exp "iter43-simulate-frontier-haiku" 8103 \
    "anthropic/claude-haiku-4.5" \
    "HIVEMIND_AGENT_TIMEOUT=900" "default-scope-sim" "sim"


# iter45: concurrent simulate_multi — scope can run 2-3 scope_fn candidates
# in parallel and pick the best. Tests whether splitting strategy across
# row-exclusion/value-redaction/aggregation candidates beats a single
# committed strategy. Uses default-scope image (tool gated on env flag).
register_exp "iter45-simulate-multi-haiku" 8104 \
    "anthropic/claude-haiku-4.5" \
    "HIVEMIND_SCOPE_MULTI=true HIVEMIND_AGENT_TIMEOUT=900" "" "no"

# iter59: contextual-integrity workflow. Scope treats the query agent as the
# recipient, reads /workspace/query-agent/ (static), declares residual
# behavioral uncertainty, and runs simulate_multi on 2-3 candidates (dynamic)
# before picking + emitting. Workflow rebuild of default-scope image needed —
# the prompt is baked in when HIVEMIND_SCOPE_CI=true triggers the injection.
# Expects scope to exhibit: Read on query-agent source + simulate_multi call.
# Runs against the existing 6-scenario bench on Haiku for comparability with
# iter29/iter45 baselines.
register_exp "iter59-ci-workflow-haiku" 8119 \
    "anthropic/claude-haiku-4.5" \
    "HIVEMIND_SCOPE_CI=true HIVEMIND_AGENT_TIMEOUT=900" "" "no"

# iter60: CI-phrased scenario policies (NO scope prompt change). Isolates the
# lever — does the effect seen in iter59 come from the scope prompt injection,
# or from the policy wording? This run keeps the default-scope prompt but
# rewords each of the 6 baseline policies in CI/behavioral terms (denial-as-leak
# made explicit). Default scope image, same Haiku model, same 6 scenarios.
# HIVEMIND_BENCH_CI_POLICIES must reach the bench process — run_bench forwards
# HIVEMIND_BENCH_* vars explicitly.
register_exp "iter60-ci-policies-haiku" 8120 \
    "anthropic/claude-haiku-4.5" \
    "HIVEMIND_BENCH_CI_POLICIES=true HIVEMIND_AGENT_TIMEOUT=900" "" "no"

# ─────────────────────────────────────────────────────────────────────
# OPERATIONS
# ─────────────────────────────────────────────────────────────────────

preflight() {
    log "=== PREFLIGHT — current server state ==="
    log "hivemind.server processes:"
    pgrep -af 'hivemind.server' | tee -a "$LOGDIR/parallel.log" || log "  (none)"
    log "ports in use by hivemind (8100-8199):"
    for p in 8100 8101 8102 8103 8104 8105; do
        local pid=$(lsof -ti :"$p" 2>/dev/null || true)
        if [ -n "$pid" ]; then
            log "  :$p  pid=$pid  health=$(curl -sS --max-time 2 http://localhost:$p/v1/health 2>/dev/null | head -c 80 || echo DOWN)"
        fi
    done
    log "latest 5 runs in results.tsv:"
    tail -5 "$REPO/autoresearch/results.tsv" | tee -a "$LOGDIR/parallel.log"
    log "=== END PREFLIGHT ==="
}

ensure_scope_claw_image() {
    if docker images --format '{{.Repository}}' | grep -q 'hivemind-default-scope-claw'; then
        log "scope-claw image already present"
        return 0
    fi
    if [ ! -x "$HOME/claw-code/rust/target/release/claw" ]; then
        log "ERROR: claw binary not found at \$HOME/claw-code/rust/target/release/claw"
        return 1
    fi
    log "building scope-claw image"
    bash agents/default-scope-claw/build_prep.sh > "$LOGDIR/scope_claw_prep.log" 2>&1 || true
    docker build -t hivemind-default-scope-claw:local \
        -f agents/default-scope-claw/Dockerfile \
        agents/default-scope-claw \
        > "$LOGDIR/scope_claw_build.log" 2>&1
    if docker images --format '{{.Repository}}' | grep -q 'hivemind-default-scope-claw'; then
        log "scope-claw build ok"
        return 0
    fi
    log "scope-claw build FAILED. tail:"
    tail -30 "$LOGDIR/scope_claw_build.log" | tee -a "$LOGDIR/parallel.log"
    return 1
}

ensure_scope_sim_image() {
    # Builds the simulate-as-frontier-search scope variant. This image is
    # independent of the default-scope image — iter36/iter37 continue to
    # use the old prompt; only iter43 uses this one.
    log "building default-scope-sim image (forces fresh for prompt updates)"
    docker build -t hivemind-default-scope-sim:local \
        -f agents/default-scope-sim/Dockerfile \
        agents/default-scope-sim \
        > "$LOGDIR/scope_sim_build.log" 2>&1
    if docker images --format '{{.Repository}}' | grep -q 'hivemind-default-scope-sim'; then
        log "scope-sim build ok"
        return 0
    fi
    log "scope-sim build FAILED. tail:"
    tail -30 "$LOGDIR/scope_sim_build.log" | tee -a "$LOGDIR/parallel.log"
    return 1
}

launch_server() {
    local port="$1"; local name="$2"; local extra_env="$3"; local model="$4"
    local scope_agent="$5"

    if [ "$port" = "8100" ]; then
        log "REFUSING to touch port 8100 — reserved for existing runners."
        return 1
    fi

    local existing=$(lsof -ti :"$port" 2>/dev/null || true)
    if [ -n "$existing" ]; then
        local health=$(curl -sS --max-time 2 "http://localhost:$port/v1/health" 2>/dev/null || echo "DOWN")
        log "$name port $port already in use (pid=$existing health=$health) — ABORTING this experiment"
        return 1
    fi

    local trace_dir="/tmp/traces/$name"
    mkdir -p "$trace_dir"

    local env_line="HIVEMIND_PORT=$port HIVEMIND_LLM_MODEL=$model HIVEMIND_TRACE_DIR=$trace_dir PYTHONUNBUFFERED=1"
    if [ -n "$extra_env" ]; then env_line="$env_line $extra_env"; fi

    # If a non-default scope agent is requested, wire it through the
    # autoload settings so the server registers agent_id=$scope_agent
    # pointing at the matching image. No HTTP register step needed —
    # bench just passes --scope-agent $scope_agent and the lookup hits.
    if [ -n "$scope_agent" ]; then
        local img="hivemind-$scope_agent:local"
        env_line="$env_line HIVEMIND_DEFAULT_SCOPE_AGENT=$scope_agent HIVEMIND_DEFAULT_SCOPE_IMAGE=$img"
        log "$name autoload scope: id=$scope_agent image=$img"
    fi

    log "$name starting server :$port model=$model extras=${extra_env:-none}"
    eval "$env_line nohup .venv/bin/python -m hivemind.server \
        > $LOGDIR/${name}_server.log 2>&1 &"
    local pid=$!
    echo "$pid" > "$LOGDIR/${name}.pid"

    for i in $(seq 1 25); do
        sleep 2
        if curl -sS --max-time 3 "http://localhost:$port/v1/health" 2>/dev/null | grep -q '"ok"'; then
            log "$name healthy on :$port (pid=$pid, ${i} checks)"
            return 0
        fi
    done
    log "$name FAILED health check on :$port (pid=$pid). tail:"
    tail -30 "$LOGDIR/${name}_server.log" | tee -a "$LOGDIR/parallel.log"
    return 1
}

run_bench() {
    local port="$1"; local name="$2"; local model="$3"; local scope_agent="$4"
    local extra_env="$5"

    local bench_args="--url http://localhost:$port --rounds 1"
    if [ -n "$scope_agent" ]; then
        # No HTTP register — autoload registered this agent_id at server
        # startup via HIVEMIND_DEFAULT_SCOPE_AGENT / HIVEMIND_DEFAULT_SCOPE_IMAGE.
        bench_args="$bench_args --scope-agent $scope_agent"
    fi

    # Forward HIVEMIND_BENCH_* env vars to the bench process so flags like
    # HIVEMIND_BENCH_CI_POLICIES (which reshape scenarios.py at import time)
    # take effect. Server-only env vars are silently ignored by bench.
    local bench_env=""
    if [ -n "$extra_env" ]; then
        for kv in $extra_env; do
            case "$kv" in
                HIVEMIND_BENCH_*)
                    bench_env="$bench_env $kv"
                    ;;
            esac
        done
    fi

    log "$name launching bench: $bench_args env:${bench_env:-none}"
    eval "PYTHONUNBUFFERED=1$bench_env .venv/bin/python -u -m bench.cli run $bench_args \
        > $LOGDIR/${name}_bench.log 2>&1"
    local rc=$?
    log "$name bench exit=$rc"

    local overall=$(grep "^  OVERALL" "$LOGDIR/${name}_bench.log" | tail -1 | tr -s ' ' | cut -d' ' -f3-)
    log "$name overall=$overall"

    local git_sha=$(git rev-parse --short HEAD)
    local def=$(echo "$overall" | awk '{print $1}' | tr -d '%' | sed 's/[^0-9]//g')
    local util=$(echo "$overall" | awk '{print $2}' | tr -d '%' | sed 's/[^0-9]//g')
    local grade=$(echo "$overall" | awk '{print $4}' | sed 's/\x1b\[[0-9;]*m//g' | tr -d ' ')
    local ts=$(date -u +%FT%TZ)
    printf "%s\t%s\t%s\t1.00\t%s\t%s\t%s\toverall=%s on %s scope=%s port=%s\n" \
        "$ts" "$name" "$git_sha" "${def:-0}" "${util:-0}" "${grade:-F}" \
        "$overall" "$model" "${scope_agent:-default-scope}" "$port" \
        >> autoresearch/results.tsv
    git add autoresearch/results.tsv
    git -c user.email=overnight@local -c user.name="parallel runner" \
        commit -m "parallel: $name overall=$overall" > /dev/null 2>&1 || true
}

kill_server() {
    local name="$1"
    if [ -f "$LOGDIR/${name}.pid" ]; then
        local pid=$(cat "$LOGDIR/${name}.pid")
        log "$name killing server pid=$pid"
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$LOGDIR/${name}.pid"
    fi
}

run_experiment() {
    local name="$1"
    local port="${EXP_PORT[$name]:-}"
    if [ -z "$port" ]; then
        log "UNKNOWN experiment: $name"
        return 1
    fi
    local model="${EXP_MODEL[$name]}"
    local extra_env="${EXP_ENV[$name]}"
    local scope_agent="${EXP_SCOPE[$name]}"
    local needs_img="${EXP_NEEDS_IMG[$name]}"

    log "=== $name START port=$port model=$model scope=${scope_agent:-default} ==="
    case "$needs_img" in
        yes)
            if ! ensure_scope_claw_image; then
                log "$name ABORTED — scope-claw image unavailable"
                return 1
            fi
            ;;
        sim)
            if ! ensure_scope_sim_image; then
                log "$name ABORTED — scope-sim image unavailable"
                return 1
            fi
            ;;
        no|"")
            ;;
        *)
            log "$name UNKNOWN needs_img value: $needs_img"
            return 1
            ;;
    esac
    if launch_server "$port" "$name" "$extra_env" "$model" "$scope_agent"; then
        run_bench "$port" "$name" "$model" "$scope_agent" "$extra_env"
    else
        log "$name SERVER_LAUNCH_FAILED — skipping bench"
    fi
    kill_server "$name"
    log "=== $name DONE ==="
}

# ─────────────────────────────────────────────────────────────────────
# DISPATCH
# ─────────────────────────────────────────────────────────────────────

cmd="${1:-preflight}"
case "$cmd" in
    preflight)
        preflight
        ;;
    launch)
        name="${2:-}"
        if [ -z "$name" ]; then
            log "usage: $0 launch <experiment-name>"
            log "available: ${!EXP_PORT[*]}"
            exit 1
        fi
        preflight
        run_experiment "$name"
        ;;
    launch-all)
        preflight
        pids=()
        for name in "${!EXP_PORT[@]}"; do
            run_experiment "$name" &
            pids+=($!)
            log "launched $name as bg pid=$!"
            sleep 3  # slight stagger so the first dock pull doesn't race
        done
        for pid in "${pids[@]}"; do
            wait "$pid" || true
        done
        log "=== launch-all complete ==="
        ;;
    list)
        log "registered experiments:"
        for name in "${!EXP_PORT[@]}"; do
            log "  $name  port=${EXP_PORT[$name]}  model=${EXP_MODEL[$name]}  scope=${EXP_SCOPE[$name]:-default}  env=${EXP_ENV[$name]:-none}"
        done
        ;;
    *)
        echo "usage: $0 {preflight|launch <name>|launch-all|list}"
        exit 1
        ;;
esac

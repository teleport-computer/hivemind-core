#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'EOF'
[autoresearch] parallel_ablations.sh is archived.

The old ablation runner drove the retired GAN-style benchmark preserved under
autoresearch/legacy_bench/. It is intentionally disabled so new work does not
optimize against the stale LLM-judge loop.

Use eval/ for current room-agent latency, failure-rate, and leakage evals.
EOF

exit 2

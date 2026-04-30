#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'EOF'
[autoresearch] run_experiment.sh is archived.

The old GAN-style benchmark is preserved under autoresearch/legacy_bench/,
but it targets removed APIs and used an LLM judge. Do not use it to optimize
current room agents.

Use eval/ for the current room-native harness.
EOF

exit 2

#!/bin/bash
# Assemble the scope-claw Docker build context by copying in vendored
# files from the rest of the repo. Run this before `docker build`.
#
# Usage: ./build_prep.sh [CLAW_BIN_PATH]
set -euo pipefail

CLAW_BIN="${1:-$HOME/claw-code/rust/target/release/claw}"

if [ ! -x "$CLAW_BIN" ]; then
    echo "error: claw binary not found at $CLAW_BIN" >&2
    exit 1
fi

cd "$(dirname "$0")"

# claw binary into build context
cp "$CLAW_BIN" ./claw

# stdio MCP proxy
cp ../../hivemind/mcp_stdio_proxy.py ./mcp_stdio_proxy.py

# vendor the default-scope source for SYSTEM_PROMPT extraction
cp ../default-scope/agent.py ./default-scope-agent.py

ls -lh claw mcp_stdio_proxy.py default-scope-agent.py

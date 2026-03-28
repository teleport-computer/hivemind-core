#!/bin/bash
set -euo pipefail

# query-boot.sh — Boot script for ephemeral query agent CVMs
#
# The base image (hivemind-agent-sdk-base) has all dependencies pre-installed.
# This script decodes agent source code from AGENT_SOURCE_B64 env var,
# extracts it to /app, and runs the agent entrypoint.
#
# Environment:
#   AGENT_SOURCE_B64  — base64-encoded tar.gz of agent source files
#   AGENT_ENTRYPOINT  — command to run (default: "python agent.py")

cd /app

# Decode and extract source files
if [ -z "${AGENT_SOURCE_B64:-}" ]; then
    echo "[boot] FATAL: AGENT_SOURCE_B64 not set"
    exit 1
fi

echo "[boot] Extracting agent source..."
echo "$AGENT_SOURCE_B64" | base64 -d | tar xz

echo "[boot] Files:"
ls -la

# Run the agent
ENTRYPOINT="${AGENT_ENTRYPOINT:-python agent.py}"
echo "[boot] Running: $ENTRYPOINT"
exec $ENTRYPOINT

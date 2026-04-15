#!/usr/bin/env bash
# Cross-repo scope agent evaluation.
#
# Runs the agent-test eval harness against the scope-prompt.md from this repo.
# Requires: ../hivemind-core-agent-test/ to exist as a sibling directory.
#
# Usage:
#   ./scripts/eval-scope.sh                          # default: sdk agent, 3 trials
#   ./scripts/eval-scope.sh --trials 5               # more trials
#   ./scripts/eval-scope.sh --adversarial             # adversarial red team
#   ./scripts/eval-scope.sh --scenarios pii_redaction # specific scenario
#   RULES="Only allow aggregates" ./scripts/eval-scope.sh  # fuse custom rules
#
# Environment:
#   ANTHROPIC_API_KEY — required
#   RULES            — optional English rules to fuse into {scenario_description}
#                      (if unset, the placeholder is left for the eval harness)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AGENT_TEST_DIR="$(cd "$REPO_ROOT/../hivemind-core-agent-test" 2>/dev/null && pwd)" || true

if [ -z "$AGENT_TEST_DIR" ] || [ ! -f "$AGENT_TEST_DIR/run_eval.py" ]; then
    echo "Error: Cannot find ../hivemind-core-agent-test/ (sibling directory)"
    echo "Expected: $REPO_ROOT/../hivemind-core-agent-test/run_eval.py"
    exit 1
fi

SCOPE_PROMPT="$REPO_ROOT/agents/default-scope/scope-prompt.md"
if [ ! -f "$SCOPE_PROMPT" ]; then
    echo "Error: Scope prompt not found: $SCOPE_PROMPT"
    exit 1
fi

# If RULES is set, fuse it into a temp copy of the prompt
PROMPT_TO_USE="$SCOPE_PROMPT"
if [ -n "${RULES:-}" ]; then
    TMPFILE="$(mktemp /tmp/scope-prompt-fused-XXXXXX.md)"
    trap "rm -f $TMPFILE" EXIT
    sed "s|{scenario_description}|$RULES|g" "$SCOPE_PROMPT" > "$TMPFILE"
    PROMPT_TO_USE="$TMPFILE"
    echo "Fused rules into prompt: $RULES"
fi

echo "Running scope eval..."
echo "  Prompt: $PROMPT_TO_USE"
echo "  Agent-test: $AGENT_TEST_DIR"
echo ""

cd "$AGENT_TEST_DIR"
exec uv run python run_eval.py --prompt "$PROMPT_TO_USE" "$@"

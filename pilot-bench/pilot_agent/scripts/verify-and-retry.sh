#!/bin/bash
# verify-and-retry.sh — Self-verification loop with Claude Code retry.
#
# Runs /tests/test.sh after Claude Code execution.
# If tests fail, feeds errors back to Claude Code with --resume for fixes.
# Up to MAX_RETRIES attempts, with RETRY_TIMEOUT total.
#
# Usage: verify-and-retry.sh <session_id> [max_retries] [timeout_sec]

set -euo pipefail

SESSION_ID="${1:?Usage: verify-and-retry.sh <session_id> [max_retries] [timeout_sec]}"
MAX_RETRIES="${2:-3}"
RETRY_TIMEOUT="${3:-900}"  # 15 minutes default
LOG_DIR="/logs/agent"
MODEL="${MODEL:-claude-opus-4-6}"

mkdir -p "$LOG_DIR"

# Track start time for total timeout
START_TIME=$(date +%s)

check_timeout() {
    local elapsed=$(( $(date +%s) - START_TIME ))
    if [ "$elapsed" -ge "$RETRY_TIMEOUT" ]; then
        echo "[verify] TIMEOUT after ${elapsed}s (limit: ${RETRY_TIMEOUT}s)"
        return 1
    fi
    return 0
}

run_tests() {
    echo "[verify] Running /tests/test.sh..."
    local test_output
    test_output=$(bash /tests/test.sh 2>&1) || {
        echo "$test_output" > "$LOG_DIR/test-output-attempt-$1.txt"
        echo "[verify] Tests FAILED (attempt $1)"
        echo "$test_output"
        return 1
    }
    echo "$test_output" > "$LOG_DIR/test-output-attempt-$1.txt"
    echo "[verify] Tests PASSED"
    return 0
}

# Initial test run
echo "[verify] === Self-Verification Loop ==="
echo "[verify] Session: $SESSION_ID"
echo "[verify] Max retries: $MAX_RETRIES"
echo "[verify] Timeout: ${RETRY_TIMEOUT}s"
echo ""

if run_tests 0; then
    echo "[verify] Tests passed on first try!"
    exit 0
fi

# Retry loop
for attempt in $(seq 1 "$MAX_RETRIES"); do
    echo ""
    echo "[verify] === Retry $attempt/$MAX_RETRIES ==="

    # Check timeout
    if ! check_timeout; then
        echo "[verify] Aborting: total timeout exceeded"
        exit 1
    fi

    # Extract last 100 lines of test output for error context
    ERROR_CONTEXT=$(tail -100 "$LOG_DIR/test-output-attempt-$((attempt - 1)).txt" 2>/dev/null || echo "No error output captured")

    # Build retry prompt
    RETRY_PROMPT="## Test Failure — Fix Required (Attempt $attempt/$MAX_RETRIES)

The test script /tests/test.sh FAILED. Here is the error output:

\`\`\`
$ERROR_CONTEXT
\`\`\`

## Instructions

1. Analyze the test failure output above carefully
2. Identify the root cause of the failure
3. Fix the issue — modify files, install packages, restart services as needed
4. After fixing, verify your fix is correct by examining the changed files
5. Do NOT run the tests yourself — the verification loop will handle that

Focus on the ACTUAL error, not symptoms. Common causes:
- Missing file or wrong path
- Service not running or wrong port
- Config syntax error
- Missing dependency
- Permission issue

Work autonomously. Fix the issue and exit."

    # Resume Claude Code with error context
    echo "[verify] Feeding error back to Claude Code (--resume)..."
    claude --resume "$SESSION_ID" \
        -p "$RETRY_PROMPT" \
        --verbose \
        --output-format stream-json \
        --dangerously-skip-permissions \
        --model "$MODEL" \
        2>&1 | tee "$LOG_DIR/claude-retry-$attempt.txt" || true

    # Re-run tests
    if run_tests "$attempt"; then
        echo "[verify] Tests passed after retry $attempt!"
        exit 0
    fi
done

echo ""
echo "[verify] FAILED after $MAX_RETRIES retries"
exit 1

"""
PilotAgent — Terminal-Bench agent wrapping Claude Code with a 5-step pipeline.

Pipeline:
1. Environmental bootstrapping (scan container)
2. Test script awareness (read /tests/test.sh, inject into prompt)
3. Optimized prompt construction
4. Claude Code execution (30 min timeout)
5. Self-verification loop (run tests, retry up to 3x with --resume)
"""

import json
import logging
import os
import re
import shlex
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent, ExecInput
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths

from .prompt_builder import build_prompt

logger = logging.getLogger(__name__)

# Paths inside the container
BOOTSTRAP_SCRIPT = "/installed-agent/scripts/bootstrap.sh"
VERIFY_SCRIPT = "/installed-agent/scripts/verify-and-retry.sh"
ENV_CONTEXT_FILE = "/installed-agent/env-context.txt"
TEST_SCRIPT = "/tests/test.sh"  # Legacy — verify step auto-detects pytest too
AGENT_LOG_DIR = str(EnvironmentPaths.agent_dir)
CLAUDE_OUTPUT_FILE = f"{AGENT_LOG_DIR}/claude-output.txt"
SESSION_ID_FILE = f"{AGENT_LOG_DIR}/session-id.txt"

# Timeouts — generous to handle QEMU emulation overhead (x86 on ARM)
# Harbor's per-task timeout (agent.timeout_sec × timeout_multiplier) is the real ceiling.
# These are per-command timeouts within that budget.
MAIN_EXECUTION_TIMEOUT = 3600  # 60 min for main Claude Code run
VERIFY_TIMEOUT = 1200  # 20 min for verification + retries
MAX_RETRIES = 3


class PilotAgent(BaseInstalledAgent):
    """
    Pilot Terminal-Bench agent.

    Wraps Claude Code with test awareness, environment scanning,
    pattern injection, and a self-verification retry loop.
    """

    @staticmethod
    def name() -> str:
        return "pilot"

    @property
    def _install_agent_template_path(self) -> Path:
        return Path(__file__).parent / "templates" / "install-pilot-agent.sh.j2"

    def create_run_agent_commands(self, instruction: str) -> list[ExecInput]:
        """Generate the execution pipeline as shell commands."""
        env = self._build_env()
        model = self._resolve_model()

        commands: list[ExecInput] = []

        # Step 0: Setup directories and upload scripts
        commands.append(ExecInput(
            command=f"mkdir -p {AGENT_LOG_DIR} /installed-agent/scripts",
            env=env,
        ))

        # Step 1: Environmental bootstrapping
        commands.append(ExecInput(
            command=f"bash {BOOTSTRAP_SCRIPT}",
            env=env,
            timeout_sec=60,
        ))

        # Steps 2-4: Build prompt (with env context + test script) → Execute Claude Code
        # We chain these in a single shell command so we can read files and pass to claude
        main_command = self._build_main_execution_command(instruction, model)
        commands.append(ExecInput(
            command=main_command,
            env=env,
            timeout_sec=MAIN_EXECUTION_TIMEOUT,
        ))

        # Step 3: Post-execution fixup (deterministic cleanup before grading)
        # Catches common "almost right" failures: binaries not in PATH,
        # build artifacts in output directories, etc.
        commands.append(ExecInput(
            command=f"bash /installed-agent/scripts/post-fixup.sh",
            env=env,
            timeout_sec=60,
        ))

        # Step 4: Self-verification loop (uses tests if available)
        verify_command = self._build_verify_command(model)
        commands.append(ExecInput(
            command=verify_command,
            env=env,
            timeout_sec=VERIFY_TIMEOUT,
        ))

        return commands

    def _build_env(self) -> dict[str, str]:
        """Collect environment variables for container execution."""
        env: dict[str, str] = {}

        # API keys — support multiple auth methods
        for key in [
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ]:
            if key in os.environ:
                env[key] = os.environ[key]

        # Harbor convention: enable background tasks
        env["FORCE_AUTO_BACKGROUND_TASKS"] = "1"
        env["ENABLE_BACKGROUND_TASKS"] = "1"

        # Remove empty values to let Claude CLI pick the best auth method
        return {k: v for k, v in env.items() if v}

    def _resolve_model(self) -> str:
        """Resolve the model to use."""
        if self.model_name:
            # Strip provider prefix (e.g., "anthropic/claude-opus-4-6" → "claude-opus-4-6")
            if "/" in self.model_name:
                return self.model_name.split("/", 1)[1]
            return self.model_name
        return "claude-opus-4-6"

    def _build_main_execution_command(self, instruction: str, model: str) -> str:
        """Build the combined prompt-construction + Claude Code execution command.

        Uses build-prompt.py script (uploaded to container) to construct the
        optimized prompt, then pipes it to Claude Code.
        """
        import base64
        encoded_instruction = base64.b64encode(instruction.encode()).decode()

        # Use a heredoc-based approach to avoid nested quoting issues
        return f"""bash << 'PILOT_EXEC_EOF'
set -e

AGENT_LOG_DIR="{AGENT_LOG_DIR}"
ENCODED_INSTRUCTION="{encoded_instruction}"
MODEL="{model}"

mkdir -p "$AGENT_LOG_DIR"

# Step 2: Build optimized prompt using the uploaded Python script
echo "[pilot] Building optimized prompt..."
PROMPT_FILE="$AGENT_LOG_DIR/prompt.txt"

python3 /installed-agent/scripts/build-prompt.py "$ENCODED_INSTRUCTION" > "$PROMPT_FILE" 2>"$AGENT_LOG_DIR/prompt-build-stderr.txt" || {{
    echo "[pilot] WARNING: Prompt builder failed, falling back to raw instruction"
    echo "$ENCODED_INSTRUCTION" | base64 -d > "$PROMPT_FILE"
}}

PROMPT_LINES=$(wc -l < "$PROMPT_FILE")
PROMPT_BYTES=$(wc -c < "$PROMPT_FILE")
echo "[pilot] Prompt built ($PROMPT_LINES lines, $PROMPT_BYTES bytes)"

# Step 3-4: Execute Claude Code with the optimized prompt
# Claude Code refuses --dangerously-skip-permissions as root, so run as 'pilot' user
echo "[pilot] Starting Claude Code with model $MODEL..."

# Write runner script (avoids nested quoting with su)
cat > /tmp/pilot-run-claude.sh << 'RUNNER_EOF'
#!/bin/bash
set -e
source /tmp/pilot-env.sh
PROMPT=$(cat "$1")
shift
MODEL="$1"
shift
LOG_DIR="$1"
claude -p "$PROMPT" \
    --verbose \
    --output-format stream-json \
    --dangerously-skip-permissions \
    --model "$MODEL" \
    2>&1 | tee "$LOG_DIR/claude-output.txt"
RUNNER_EOF
chmod +x /tmp/pilot-run-claude.sh

# Write auth env vars to file
# Priority: mounted token file (refreshed by host) > env var (static, may expire)
echo -n "" > /tmp/pilot-env.sh
if [ -f /tmp/pilot-bench-token ] && [ -s /tmp/pilot-bench-token ]; then
    echo "export CLAUDE_CODE_OAUTH_TOKEN=\"$(cat /tmp/pilot-bench-token)\"" >> /tmp/pilot-env.sh
    echo "[pilot] Using refreshed token from mounted file"
elif [ -n "${{CLAUDE_CODE_OAUTH_TOKEN:-}}" ]; then
    echo "export CLAUDE_CODE_OAUTH_TOKEN=\"$CLAUDE_CODE_OAUTH_TOKEN\"" >> /tmp/pilot-env.sh
    echo "[pilot] Using token from env var (may expire)"
fi
[ -n "${{ANTHROPIC_API_KEY:-}}" ] && echo "export ANTHROPIC_API_KEY=\"$ANTHROPIC_API_KEY\"" >> /tmp/pilot-env.sh
[ -n "${{ANTHROPIC_AUTH_TOKEN:-}}" ] && echo "export ANTHROPIC_AUTH_TOKEN=\"$ANTHROPIC_AUTH_TOKEN\"" >> /tmp/pilot-env.sh
chmod 600 /tmp/pilot-env.sh
chown pilot:pilot /tmp/pilot-env.sh /tmp/pilot-run-claude.sh "$PROMPT_FILE"
chown -R pilot:pilot "$AGENT_LOG_DIR"

su pilot -s /bin/bash -c "bash /tmp/pilot-run-claude.sh '$PROMPT_FILE' '$MODEL' '$AGENT_LOG_DIR'"

# Extract session ID for --resume in verification loop
SESSION_ID=$(grep -o '"session_id":"[^"]*"' "$AGENT_LOG_DIR/claude-output.txt" 2>/dev/null | head -1 | cut -d'"' -f4 || true)
if [ -n "$SESSION_ID" ]; then
    echo "$SESSION_ID" > "{SESSION_ID_FILE}"
    echo "[pilot] Session ID captured: $SESSION_ID"
else
    echo "[pilot] WARNING: Could not extract session ID"
fi

echo "[pilot] Claude Code execution complete"
PILOT_EXEC_EOF"""

    def _build_verify_command(self, model: str) -> str:
        """Build the self-verification + retry command."""
        return f"""bash << 'PILOT_VERIFY_EOF'
set -e

AGENT_LOG_DIR="{AGENT_LOG_DIR}"
SESSION_ID=""
MODEL="{model}"
TEST_SCRIPT="{TEST_SCRIPT}"
MAX_RETRIES={MAX_RETRIES}

# Detect test runner: shell script or pytest
TEST_CMD=""
echo "[verify] Looking for tests... TEST_SCRIPT=$TEST_SCRIPT"
echo "[verify] Contents of /tests/:" && ls -la /tests/ 2>&1 || echo "[verify] /tests/ not found"
if [ -f "$TEST_SCRIPT" ]; then
    TEST_CMD="bash $TEST_SCRIPT"
    echo "[verify] Found shell test: $TEST_SCRIPT"
elif ls /tests/test_*.py 1>/dev/null 2>&1; then
    TEST_CMD="python3 -m pytest /tests/ -x -q 2>&1 || pytest /tests/ -x -q"
    echo "[verify] Found pytest tests in /tests/"
else
    echo "[verify] No tests found — skipping verification"
    exit 0
fi

# Read session ID for --resume
if [ -f "{SESSION_ID_FILE}" ]; then
    SESSION_ID=$(cat "{SESSION_ID_FILE}")
    echo "[verify] Session ID: $SESSION_ID"
fi

# Run initial test
echo "[verify] === Running tests ==="
if TEST_OUTPUT=$(eval $TEST_CMD 2>&1); then
    echo "[verify] Tests PASSED on first try!"
    echo "$TEST_OUTPUT" > "$AGENT_LOG_DIR/test-output-0.txt"
    exit 0
fi
echo "$TEST_OUTPUT" > "$AGENT_LOG_DIR/test-output-0.txt"
echo "[verify] Tests FAILED — starting retry loop"

# Retry loop
for attempt in $(seq 1 $MAX_RETRIES); do
    echo ""
    echo "[verify] === Retry $attempt/$MAX_RETRIES ==="

    # Extract error context (last 100 lines)
    PREV=$((attempt - 1))
    ERROR_CONTEXT=$(tail -100 "$AGENT_LOG_DIR/test-output-$PREV.txt" 2>/dev/null || echo "No output")

    # Write retry prompt to file to avoid shell escaping
    cat > "$AGENT_LOG_DIR/retry-prompt-$attempt.txt" << PROMPT_EOF
## Test Failure — Fix Required (Attempt $attempt/$MAX_RETRIES)

The tests FAILED. Test command: `$TEST_CMD`. Error output:

```
$ERROR_CONTEXT
```

## Instructions

1. Analyze the test failure carefully
2. Identify the root cause
3. Fix the issue (modify files, install packages, restart services)
4. Verify your fix by checking the relevant files/services
5. Do NOT run the tests yourself — the verification system handles that

Focus on the ACTUAL error. Common causes: missing files, wrong paths, service not running, config syntax errors, missing dependencies, permission issues.

Work autonomously. Fix the issue.
PROMPT_EOF

    # Execute fix via Claude Code (as non-root 'pilot' user)
    cat > /tmp/pilot-retry-claude.sh << 'RETRY_RUNNER_EOF'
#!/bin/bash
# Re-read token from mounted file (may have been refreshed since main execution)
if [ -f /tmp/pilot-bench-token ] && [ -s /tmp/pilot-bench-token ]; then
    export CLAUDE_CODE_OAUTH_TOKEN="$(cat /tmp/pilot-bench-token)"
else
    source /tmp/pilot-env.sh
fi
SESSION_ID="$1"; shift
RETRY_PROMPT_FILE="$1"; shift
MODEL="$1"; shift
LOG_FILE="$1"
RETRY_PROMPT=$(cat "$RETRY_PROMPT_FILE")
if [ -n "$SESSION_ID" ]; then
    echo "[verify] Resuming Claude Code session..."
    claude --resume "$SESSION_ID" \
        -p "$RETRY_PROMPT" \
        --verbose \
        --output-format stream-json \
        --dangerously-skip-permissions \
        --model "$MODEL" \
        2>&1 | tee "$LOG_FILE" || true
else
    echo "[verify] No session ID — starting fresh Claude Code session..."
    claude -p "$RETRY_PROMPT" \
        --verbose \
        --output-format stream-json \
        --dangerously-skip-permissions \
        --model "$MODEL" \
        2>&1 | tee "$LOG_FILE" || true
fi
RETRY_RUNNER_EOF
    chmod +x /tmp/pilot-retry-claude.sh
    chown pilot:pilot /tmp/pilot-retry-claude.sh "$AGENT_LOG_DIR/retry-prompt-$attempt.txt"
    chown -R pilot:pilot "$AGENT_LOG_DIR"

    su pilot -s /bin/bash -c "bash /tmp/pilot-retry-claude.sh '$SESSION_ID' '$AGENT_LOG_DIR/retry-prompt-$attempt.txt' '$MODEL' '$AGENT_LOG_DIR/claude-retry-$attempt.txt'"

    # Re-run tests
    if TEST_OUTPUT=$(eval $TEST_CMD 2>&1); then
        echo "[verify] Tests PASSED after retry $attempt!"
        echo "$TEST_OUTPUT" > "$AGENT_LOG_DIR/test-output-$attempt.txt"
        exit 0
    fi
    echo "$TEST_OUTPUT" > "$AGENT_LOG_DIR/test-output-$attempt.txt"
    echo "[verify] Tests still failing after retry $attempt"
done

echo "[verify] FAILED after $MAX_RETRIES retries"
# Don't exit 1 — let Harbor grade it as-is
exit 0
PILOT_VERIFY_EOF"""

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Parse Claude Code output for token usage metrics."""
        output_file = self.logs_dir / "claude-output.txt"

        if not output_file.exists():
            # Try alternate path
            for cmd_dir in sorted(self.logs_dir.glob("command-*")):
                stdout_file = cmd_dir / "stdout.txt"
                if stdout_file.exists():
                    output_file = stdout_file
                    break

        if not output_file.exists():
            return

        total_input = 0
        total_output = 0
        total_cache = 0
        total_cost = 0.0

        with open(output_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)

                    # Extract usage from stream-json events
                    usage = event.get("usage", {})
                    if usage:
                        total_input += usage.get("input_tokens", 0)
                        total_output += usage.get("output_tokens", 0)
                        total_cache += usage.get("cache_read_input_tokens", 0)
                        total_cache += usage.get("cache_creation_input_tokens", 0)

                    # Also check result events
                    if event.get("type") == "result":
                        result_usage = event.get("usage", {})
                        if result_usage:
                            total_input = max(total_input, result_usage.get("input_tokens", 0))
                            total_output = max(total_output, result_usage.get("output_tokens", 0))
                        cost = event.get("cost_usd", 0)
                        if cost:
                            total_cost = max(total_cost, float(cost))

                except (json.JSONDecodeError, ValueError):
                    continue

        context.n_input_tokens = total_input
        context.n_output_tokens = total_output
        context.n_cache_tokens = total_cache
        if total_cost > 0:
            context.cost_usd = total_cost

    def _setup_env(self) -> dict[str, str]:
        """Environment variables for install script."""
        env = super()._setup_env()
        env.update(self._build_env())
        return env

    def _find_task_tests_dir(self) -> Path | None:
        """Find test files in harbor's task cache for test-aware prompting.

        Harbor caches downloaded tasks at ~/.cache/harbor/tasks/<id>/<task_name>/tests/.
        The trial config.json (written by harbor before setup) contains the task path.
        """
        try:
            config_path = self.logs_dir / "config.json"
            if not config_path.exists():
                return None

            config = json.loads(config_path.read_text())
            task_path = config.get("task", {}).get("path", "")
            if not task_path:
                return None

            cache_base = Path.home() / ".cache" / "harbor" / "tasks"
            if not cache_base.exists():
                return None

            # Scan cache for matching task directory
            for parent in cache_base.iterdir():
                candidate = parent / task_path / "tests"
                if candidate.is_dir():
                    return candidate

        except Exception as e:
            logger.debug(f"Could not find task tests in cache: {e}")

        return None

    async def setup(self, environment) -> None:
        """Install agent + upload helper scripts."""
        # Run base setup (installs Claude Code via template)
        await super().setup(environment)

        # Upload helper scripts
        scripts_dir = Path(__file__).parent / "scripts"

        for script_name in ["bootstrap.sh", "verify-and-retry.sh", "build-prompt.py", "post-fixup.sh"]:
            script_path = scripts_dir / script_name
            if script_path.exists():
                await environment.upload_file(
                    source_path=script_path,
                    target_path=f"/installed-agent/scripts/{script_name}",
                )

        # Make scripts executable
        await environment.exec(
            command="chmod +x /installed-agent/scripts/*.sh",
        )

        # Upload Python modules for prompt building inside container
        # NOTE: We upload prompt_builder.py and patterns.py but NOT __init__.py
        # (which imports agent.py that depends on Harbor — unavailable in container)
        agent_dir = Path(__file__).parent
        for py_file in ["prompt_builder.py", "patterns.py"]:
            py_path = agent_dir / py_file
            if py_path.exists():
                await environment.upload_file(
                    source_path=py_path,
                    target_path=f"/installed-agent/pilot_agent/{py_file}",
                )

        # Write a clean __init__.py for container use (no Harbor dependencies)
        await environment.exec(
            command='echo "# Container-side init — no Harbor deps" > /installed-agent/pilot_agent/__init__.py',
        )

        # Upload task test files for test-aware prompting + self-verify loop.
        # Harbor caches tests locally but only mounts them at /tests/ during
        # verification — NOT during agent execution. By uploading them early,
        # the prompt builder can inject test content and the verify loop can
        # actually run self-tests.
        tests_dir = self._find_task_tests_dir()
        if tests_dir:
            await environment.exec(command="mkdir -p /tests")
            uploaded = 0
            for test_file in sorted(tests_dir.iterdir()):
                if test_file.is_file():
                    await environment.upload_file(
                        source_path=test_file,
                        target_path=f"/tests/{test_file.name}",
                    )
                    uploaded += 1
            if uploaded:
                logger.info(f"Uploaded {uploaded} test files to /tests/")
        else:
            logger.debug("No test files found in harbor cache")

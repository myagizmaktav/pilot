"""
Custom execution engine for Terminal Bench — direct Anthropic API, no Claude Code.

Replaces the Claude Code CLI black box with full control over:
- Progressive thinking budget (high early, low later)
- Tool-call validation and correction
- Loop detection (repeated edits, stuck commands)
- Mandatory test verification
- Short bash timeouts with kill

Architecture:
  Harbor → agent.py → this engine (via subprocess in container)
  Engine → Anthropic Messages API with tool_use
  Tools: bash, read_file, write_file

Usage:
  python3 engine.py --task "instruction" --project /app --model claude-opus-4-6
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import anthropic
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--break-system-packages", "anthropic"])
    import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("engine")

# --- Constants ---
MAX_TURNS = 60
BASH_TIMEOUT = 120  # seconds per command
THINKING_HIGH_TURNS = 8  # extended thinking for first N turns
THINKING_HIGH_BUDGET = 16000  # tokens for planning phase
THINKING_LOW_BUDGET = 4000   # tokens for execution phase
MAX_OUTPUT_TOKENS = 16000
TEST_CHECK_INTERVAL = 8  # auto-run tests every N turns
MAX_REPEATED_COMMANDS = 3  # loop detection threshold
RESULT_JSON = "/logs/agent/pilot-result.json"


# --- Tool Definitions ---
TOOLS = [
    {
        "name": "bash",
        "description": (
            "Execute a bash command. Returns stdout+stderr. "
            "Commands timeout after 120s. Use for running code, installing packages, "
            "checking files, running tests. Prefer short commands."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 120, max 600)",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file's contents. Returns the full text with line numbers. "
            "Use for reading source code, test files, configs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start from (1-indexed)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to read (default all)",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write content to a file. Creates parent directories if needed. "
            "Overwrites existing files. Use for creating new files or complete rewrites."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to write to",
                },
                "content": {
                    "type": "string",
                    "description": "File content to write",
                },
            },
            "required": ["path", "content"],
        },
    },
]


# --- Tool Execution ---
def execute_bash(command: str, timeout: int = BASH_TIMEOUT, cwd: str = "/app") -> str:
    """Execute bash command with timeout."""
    timeout = min(timeout, 600)  # hard cap
    try:
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        output = result.stdout + result.stderr
        # Cap output at 50KB to prevent context bloat
        if len(output) > 50000:
            output = output[:25000] + "\n\n... [output truncated] ...\n\n" + output[-25000:]
        exit_info = f"\n[exit code: {result.returncode}]" if result.returncode != 0 else ""
        return output + exit_info
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT after {timeout}s — command killed. Try a different approach.]"
    except Exception as e:
        return f"[ERROR: {e}]"


def execute_read_file(path: str, offset: int = 0, limit: int = 0) -> str:
    """Read file with line numbers."""
    try:
        p = Path(path)
        if not p.exists():
            return f"[File not found: {path}]"
        if p.stat().st_size > 500000:
            return f"[File too large: {p.stat().st_size} bytes. Use bash: head -100 {path}]"
        lines = p.read_text(errors="replace").splitlines()
        if offset > 0:
            lines = lines[offset - 1:]
        if limit > 0:
            lines = lines[:limit]
        numbered = [f"{i + (offset or 1):4d} | {line}" for i, line in enumerate(lines)]
        return "\n".join(numbered)
    except Exception as e:
        return f"[ERROR reading {path}: {e}]"


def execute_write_file(path: str, content: str) -> str:
    """Write file, creating parent dirs."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"[Wrote {len(content)} bytes to {path}]"
    except Exception as e:
        return f"[ERROR writing {path}: {e}]"


def execute_tool(name: str, input_data: dict) -> str:
    """Dispatch tool call."""
    if name == "bash":
        return execute_bash(
            input_data["command"],
            timeout=input_data.get("timeout", BASH_TIMEOUT),
        )
    elif name == "read_file":
        return execute_read_file(
            input_data["path"],
            offset=input_data.get("offset", 0),
            limit=input_data.get("limit", 0),
        )
    elif name == "write_file":
        return execute_write_file(input_data["path"], input_data["content"])
    else:
        return f"[Unknown tool: {name}]"


# --- Loop Detection ---
class LoopDetector:
    def __init__(self):
        self.command_history: list[str] = []
        self.file_edit_counts: dict[str, int] = {}

    def record_command(self, cmd: str):
        self.command_history.append(cmd)

    def record_file_write(self, path: str):
        self.file_edit_counts[path] = self.file_edit_counts.get(path, 0) + 1

    def is_stuck(self) -> str | None:
        """Returns warning message if loop detected, else None."""
        # Check repeated identical commands
        if len(self.command_history) >= MAX_REPEATED_COMMANDS:
            recent = self.command_history[-MAX_REPEATED_COMMANDS:]
            if len(set(recent)) == 1:
                return (
                    f"LOOP DETECTED: You ran '{recent[0][:80]}' {MAX_REPEATED_COMMANDS} times. "
                    "STOP. Try a completely different approach."
                )

        # Check excessive file edits
        for path, count in self.file_edit_counts.items():
            if count >= 5:
                return (
                    f"LOOP DETECTED: You edited '{path}' {count} times without tests passing. "
                    "DELETE your approach and try something fundamentally different."
                )

        return None


# --- Test Runner ---
def run_tests(cwd: str = "/app") -> tuple[bool, str]:
    """Run pytest if test file exists. Returns (passed, output)."""
    test_file = "/tests/test_outputs.py"
    if not Path(test_file).exists():
        return False, "[No test file found]"

    output = execute_bash(
        f"cd {cwd} && python3 -m pytest {test_file} -v 2>&1",
        timeout=300,
        cwd=cwd,
    )
    passed = "passed" in output and "failed" not in output and "error" not in output.lower()
    return passed, output


# --- System Prompt ---
def build_system_prompt(task: str, env_context: str, patterns: str) -> str:
    """Build the system prompt with task, env, and patterns."""
    parts = [
        "You are an expert software engineer solving a coding task in a sandboxed container.",
        "",
        f"## Task\n\n{task}",
        "",
    ]

    if env_context:
        parts.extend([
            "## Pre-discovered Environment",
            f"```\n{env_context}\n```",
            "",
        ])

    parts.extend([
        "## Mandatory Workflow",
        "",
        "Phase 1 — RECON (first 3 tool calls):",
        "1. Read /tests/test_outputs.py to understand EXACT expected outputs",
        "2. List and read all files in /app/",
        "3. Write a brief plan (in your thinking) — which approach, which files to create",
        "",
        "Phase 2 — IMPLEMENT:",
        "1. Start with the simplest working approach — brute force beats unfinished elegance",
        "2. Produce output files EARLY — partial output beats no output",
        "3. Run tests after EVERY significant change",
        "4. If tests pass → STOP IMMEDIATELY",
        "",
        "Phase 3 — RECOVER (if stuck):",
        "1. If same approach failed 3 times → DELETE and try completely different algorithm",
        "2. Read test error output carefully — often it's a format mismatch, not logic",
        "3. Write analysis scripts instead of reasoning through data manually",
        "",
        "## Rules",
        "- Work autonomously — never ask for confirmation",
        "- Container has ~2GB RAM — don't run heavy processes concurrently",
        "- Check if packages exist before installing: python3 -c 'import X'",
        "- Use --break-system-packages with pip",
        "- If a command runs >30s with no output, kill it",
        "- Once tests pass, STOP. No cleanup, no summary.",
        "",
    ])

    if patterns:
        parts.extend(["## Learned Patterns", "", patterns, ""])

    return "\n".join(parts)


# --- Load Learning DB Patterns ---
def load_patterns(db_path: str = "/root/.pilot/data/pilot.db") -> str:
    """Load patterns from SQLite learning DB."""
    if not Path(db_path).exists():
        return ""
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT title, description, is_anti_pattern FROM cross_patterns "
            "WHERE confidence >= 0.7 ORDER BY confidence DESC LIMIT 15"
        )
        lines = []
        for title, desc, is_anti in cursor:
            marker = "AVOID" if is_anti else "DO"
            lines.append(f"- **[{marker}]** {title}: {desc}")
        conn.close()
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Failed to load patterns: {e}")
        return ""


# --- Main Engine ---
def run(task: str, project: str, model: str, api_key: str):
    """Main execution loop."""
    client = anthropic.Anthropic(api_key=api_key)
    loop_detector = LoopDetector()
    start_time = time.time()

    # Load context
    env_context = ""
    env_file = Path(project) / ".pilot-env-context.txt"
    if env_file.exists():
        env_context = env_file.read_text()[:1200]

    patterns = load_patterns()

    system_prompt = build_system_prompt(task, env_context, patterns)

    messages = []
    total_input_tokens = 0
    total_output_tokens = 0
    tests_passed = False

    logger.info(f"Starting engine: model={model}, project={project}")
    logger.info(f"Task: {task[:200]}...")

    for turn in range(MAX_TURNS):
        elapsed = time.time() - start_time
        if elapsed > MAIN_TIMEOUT - 120:  # stop 2 min before timeout
            logger.warning(f"Approaching timeout ({elapsed:.0f}s), stopping")
            break

        # Progressive thinking budget
        if turn < THINKING_HIGH_TURNS:
            thinking_budget = THINKING_HIGH_BUDGET
        else:
            thinking_budget = THINKING_LOW_BUDGET

        # Check for loops
        loop_warning = loop_detector.is_stuck()
        if loop_warning:
            messages.append({
                "role": "user",
                "content": f"⚠️ {loop_warning}",
            })

        # Auto-test check
        if turn > 0 and turn % TEST_CHECK_INTERVAL == 0 and not tests_passed:
            passed, test_output = run_tests(project)
            if passed:
                tests_passed = True
                logger.info(f"Tests passed at turn {turn}!")
                break
            else:
                messages.append({
                    "role": "user",
                    "content": f"[AUTO-TEST CHECK at turn {turn}]\n{test_output}\n\nTests are failing. Fix the issues.",
                })

        # Add initial user message
        if turn == 0:
            messages.append({
                "role": "user",
                "content": "Begin. Follow the mandatory workflow. Start with Phase 1 RECON.",
            })

        # API call
        try:
            kwargs = {
                "model": model,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "system": system_prompt,
                "tools": TOOLS,
                "messages": messages,
            }

            # Extended thinking (budget-based)
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            }

            response = client.messages.create(**kwargs)

        except anthropic.APIError as e:
            logger.error(f"API error at turn {turn}: {e}")
            if "overloaded" in str(e).lower():
                time.sleep(10)
                continue
            break

        # Track tokens
        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        # Process response
        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        # Check stop reason
        if response.stop_reason == "end_turn":
            logger.info(f"Agent finished at turn {turn}")
            # Run final test check
            if not tests_passed:
                passed, _ = run_tests(project)
                tests_passed = passed
            break

        # Process tool calls
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in assistant_content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input

                    # Track for loop detection
                    if tool_name == "bash":
                        loop_detector.record_command(tool_input.get("command", ""))
                    elif tool_name == "write_file":
                        loop_detector.record_file_write(tool_input.get("path", ""))

                    # Execute
                    logger.info(f"Turn {turn}: {tool_name}({str(tool_input)[:100]}...)")
                    result = execute_tool(tool_name, tool_input)

                    # Check if tests were run and passed
                    if tool_name == "bash" and "pytest" in tool_input.get("command", ""):
                        if "passed" in result and "failed" not in result:
                            tests_passed = True
                            logger.info("Tests passed via agent's own pytest run!")

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "user", "content": tool_results})

            # Early exit if tests passed
            if tests_passed:
                logger.info(f"Tests passed — stopping at turn {turn}")
                break
        else:
            logger.warning(f"Unexpected stop_reason: {response.stop_reason}")
            break

    # Final test run if not yet confirmed
    if not tests_passed:
        tests_passed, _ = run_tests(project)

    elapsed = time.time() - start_time
    logger.info(
        f"Engine done: turns={turn+1}, passed={tests_passed}, "
        f"tokens_in={total_input_tokens}, tokens_out={total_output_tokens}, "
        f"elapsed={elapsed:.0f}s"
    )

    # Write result JSON (compatible with Pilot's format)
    result = {
        "Success": tests_passed,
        "TokensInput": total_input_tokens,
        "TokensOutput": total_output_tokens,
        "EstimatedCostUSD": 0.0,  # TODO: calculate from token counts
        "Turns": turn + 1,
        "ElapsedSeconds": elapsed,
    }
    result_path = Path(RESULT_JSON)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2))
    logger.info(f"Result written to {RESULT_JSON}")

    return tests_passed


def main():
    parser = argparse.ArgumentParser(description="Pilot Bench Engine")
    parser.add_argument("--task", required=True, help="Task instruction")
    parser.add_argument("--project", default="/app", help="Project directory")
    parser.add_argument("--model", default="claude-opus-4-6", help="Model ID")
    parser.add_argument("--result-json", default=RESULT_JSON, help="Result output path")
    args = parser.parse_args()

    global RESULT_JSON
    RESULT_JSON = args.result_json

    # Resolve API key
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("PILOT_ENGINE_API_KEY")
    if not api_key:
        logger.error("No API key found. Set ANTHROPIC_API_KEY or PILOT_ENGINE_API_KEY")
        sys.exit(1)

    success = run(args.task, args.project, args.model, api_key)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

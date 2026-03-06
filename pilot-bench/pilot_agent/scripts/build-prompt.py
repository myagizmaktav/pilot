#!/usr/bin/env python3
"""
Build the optimized prompt for Claude Code execution.

Reads environment context and test script, combines with task instruction
using the prompt builder module. Outputs the complete prompt to stdout.

Usage: build-prompt.py <base64-encoded-instruction>
"""

import base64
import sys
from pathlib import Path

# Add the installed agent directory to path for importing pilot_agent
sys.path.insert(0, "/installed-agent")

ENV_CONTEXT_FILE = "/installed-agent/env-context.txt"
TEST_SCRIPT_FILE = "/tests/test.sh"


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: build-prompt.py <base64-encoded-instruction>", file=sys.stderr)
        sys.exit(1)

    # Decode instruction from base64 (avoids shell escaping issues)
    instruction = base64.b64decode(sys.argv[1]).decode("utf-8")

    # Read environment context
    env_context = None
    env_path = Path(ENV_CONTEXT_FILE)
    if env_path.exists():
        env_context = env_path.read_text()

    # Read test script — check shell test first, then pytest
    test_script = None
    test_path = Path(TEST_SCRIPT_FILE)
    if test_path.exists():
        test_script = test_path.read_text()
    else:
        # Look for pytest test files
        tests_dir = Path("/tests")
        if tests_dir.exists():
            pytest_files = sorted(tests_dir.glob("test_*.py"))
            if pytest_files:
                parts = []
                for pf in pytest_files:
                    parts.append(f"# {pf.name}\n{pf.read_text()}")
                test_script = "\n\n".join(parts)

    # Build prompt
    from pilot_agent.prompt_builder import build_prompt
    prompt = build_prompt(instruction, env_context, test_script)

    # Output to stdout
    print(prompt)


if __name__ == "__main__":
    main()

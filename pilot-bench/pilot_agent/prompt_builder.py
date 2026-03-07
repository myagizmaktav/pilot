"""
Prompt builder for Terminal-Bench tasks.

Phase 2: Jinja2-based prompt construction with:
- Task type auto-detection
- Conditional template sections
- Category-specific pattern injection
- Test script analysis
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader
    _HAS_JINJA2 = True
except ImportError:
    _HAS_JINJA2 = False

from .patterns import format_patterns_for_prompt

TEMPLATES_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "prompt.md.j2"

# Task type detection keywords
_TYPE_SIGNALS: dict[str, list[str]] = {
    "service_setup": [
        "install", "configure", "setup", "daemon", "server", "service",
        "nginx", "apache", "postgresql", "mysql", "redis", "mongodb",
        "systemd", "systemctl", "docker", "start", "run",
        "mailman", "pypi-server", "webserver",
    ],
    "file_config": [
        "config", "configuration", "yaml", "json", "toml", "ini",
        "edit", "modify", "create file", "write file",
        "certificate", "cert", "openssl",
    ],
    "scripting": [
        "script", "bash", "shell", "python script", "automate",
        "cron", "schedule", "regex", "parse", "extract",
        "log", "filter", "process",
    ],
    "network": [
        "network", "port", "socket", "http", "https", "grpc",
        "firewall", "iptable", "proxy", "tunnel", "ssh",
        "tcp", "udp", "dns",
    ],
    "database": [
        "database", "sql", "sqlite", "postgres", "mysql", "mongo",
        "query", "schema", "migration", "wal", "recovery",
        "table", "index",
    ],
    "build": [
        "compile", "build", "make", "cmake", "gcc", "linker",
        "cross-compil", "mips", "arm", "cython", "extension",
        "library", "linking", "object file",
    ],
    "scientific": [
        "scientific", "dna", "protein", "raman", "spectrum",
        "bioinformatics", "enzyme", "assembly", "mcmc", "stan",
        "mujoco", "simulation", "eigenval",
    ],
    "security": [
        "security", "vulnerability", "exploit", "hash", "crack",
        "cryptanalysis", "differential", "linear", "xss",
        "sanitize", "password", "secret",
    ],
    "ml": [
        "machine learning", "model", "training", "inference",
        "pytorch", "tensorflow", "huggingface", "transformer",
        "gpu", "cuda", "batch", "tensor", "pipeline",
        "fasttext", "cifar", "gpt",
    ],
}


def detect_task_type(instruction: str, test_script: str | None = None) -> str | None:
    """Detect task type from instruction and test script content.

    Uses keyword matching with scoring. Returns the highest-scoring
    task type, or None if no strong signal.
    """
    text = instruction.lower()
    if test_script:
        text += " " + test_script.lower()

    scores: dict[str, int] = {}
    for task_type, keywords in _TYPE_SIGNALS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[task_type] = score

    if not scores:
        return None

    # Require at least 2 keyword matches to be confident
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    if scores[best] >= 2:
        return best

    return None


def analyze_test_script(test_script: str) -> str | None:
    """Extract key assertions from a test script for the prompt.

    Returns a brief analysis of what the test checks.
    """
    if not test_script:
        return None

    checks: list[str] = []

    # File existence checks
    file_checks = re.findall(r'(?:-f|-e|-d)\s+"?([^"\s]+)"?', test_script)
    if file_checks:
        checks.append(f"**Files checked**: {', '.join(f'`{f}`' for f in file_checks[:10])}")

    # Port/curl checks
    port_checks = re.findall(r'(?:localhost|127\.0\.0\.1):(\d+)', test_script)
    if port_checks:
        unique_ports = sorted(set(port_checks))
        checks.append(f"**Ports checked**: {', '.join(unique_ports)}")

    # grep/content checks
    grep_checks = re.findall(r'grep\s+(?:-[a-zA-Z]+\s+)*"?([^"\n]+)"?', test_script)
    if grep_checks:
        checks.append(f"**Content checks**: {len(grep_checks)} pattern(s)")

    # Process checks
    process_checks = re.findall(r'(?:pgrep|pidof|ps.*grep)\s+"?([^"\s|]+)"?', test_script)
    if process_checks:
        checks.append(f"**Processes expected**: {', '.join(f'`{p}`' for p in process_checks)}")

    # Command checks
    cmd_checks = re.findall(r'command\s+-v\s+(\S+)|which\s+(\S+)', test_script)
    if cmd_checks:
        cmds = [c[0] or c[1] for c in cmd_checks]
        checks.append(f"**Commands required**: {', '.join(f'`{c}`' for c in cmds)}")

    # Exit code checks
    exit_checks = re.findall(r'\$\?\s*(?:-eq|-ne|==|!=)\s*(\d+)', test_script)
    if exit_checks:
        checks.append(f"**Exit code assertions**: {len(exit_checks)} check(s)")

    if not checks:
        return None

    return "Key assertions detected in test script:\n" + "\n".join(f"- {c}" for c in checks)


def extract_verification_commands(test_script: str) -> list[str]:
    """Extract concrete verification commands from test script assertions.

    Parses pytest/shell test scripts to produce actionable verification steps
    that Claude must run before finishing. Returns a list of specific commands.
    """
    if not test_script:
        return []

    commands: list[str] = []
    seen: set[str] = set()

    # ── Python test patterns ──

    # shutil.which() / which checks → binary must be in PATH
    which_checks = re.findall(
        r'(?:shutil\.which|which)\s*\(\s*["\']([^"\']+)["\']', test_script
    )
    for cmd in which_checks:
        key = f"which-{cmd}"
        if key not in seen:
            seen.add(key)
            commands.append(
                f"Run `which {cmd}` — if not found, locate the binary "
                f"(`find / -name {cmd} -type f 2>/dev/null`) and "
                f"`cp` it to `/usr/local/bin/{cmd}`"
            )

    # subprocess.run(["cmd", ...]) → command must be available
    subprocess_cmds = re.findall(
        r'subprocess\.run\(\s*\[\s*["\']([^"\']+)["\']', test_script
    )
    for cmd in subprocess_cmds:
        key = f"which-{cmd}"
        if key not in seen:
            seen.add(key)
            commands.append(
                f"Ensure `{cmd}` is available: run `which {cmd}`. "
                f"If missing, install or copy the binary to `/usr/local/bin/`"
            )

    # os.listdir(path) == [...] → directory must contain EXACT files
    # Handle both direct: `os.listdir(path) == [...]`
    # and indirect: `var = os.listdir(path)` ... `assert var == [...]`
    listdir_checks = re.findall(
        r'os\.listdir\(\s*["\']([^"\']+)["\']\s*\)\s*==\s*(\[.+?\])',
        test_script,
    )
    # Also find indirect pattern: var = os.listdir(path), then assert var == [...]
    listdir_vars = re.findall(
        r'(\w+)\s*=\s*os\.listdir\(\s*["\']([^"\']+)["\']\s*\)',
        test_script,
    )
    for var_name, path in listdir_vars:
        # Find the assert for this variable
        assert_match = re.search(
            rf'assert\s+{re.escape(var_name)}\s*==\s*(\[.+?\])',
            test_script,
        )
        if assert_match:
            listdir_checks.append((path, assert_match.group(1)))

    for path, expected in listdir_checks:
        # Parse the expected list to get file names for grep
        expected_files = re.findall(r'["\']([^"\']+)["\']', expected)
        if expected_files:
            keep_pattern = "|".join(re.escape(f) for f in expected_files)
            commands.append(
                f"CRITICAL: Run `ls {path}` — must contain EXACTLY {expected}. "
                f"Remove ALL other files: "
                f"`cd {path} && ls | grep -vE '^({keep_pattern})$' | xargs rm -f`"
            )
        else:
            commands.append(
                f"Run `ls {path}` — must contain EXACTLY {expected}. "
                f"Remove ALL unexpected files from this directory."
            )

    # os.path.exists / os.path.isfile → file must exist
    file_checks = re.findall(
        r'os\.path\.(?:exists|isfile)\(\s*["\']([^"\']+)["\']\s*\)',
        test_script,
    )
    for path in file_checks:
        key = f"exists-{path}"
        if key not in seen:
            seen.add(key)
            commands.append(f"Verify file exists: `test -f {path} && echo OK`")

    # curl / requests.get → endpoint must respond
    url_checks = re.findall(
        r'(?:requests\.get|urllib\.request\.urlopen|curl)\s*\(\s*["\']?'
        r'(https?://[^"\')\s]+)',
        test_script,
    )
    for url in url_checks:
        key = f"url-{url}"
        if key not in seen:
            seen.add(key)
            commands.append(f"Verify endpoint responds: `curl -sf {url}`")

    # Shell test patterns: command -v / which
    shell_which = re.findall(
        r'(?:command\s+-v|which)\s+(\S+)', test_script
    )
    for cmd in shell_which:
        key = f"which-{cmd}"
        if key not in seen:
            seen.add(key)
            commands.append(
                f"Ensure `{cmd}` is in PATH: `which {cmd}` must succeed"
            )

    # Shell test patterns: file checks
    shell_files = re.findall(r'-f\s+"?([^"\s]+)"?', test_script)
    for path in shell_files:
        if path.startswith("$"):
            continue  # Skip variable references
        key = f"exists-{path}"
        if key not in seen:
            seen.add(key)
            commands.append(f"Verify file exists: `test -f {path}`")

    # Shell test patterns: port checks
    port_checks = re.findall(
        r'(?:curl|wget|nc)\s+.*?(?:localhost|127\.0\.0\.1):(\d+)',
        test_script,
    )
    for port in sorted(set(port_checks)):
        commands.append(
            f"Verify service on port {port}: "
            f"`curl -sf http://localhost:{port}/ || ss -tlnp | grep :{port}`"
        )

    # Shell test patterns: process checks
    proc_checks = re.findall(
        r'(?:pgrep|pidof)\s+(\S+)', test_script
    )
    for proc in proc_checks:
        key = f"proc-{proc}"
        if key not in seen:
            seen.add(key)
            commands.append(
                f"Verify process running: `pgrep {proc} || ps aux | grep {proc}`"
            )

    return commands


def build_prompt(
    instruction: str,
    env_context: str | None = None,
    test_script: str | None = None,
) -> str:
    """Build the complete prompt for Claude Code execution.

    Uses Jinja2 template with task type detection and conditional sections.

    Args:
        instruction: The Terminal-Bench task instruction.
        env_context: Pre-scanned environment context (from bootstrap.sh).
        test_script: Contents of /tests/test.sh for test awareness.

    Returns:
        Complete prompt string.
    """
    # Detect task type
    task_type = detect_task_type(instruction, test_script)

    # Analyze test script
    test_analysis = analyze_test_script(test_script) if test_script else None

    # Extract concrete verification commands from test script
    verification_commands = extract_verification_commands(test_script) if test_script else []

    # Build patterns section
    patterns = format_patterns_for_prompt(task_type)

    # Try Jinja2 template first, fall back to inline
    if _HAS_JINJA2:
        try:
            return _render_jinja2(
                instruction=instruction,
                env_context=env_context,
                test_script=test_script,
                test_analysis=test_analysis,
                task_type=task_type,
                patterns=patterns,
            )
        except Exception:
            pass

    # Fallback to inline prompt (no Jinja2 dependency)
    return _build_inline(
            instruction=instruction,
            env_context=env_context,
            test_script=test_script,
            patterns=patterns,
            verification_commands=verification_commands,
        )


def _render_jinja2(
    instruction: str,
    env_context: str | None,
    test_script: str | None,
    test_analysis: str | None,
    task_type: str | None,
    patterns: str,
) -> str:
    """Render prompt from Jinja2 template."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(TEMPLATE_NAME)

    return template.render(
        instruction=instruction.strip(),
        env_context=env_context.strip() if env_context else None,
        test_script=test_script.strip() if test_script else None,
        test_analysis=test_analysis,
        task_type=task_type,
        patterns=patterns,
    )


def _build_inline(
    instruction: str,
    env_context: str | None,
    test_script: str | None,
    patterns: str,
    verification_commands: list[str] | None = None,
) -> str:
    """Fallback inline prompt builder (no Jinja2)."""
    sections: list[str] = []

    sections.append(
        "## Execution Mode: Fully Autonomous\n\n"
        "You are running in a Docker container for a Terminal-Bench evaluation.\n"
        "- There is NO human to ask questions to\n"
        "- You must complete the task entirely on your own\n"
        "- Your output is the FINAL submission — there is NO second chance\n"
        "- Work methodically: understand → plan → implement → verify\n"
        "- **You run as non-root user `pilot`**. You CANNOT write to /usr/local/bin/.\n"
        "  If you compile binaries, leave them in the build directory.\n"
        "  A post-execution fixup script will install them to PATH.\n"
        "  Do NOT waste time trying `sudo`, `su`, or modifying PATH/profile files."
    )

    if env_context:
        sections.append(
            f"## System Environment\n\n```\n{env_context.strip()}\n```\n\n"
            "Use detected tools and package managers. Don't install what's already available."
        )

    if test_script:
        sections.append(
            f"## Test Script (CRITICAL — READ THIS FIRST)\n\n"
            f"Your work will be graded by EXACTLY these tests:\n\n"
            f"```\n{test_script.strip()}\n```\n\n"
            "Read it line by line. Work backward from assertions. Match exact values.\n"
            "The tests run in a SEPARATE process — they call binaries via PATH (`which`),\n"
            "check directory contents via `os.listdir()`, and use `subprocess.run()`.\n"
            "Your binaries MUST be in PATH. Your output directories MUST be clean."
        )

    sections.append(f"## Task\n\n{instruction.strip()}")

    if patterns:
        sections.append(patterns)

    sections.append(
        "## Constraints\n\n"
        "- Timeout: ~60 minutes\n"
        "- Check if packages already installed before installing\n"
        "- Never modify /tests/\n"
        "- Container state persists — your changes are the final submission"
    )

    # Execution strategy comes after patterns/constraints for recency
    sections.append(
        "## Execution Strategy\n\n"
        "1. **Understand**: Re-read the task. Note EXACT file paths, output formats, constraints.\n"
        "2. **Plan**: List concrete steps. Identify what gets checked (paths, binaries in PATH, file contents).\n"
        "3. **Implement**: Execute step by step. After each step, verify it worked.\n"
        "4. **Final Cleanup** (do ALL of these before finishing):\n"
        "   - Output directories contain ONLY the requested files: `ls <output_dir>` and remove extras\n"
        "   - Services are running and responding: `curl localhost:<port>`\n"
        "   - File contents match the exact format (newlines, encoding, no trailing whitespace)\n"
        "   - NOTE: Do NOT try to install binaries to /usr/local/bin/ — you don't have root access.\n"
        "     Just leave compiled binaries in their build directory. They will be installed automatically."
    )

    # MANDATORY VERIFICATION — last section for maximum recency effect
    if verification_commands:
        lines = [
            "## MANDATORY VERIFICATION (run these EXACT checks before finishing)\n",
            "These conditions are extracted from the test script. Your submission WILL FAIL",
            "if any of these checks fail. Run each one and fix any issues:\n",
        ]
        for i, cmd in enumerate(verification_commands, 1):
            lines.append(f"{i}. {cmd}")
        lines.append("")
        lines.append(
            "Do NOT skip this section. Run every check. Fix every failure. "
            "This is the difference between pass and fail."
        )
        sections.append("\n".join(lines))
    else:
        # Generic mandatory verification when no test script is available
        sections.append(
            "## MANDATORY VERIFICATION (before finishing)\n\n"
            "1. Output directories contain ONLY requested files (remove build artifacts, *.o, temp files)\n"
            "2. All services running: `ps aux | grep <service>` and `curl localhost:<port>`\n"
            "3. All files exist at the EXACT paths specified in the task\n"
            "4. Compiled binaries exist in their build directory (PATH installation handled automatically)"
        )

    return "\n\n".join(sections)

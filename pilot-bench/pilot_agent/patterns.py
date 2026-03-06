"""
Terminal-Bench failure pattern library.

Built from:
- Terminal-Bench 2.0 paper failure taxonomy (8 types)
- Top agent post-mortems (Apex2, Warp, KIRA)
- Category-level failure analysis across 89 tasks

Patterns are injected into the prompt based on detected task type.
"""

from __future__ import annotations


# ── Core patterns (apply to ALL tasks) ──────────────────────────────────────

CORE_PATTERNS: list[dict[str, str]] = [
    # Failure Type 1: Disobey Task Specification (most common)
    {
        "id": "spec-compliance",
        "category": "Task Specification",
        "pattern": "Read the task description TWICE. If it says 'use CLI tool', don't use the Python API. If it says 'single file', don't create multiple files. If it specifies an output path, use that EXACT path.",
        "severity": "critical",
    },
    # Failure Type 2: Step Repetition
    {
        "id": "no-loops",
        "category": "Avoid Repetition Loops",
        "pattern": "If the same approach fails twice, STOP and try a fundamentally different strategy. Do not retry the same method more than twice. One agent wasted 452 tool calls retrying the same broken approach.",
        "severity": "critical",
    },
    # Failure Type 5: Premature Termination
    {
        "id": "verify-before-done",
        "category": "Verification Before Completion",
        "pattern": "NEVER declare 'done' until you have verified your work against the test script or task requirements. 25% of failures come from weak or missing verification.",
        "severity": "critical",
    },
    # Failure Type 6: Reasoning-Action Mismatch
    {
        "id": "match-actions",
        "category": "Reasoning-Action Alignment",
        "pattern": "If you say 'tests passed', the tests must ACTUALLY have passed. If you say 'I'll use method X', USE method X. Don't describe one approach then implement another.",
        "severity": "high",
    },
    # Missing executables (24.1% of command failures)
    {
        "id": "check-binaries",
        "category": "Missing Executables",
        "pattern": "24% of command failures are from missing executables. Before running ANY tool, verify it exists: `command -v <tool>` or `which <tool>`. Install if missing. Don't assume anything is pre-installed.",
        "severity": "critical",
    },
    # Context loss
    {
        "id": "track-state",
        "category": "State Tracking",
        "pattern": "Track what files you've created/modified. Don't overwrite your own earlier work by accident. Before editing a file, check its current contents.",
        "severity": "high",
    },
    # Heredoc failures (Apex2 finding)
    {
        "id": "heredoc-safety",
        "category": "Heredoc Safety",
        "pattern": "When writing multi-line content to files, use `cat > file << 'EOF'` (quoted EOF) to prevent variable expansion. Unescaped $ and backticks in heredocs cause silent corruption.",
        "severity": "high",
    },
    # Binary not in PATH (observed: sqlite, compile tasks)
    {
        "id": "install-to-path",
        "category": "Binary Installation",
        "pattern": "After compiling from source, ensure the binary is in a known location. You run as non-root so you CANNOT write to /usr/local/bin/. Just leave it in the build directory — a post-execution fixup handles PATH installation. Do NOT waste time trying sudo, su, or PATH/profile hacks.",
        "severity": "critical",
    },
    # Leftover build artifacts (observed: polyglot task)
    {
        "id": "clean-artifacts",
        "category": "Clean Output Directory",
        "pattern": "If the task says 'write X to /path/', the output directory must contain ONLY the requested file(s). Remove compiled binaries, temp files, and build artifacts from the output directory. Tests often check `os.listdir()` for exact contents.",
        "severity": "critical",
    },
    # Output format exactness
    {
        "id": "exact-output",
        "category": "Exact Output Format",
        "pattern": "Match output formats EXACTLY — file names, paths, separators, newlines. If task says 'one per line', use newlines not spaces. If task says 'write to /app/result.txt', use EXACTLY that path. Trailing whitespace/newlines can fail tests.",
        "severity": "critical",
    },
]


# ── Category-specific patterns ──────────────────────────────────────────────

SERVICE_PATTERNS: list[dict[str, str]] = [
    {
        "id": "service-binding",
        "category": "Service Binding",
        "pattern": "Services must bind to 0.0.0.0, not 127.0.0.1, for Docker network access. Check the service config explicitly.",
        "severity": "high",
    },
    {
        "id": "service-readiness",
        "category": "Service Readiness",
        "pattern": "After starting a service, WAIT for it to be ready. Poll with a loop: `for i in $(seq 1 30); do curl -s localhost:PORT && break; sleep 1; done`. Don't assume instant startup.",
        "severity": "high",
    },
    {
        "id": "daemon-survival",
        "category": "Daemon Management",
        "pattern": "Background processes (&) may die when the script exits. Use `nohup`, systemd, or the service's built-in daemon mode. Verify with `ps aux | grep`.",
        "severity": "high",
    },
    {
        "id": "port-conflicts",
        "category": "Port Conflicts",
        "pattern": "Before starting a service on a port, check it's not already in use: `ss -tlnp | grep :PORT`. Kill conflicting processes if needed.",
        "severity": "medium",
    },
    {
        "id": "service-logs",
        "category": "Service Debugging",
        "pattern": "If a service won't start, check logs FIRST: journalctl -u service, /var/log/, or run in foreground to see stderr. Don't blindly retry.",
        "severity": "medium",
    },
]

FILE_CONFIG_PATTERNS: list[dict[str, str]] = [
    {
        "id": "config-validation",
        "category": "Config Validation",
        "pattern": "ALWAYS validate config file syntax after writing: `python3 -c 'import yaml; yaml.safe_load(open(\"f.yml\"))'`, `jq . file.json`, `nginx -t`, `apachectl configtest`.",
        "severity": "critical",
    },
    {
        "id": "exact-values",
        "category": "Exact Value Matching",
        "pattern": "Config values must match test expectations EXACTLY — correct key names, correct casing, correct types (string vs int). No trailing whitespace or newlines.",
        "severity": "high",
    },
    {
        "id": "file-permissions",
        "category": "File Permissions",
        "pattern": "Scripts need `chmod +x`. Config files may need specific ownership. Log files need write permissions for the service user.",
        "severity": "high",
    },
    {
        "id": "directory-creation",
        "category": "Directory Creation",
        "pattern": "Create parent directories before writing files: `mkdir -p /path/to/dir`. Missing directories cause silent write failures.",
        "severity": "medium",
    },
    {
        "id": "line-endings",
        "category": "Line Endings",
        "pattern": "Use Unix line endings (LF). If a file has CRLF issues, fix with `sed -i 's/\\r$//' file`. Some parsers fail silently on wrong line endings.",
        "severity": "medium",
    },
]

SCRIPTING_PATTERNS: list[dict[str, str]] = [
    {
        "id": "shell-compat",
        "category": "Shell Compatibility",
        "pattern": "Some containers only have /bin/sh (POSIX). Avoid bashisms: no [[ ]], no arrays, no (( )). Use #!/bin/sh for maximum compatibility, or check `command -v bash` first.",
        "severity": "high",
    },
    {
        "id": "set-e",
        "category": "Error Handling",
        "pattern": "Use `set -e` to catch failures early. Check `$?` after critical commands. Pipelines: use `set -o pipefail` if bash is available.",
        "severity": "high",
    },
    {
        "id": "quoting",
        "category": "Variable Quoting",
        "pattern": "Always quote variables: `\"$var\"` not `$var`. Unquoted variables with spaces cause word-splitting bugs that are painful to debug.",
        "severity": "medium",
    },
]

NETWORK_PATTERNS: list[dict[str, str]] = [
    {
        "id": "firewall-check",
        "category": "Firewall / iptables",
        "pattern": "Check if iptables/firewalld is blocking connections. In Docker, usually not an issue but verify with `iptables -L` if connections fail.",
        "severity": "medium",
    },
    {
        "id": "dns-resolution",
        "category": "DNS Resolution",
        "pattern": "Use IP addresses (127.0.0.1) instead of hostnames (localhost) when possible. DNS resolution can fail in minimal containers.",
        "severity": "medium",
    },
    {
        "id": "tls-certs",
        "category": "TLS Certificates",
        "pattern": "Self-signed cert tasks: use `openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=localhost'`. Match exact paths the test expects.",
        "severity": "medium",
    },
]

DATABASE_PATTERNS: list[dict[str, str]] = [
    {
        "id": "db-init",
        "category": "Database Initialization",
        "pattern": "Databases need initialization before use. Run `initdb`, create the schema, grant permissions. Don't assume tables exist.",
        "severity": "high",
    },
    {
        "id": "db-auth",
        "category": "Database Authentication",
        "pattern": "Check pg_hba.conf for PostgreSQL auth, MySQL user grants. 'Access denied' errors are auth config, not network issues.",
        "severity": "medium",
    },
    {
        "id": "wal-mode",
        "category": "WAL Mode",
        "pattern": "SQLite WAL recovery tasks require understanding page format. Read the WAL header (first 32 bytes) to determine page size and checksum algorithm.",
        "severity": "medium",
    },
]

BUILD_PATTERNS: list[dict[str, str]] = [
    {
        "id": "build-deps",
        "category": "Build Dependencies",
        "pattern": "Install build essentials FIRST: `apt-get install -y build-essential cmake pkg-config`. Missing headers are the #1 build failure cause.",
        "severity": "critical",
    },
    {
        "id": "cross-compile",
        "category": "Cross-Compilation",
        "pattern": "Cross-compilation (MIPS, ARM) requires architecture-specific toolchain: `apt-get install gcc-mips-linux-gnu`. Set CC/CXX/CFLAGS for target architecture.",
        "severity": "high",
    },
    {
        "id": "library-paths",
        "category": "Library Paths",
        "pattern": "If linker can't find libraries: `ldconfig`, check LD_LIBRARY_PATH, use `-L/path` flag. pkg-config can help: `pkg-config --libs libname`.",
        "severity": "medium",
    },
    {
        "id": "version-pins",
        "category": "Version Pinning",
        "pattern": "Use specific versions when task requires it. `pip install package==X.Y.Z` not just `pip install package`. Version mismatches cause subtle failures.",
        "severity": "medium",
    },
]

SCIENTIFIC_PATTERNS: list[dict[str, str]] = [
    {
        "id": "api-specifics",
        "category": "API Specifics",
        "pattern": "Scientific libraries have strict API requirements. Read docs or source to confirm exact function signatures, parameter names, and return types. Don't guess.",
        "severity": "high",
    },
    {
        "id": "numerical-precision",
        "category": "Numerical Precision",
        "pattern": "Use appropriate numeric types. Float32 vs Float64 matters for convergence. Check tolerance thresholds in test assertions.",
        "severity": "medium",
    },
    {
        "id": "domain-terms",
        "category": "Domain Knowledge",
        "pattern": "Bioinformatics tasks (DNA, protein assembly) require exact biological terms and protocols. If unfamiliar, use web search to find the correct API and parameters.",
        "severity": "high",
    },
]

SECURITY_PATTERNS: list[dict[str, str]] = [
    {
        "id": "hash-tools",
        "category": "Hash / Crypto Tools",
        "pattern": "Install hashcat, john, or openssl for crypto tasks. Know the hash format identification: `hashid` or check the hash prefix.",
        "severity": "medium",
    },
    {
        "id": "attack-type",
        "category": "Attack Implementation",
        "pattern": "Cryptanalysis tasks require implementing the CORRECT attack (differential vs linear). Don't attempt brute force — it won't finish in time. Implement the mathematical approach.",
        "severity": "high",
    },
]

ML_PATTERNS: list[dict[str, str]] = [
    {
        "id": "gpu-fallback",
        "category": "GPU Fallback",
        "pattern": "No GPU in Docker. Use CPU mode: `device='cpu'`, `CUDA_VISIBLE_DEVICES=''`. OOM on CPU is possible — reduce batch size.",
        "severity": "critical",
    },
    {
        "id": "model-download",
        "category": "Model Downloads",
        "pattern": "HuggingFace models may need `HF_TOKEN` or `--trust-remote-code`. Download can timeout — check network and use `--resume-download` flag.",
        "severity": "medium",
    },
    {
        "id": "save-method",
        "category": "Model Saving",
        "pattern": "Use `save_pretrained()` not `torch.save()` for HuggingFace models. The test may check for specific file format.",
        "severity": "medium",
    },
]

# ── Task type to pattern set mapping ────────────────────────────────────────

TASK_TYPE_PATTERNS: dict[str, list[list[dict[str, str]]]] = {
    "service_setup": [SERVICE_PATTERNS, NETWORK_PATTERNS],
    "file_config": [FILE_CONFIG_PATTERNS],
    "scripting": [SCRIPTING_PATTERNS],
    "network": [NETWORK_PATTERNS, SERVICE_PATTERNS],
    "database": [DATABASE_PATTERNS, SERVICE_PATTERNS],
    "build": [BUILD_PATTERNS],
    "scientific": [SCIENTIFIC_PATTERNS],
    "security": [SECURITY_PATTERNS],
    "ml": [ML_PATTERNS, BUILD_PATTERNS],
}


def get_patterns_for_task(task_type: str | None = None) -> list[dict[str, str]]:
    """Get all relevant patterns for a task type.

    Always includes CORE_PATTERNS. Adds category-specific patterns
    when task_type is detected.
    """
    patterns = list(CORE_PATTERNS)

    if task_type and task_type in TASK_TYPE_PATTERNS:
        for pattern_list in TASK_TYPE_PATTERNS[task_type]:
            patterns.extend(pattern_list)

    return patterns


def format_patterns_for_prompt(task_type: str | None = None) -> str:
    """Format patterns into a prompt section.

    Args:
        task_type: Detected task type for category-specific patterns.

    Returns:
        Formatted markdown string for prompt injection.
    """
    patterns = get_patterns_for_task(task_type)

    if not patterns:
        return ""

    lines = ["## Known Failure Patterns", ""]
    lines.append("These patterns are extracted from analysis of 89 Terminal-Bench tasks and top agent failures. Pay attention to severity.")
    lines.append("")

    # Group by severity
    critical = [p for p in patterns if p.get("severity") == "critical"]
    high = [p for p in patterns if p.get("severity") == "high"]
    medium = [p for p in patterns if p.get("severity") == "medium"]

    if critical:
        lines.append("### CRITICAL (these cause >50% of failures)")
        lines.append("")
        for p in critical:
            lines.append(f"- **{p['category']}**: {p['pattern']}")
        lines.append("")

    if high:
        lines.append("### HIGH")
        lines.append("")
        for p in high:
            lines.append(f"- **{p['category']}**: {p['pattern']}")
        lines.append("")

    if medium:
        lines.append("### MEDIUM")
        lines.append("")
        for p in medium:
            lines.append(f"- **{p['category']}**: {p['pattern']}")
        lines.append("")

    return "\n".join(lines)

#!/bin/bash
# bootstrap.sh — Scans container environment before Claude Code execution.
# Output: /installed-agent/env-context.txt
set -e

OUT="/installed-agent/env-context.txt"
mkdir -p /installed-agent

{
    echo "=== ENVIRONMENT CONTEXT ==="
    echo ""

    # OS detection
    echo "## Operating System"
    uname -a 2>/dev/null || echo "uname: not available"
    if [ -f /etc/os-release ]; then
        echo "--- /etc/os-release ---"
        cat /etc/os-release
    fi
    echo ""

    # Available tools
    echo "## Available Tools"
    for cmd in python3 python pip pip3 node npm go gcc g++ make cmake \
               curl wget git jq yq sed awk grep find tar gzip unzip \
               docker docker-compose kubectl helm \
               ruby gem cargo rustc java javac mvn gradle \
               sqlite3 mysql psql mongosh redis-cli \
               nginx apache2 httpd systemctl service \
               crontab at supervisord; do
        if command -v "$cmd" &> /dev/null; then
            ver=$($cmd --version 2>/dev/null | head -1 || echo "installed")
            echo "  ✓ $cmd: $ver"
        fi
    done
    echo ""

    # Package managers
    echo "## Package Managers"
    for pm in apt-get apk dnf yum pacman zypper brew pip pip3 npm gem cargo; do
        if command -v "$pm" &> /dev/null; then
            echo "  ✓ $pm"
        fi
    done
    echo ""

    # Shell info
    echo "## Shell"
    echo "  SHELL=$SHELL"
    echo "  bash: $(command -v bash 2>/dev/null || echo 'not found')"
    echo "  sh: $(command -v sh 2>/dev/null || echo 'not found')"
    echo "  zsh: $(command -v zsh 2>/dev/null || echo 'not found')"
    echo ""

    # Working directory
    echo "## Working Directory"
    echo "  PWD=$(pwd)"
    echo "  Contents:"
    ls -la 2>/dev/null | head -30 || echo "  (empty or inaccessible)"
    echo ""

    # Test directory
    echo "## Test Structure"
    if [ -d /tests ]; then
        echo "  /tests/ exists:"
        find /tests -maxdepth 2 -type f 2>/dev/null | head -20 || echo "  (empty)"
    else
        echo "  /tests/ does not exist"
    fi
    echo ""

    # Disk and memory
    echo "## Resources"
    echo "  Disk: $(df -h / 2>/dev/null | tail -1 || echo 'unknown')"
    echo "  Memory: $(free -h 2>/dev/null | grep Mem || echo 'unknown')"
    echo ""

    # Network
    echo "## Network"
    if command -v curl &> /dev/null; then
        echo "  Internet: $(curl -s --max-time 3 -o /dev/null -w '%{http_code}' https://api.anthropic.com/v1/messages 2>/dev/null || echo 'unreachable')"
    fi
    echo ""

    echo "=== END ENVIRONMENT CONTEXT ==="
} > "$OUT" 2>&1

echo "Environment context written to $OUT ($(wc -l < "$OUT") lines)"

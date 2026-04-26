#!/bin/bash

# Shared Go toolchain checks for repo scripts.

ensure_go_on_path() {
    if command -v go >/dev/null 2>&1; then
        return 0
    fi

    local candidate
    for candidate in \
        "$HOME/.local"/go*/bin/go \
        "$HOME/go/bin/go" \
        /usr/local/go/bin/go \
        /usr/lib/go/bin/go
    do
        if [ -x "$candidate" ]; then
            export PATH="$(dirname "$candidate"):$PATH"
            return 0
        fi
    done

    return 1
}

require_go() {
    if ensure_go_on_path; then
        return 0
    fi

    echo "Go is not installed or not on PATH. Install Go 1.24+ from https://go.dev/dl/" >&2
    return 1
}

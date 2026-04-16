#!/usr/bin/env bash
set -euo pipefail

# Optional PATH helpers (systemd services often have a minimal PATH).
if command -v brew >/dev/null 2>&1; then
    eval "$(brew shellenv bash)" 2>/dev/null || true
fi

CLAUDE_BIN="${CLAUDE_BIN:-claude}"
CURSOR_AGENT_BIN="${CURSOR_AGENT_BIN:-cursor-agent}"

AGENT_NAME="$1"
BACKEND="$2"
MODEL="$3"
WORKSPACE_DIR="$4"
PROMPT="$5"
SESSION_ID="${6:-}"

if [ "$BACKEND" = "claude" ]; then
    unset CLAUDECODE
    cd "$WORKSPACE_DIR"
    RESUME_FLAG=()
    if [ -n "$SESSION_ID" ]; then
        RESUME_FLAG=(--resume "$SESSION_ID")
    fi
    exec "$CLAUDE_BIN" --agent "$AGENT_NAME" \
        -p "$PROMPT" \
        --output-format stream-json \
        --verbose \
        --model "$MODEL" \
        "${RESUME_FLAG[@]+"${RESUME_FLAG[@]}"}"
elif [ "$BACKEND" = "cursor" ]; then
    exec "$CURSOR_AGENT_BIN" -p \
        --workspace "$WORKSPACE_DIR" \
        --model "$MODEL" \
        "$PROMPT"
else
    echo "Unknown backend: $BACKEND" >&2
    exit 1
fi

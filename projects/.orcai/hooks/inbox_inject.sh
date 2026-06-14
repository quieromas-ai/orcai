#!/usr/bin/env bash
# PostToolUse hook: surface any queued Slack follow-ups to the running agent mid-run.
# No-op unless the router set $ORCAI_INBOX and there are pending messages.
set -euo pipefail

INBOX="${ORCAI_INBOX:-}"
[ -n "$INBOX" ] && [ -s "$INBOX" ] || exit 0

# Atomically claim the queue so concurrent appends land in a fresh file.
CLAIM="${INBOX}.consumed.$$"
mv "$INBOX" "$CLAIM" 2>/dev/null || exit 0

python3 "$(dirname "$0")/inbox_emit.py" PostToolUse "$CLAIM" || true
rm -f "$CLAIM"

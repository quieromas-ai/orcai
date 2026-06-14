#!/usr/bin/env bash
# Stop hook: if Slack follow-ups queued up before the agent stopped, block the stop and
# feed them back so the SAME session handles them. No-op when the inbox is empty/unset.
set -euo pipefail

INBOX="${ORCAI_INBOX:-}"
[ -n "$INBOX" ] && [ -s "$INBOX" ] || exit 0

# Atomically claim the queue so concurrent appends land in a fresh file.
CLAIM="${INBOX}.consumed.$$"
mv "$INBOX" "$CLAIM" 2>/dev/null || exit 0

python3 "$(dirname "$0")/inbox_emit.py" Stop "$CLAIM" || true
rm -f "$CLAIM"

#!/usr/bin/env python3
"""Render claimed inbox messages into a Claude Code hook JSON payload.

Usage: inbox_emit.py <Stop|PostToolUse> <claimed_inbox_file>

Reads the JSONL inbox the router wrote ({"ts","user","text"} per line) and prints the
hook output that injects those messages back into the running agent:
  - Stop:        decision=block + reason  → keeps the process alive to handle them
  - PostToolUse: hookSpecificOutput.additionalContext → surfaces them mid-run

Prints nothing (exit 0) when there is nothing to deliver, so the hook is a no-op.
"""
import json
import sys


def main() -> None:
    if len(sys.argv) != 3:
        return
    event, path = sys.argv[1], sys.argv[2]

    msgs: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue
                who = m.get("user") or "user"
                msgs.append(f"- ({who}) {m.get('text', '')}")
    except OSError:
        return

    if not msgs:
        return

    body = (
        "New Slack message(s) arrived from the user while you were working. "
        "Treat them as fresh instructions and adapt your current work:\n" + "\n".join(msgs)
    )

    if event == "Stop":
        out = {
            "decision": "block",
            "reason": body,
            "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": body},
        }
    else:
        out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": body}}

    print(json.dumps(out))


if __name__ == "__main__":
    main()

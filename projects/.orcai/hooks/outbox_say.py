#!/usr/bin/env python3
"""Queue a proactive Slack message for the router to relay via the bot token.

Usage: outbox_say.py [--dm] <message>

Appends one JSON line {"text": <message>, "dm": <bool>} to the file named in $ORCAI_OUTBOX.
The router (which holds the Slack bot token) polls that file while the agent runs and posts each
message from the single bot identity:
  - default → into the run's Slack thread (channel + thread_ts)
  - --dm    → as a direct message to the user, for escalation

No-op (exit 0) when $ORCAI_OUTBOX is unset, so it is safe to call outside a router run.
"""
import json
import os
import sys


def main() -> None:
    args = sys.argv[1:]
    dm = False
    if args and args[0] == "--dm":
        dm = True
        args = args[1:]
    message = " ".join(args).strip()
    if not message:
        return
    outbox = os.environ.get("ORCAI_OUTBOX", "")
    if not outbox:
        return
    os.makedirs(os.path.dirname(outbox), exist_ok=True)
    line = json.dumps({"text": message, "dm": dm}, ensure_ascii=False)
    with open(outbox, "a", encoding="utf-8") as f:
        f.write(line + "\n")


if __name__ == "__main__":
    main()

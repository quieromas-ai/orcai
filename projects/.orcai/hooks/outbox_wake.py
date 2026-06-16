#!/usr/bin/env python3
"""Request a router-armed wakeup so an in-flight run resumes autonomously after this turn exits.

Usage: outbox_wake.py --delay <seconds> [--reason <text>] [--prompt <text>]

Writes a single JSON object {"delay_seconds", "reason", "prompt"} to the file named in
$ORCAI_WAKE, overwriting any earlier request (last-write-wins — only the final request before the
turn ends takes effect). After the turn exits, the router reads this file and arms a timer; when it
fires, the router re-spawns this session via `--resume` with the given prompt.

Why this instead of the harness ScheduleWakeup tool: this agent runs headless (`claude -p`), which
exits after one turn — an in-session ScheduleWakeup timer dies with the process and never fires.
This file hands the schedule to the long-lived router, which can actually honour it.

The router clamps the delay to [60, 3600] seconds. No-op (exit 0) when $ORCAI_WAKE is unset (i.e.
the agent is not wakeup-enabled, or running outside a router run), so it is always safe to call.
"""
import argparse
import json
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="Request a router-armed wakeup.")
    parser.add_argument("--delay", type=int, required=True, help="seconds until wakeup (clamped 60–3600)")
    parser.add_argument("--reason", default="", help="short reason, surfaced in router logs")
    parser.add_argument("--prompt", default="", help="prompt to resume the session with on wakeup")
    args = parser.parse_args()

    wake = os.environ.get("ORCAI_WAKE", "")
    if not wake:
        return
    os.makedirs(os.path.dirname(wake), exist_ok=True)
    payload = {
        "delay_seconds": args.delay,
        "reason": args.reason,
        "prompt": args.prompt,
    }
    # Overwrite, not append: only the latest request matters.
    with open(wake, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


if __name__ == "__main__":
    main()

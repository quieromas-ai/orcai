---
name: orcai-wake
description: Schedule a router-armed wakeup so an in-flight run resumes autonomously after this turn ends. Use before ending a turn while you still have background work to poll (a delegated task, a build, an orchestration run) and no human reply is expected to wake you.
allowed-tools: Bash
---

# Schedule a wakeup (orcai-wake)

You run **headless** (`claude -p`): your process exits when this turn ends. An in-session
`ScheduleWakeup` / `/loop` timer **does not survive** that exit and will never fire. To resume
autonomously, hand the schedule to the **orcai router** instead — it is long-lived and re-spawns
your session via `--resume` when the timer fires.

The router exports `$ORCAI_WAKE_BIN` (absolute path to the helper) for the run — use it directly;
do **not** rely on `$CLAUDE_PROJECT_DIR` (it is unset in this run).

Before ending your turn, request the next wakeup:

```bash
python3 "$ORCAI_WAKE_BIN" --delay 1200 \
  --reason "polling team-leader task, ~20m" \
  --prompt "WAKEUP: resume the in-flight run — read .orchestration state from disk first, then poll the delegated task."
```

- `--delay` — seconds until the wakeup. The router clamps it to **[60, 3600]** (1 min – 1 h). For a
  longer wait, schedule the max and re-schedule on each wakeup.
- `--reason` — short text, surfaced in the router logs (helps you and Tomasz see why it woke).
- `--prompt` — what you'll be handed when it fires. Make it self-contained: your context is fresh on
  wakeup, so tell yourself where the state lives and what to do.

How it works:
- The request is written to `$ORCAI_WAKE` (last-write-wins — only your final call this turn counts).
- After your turn exits cleanly, the router arms a timer. When it fires, the router re-spawns this
  session with `--resume` and your `--prompt` — exactly as if a Slack reply had arrived.
- If a **real** Slack reply lands in the thread before the timer fires, that wins and the pending
  wakeup is cancelled (no double-resume).

Guidance:
- Only schedule a wakeup when you genuinely have background work to come back to. If your work is
  done, just end the turn — do **not** schedule one.
- This is **in-memory** in the router: a router restart drops the timer. That's acceptable — your
  session still resumes on the next Slack reply in the thread.
- Safe to call anytime: it is a no-op when `$ORCAI_WAKE` is unset (i.e. you are not wakeup-enabled,
  or running outside a router run).

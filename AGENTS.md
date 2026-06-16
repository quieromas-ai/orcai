# AGENTS.md

This file provides context for AI agents (Claude Code, Cursor) working on the orcai-slack codebase itself.

## What this repo is

A Slack-to-agent router: a Python service that listens for Slack messages and dispatches them to Claude Code or Cursor agents running in configured project workspaces.

## Repository structure

| Path | Purpose |
|------|---------|
| `router/` | The router service — Python package, entry point, platform parsers, tests |
| `projects/` | Example project workspace — copy and customise for your own projects |

## Working on the router

- Entry point: `router/router.py` (run as `python -m router` from repo root)
- Platform parsers: `router/platforms/` — one module per integration (`slack`, `github`, `azure_devops`)
- Tests: `router/tests/` — run with `cd router && .venv/bin/python -m pytest tests/ -v`
- Config: `router/config.yaml` (gitignored — copy from `router/config.example.yaml`)

## Working on the example workspace

- `projects/.claude/agents/` — agent definitions (`engineer.md`, `pm.md`)
- `projects/.claude/skills/` — reusable skills invoked by agents (`check-inbox` for follow-ups, `orcai-say` for Slack updates, `orcai-wake` for self-scheduled resumes)
- `projects/.claude/settings.json` — inbox-delivery hooks (PostToolUse → `inbox_inject.sh`, Stop → `inbox_drain.sh`)
- `projects/.orcai/hooks/` — IPC scripts: inbox (`inbox_inject.sh`, `inbox_drain.sh`, `inbox_emit.py`), outbox (`outbox_say.py`), wake (`outbox_wake.py`)
- `projects/config.yaml` (gitignored — copy from `projects/config.example.yaml`)

## Follow-up message delivery (router ↔ live agent)

A follow-up message in a thread whose agent is still running is queued and handled by the **same session** rather than forking a second process. There are two layers — serialization is universal; mid-run delivery is opt-in:

- **Serialization (universal):** `spawn_engineer` provisions a per-thread inbox file (`<workspace>/.orcai/inbox/<session>.jsonl`, path on `SessionRecord.inbox_path`) for *every* tracked session. `_try_route_event` enqueues to it (and reacts 👀) when the thread's session is `running`/`draining` — no second process is spawned, and the original `session_by_thread` mapping is preserved. On exit, `spawn_engineer` drains any queued messages via a same-session `--resume` continuation.
- **Mid-run delivery (opt-in via `follow_thread`):** `$ORCAI_INBOX` is exported to the agent **only** when `ProjectConfig.follow_thread` is true (config key `follow_thread: true`, default false). So a flag-off agent queues replies and picks them up on the next `--resume` after it finishes, but is never interrupted mid-flight; a flag-on agent additionally gets them injected into the live run.
- **Agent side (example workspace):** the `PostToolUse`/`Stop` hooks (`projects/.orcai/hooks/`) read `$ORCAI_INBOX` and inject queued messages back into the live session; the `check-inbox` skill lets the agent poll explicitly during long waits. All hooks no-op when `$ORCAI_INBOX` is unset (i.e. for flag-off agents).

## Self-paced wakeups (router-armed)

A `wakeup_enabled` agent can schedule its own next turn — the headless-safe replacement for the harness `ScheduleWakeup`, which never fires under `claude -p` (the agent exits after one turn).

- **Provisioning (opt-in via `wakeup_enabled`):** `$ORCAI_WAKE` (request file) and `$ORCAI_WAKE_BIN` (helper path) are exported to the agent **only** when `ProjectConfig.wakeup_enabled` is true (config key `wakeup_enabled: true`, default false).
- **Arm:** on a clean idle exit (`exit_code == 0`, empty inbox), `spawn_engineer` reads the request via `_read_wake_request`, clamps the delay to `[WAKE_MIN_SECONDS, WAKE_MAX_SECONDS]`, and arms `loop.call_later(_fire_wakeup, …)` on `SessionRecord.wake_handle`. `_fire_wakeup` re-spawns via `--resume`, mirroring a Slack auto-resume.
- **Cancel:** `_cancel_wake` drops the pending timer whenever real traffic supersedes it (auto-resume, DM resume, inbox-queue, or a new session record for the thread); a drain also discards a stale request. A session is never double-resumed. In-memory only — a router restart loses armed timers.
- **Agent side (example workspace):** the `orcai-wake` skill calls `outbox_wake.py`, which writes `$ORCAI_WAKE` (last-write-wins JSON). No-op when `$ORCAI_WAKE` is unset.

## Conventions

- Do not commit `*.env`, `config.yaml`, `AGENTS.md`, or `logs/` — all gitignored
- Run the test suite before opening a PR
- Keep skills self-contained: each skill reads its inputs from `$ARGUMENTS` and produces a clear summary output

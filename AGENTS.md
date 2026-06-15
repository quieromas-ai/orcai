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
- `projects/.claude/skills/` — reusable skills invoked by agents (incl. `check-inbox` for follow-ups)
- `projects/.claude/settings.json` — inbox-delivery hooks (PostToolUse → `inbox_inject.sh`, Stop → `inbox_drain.sh`)
- `projects/.orcai/hooks/` — inbox hook scripts (`inbox_inject.sh`, `inbox_drain.sh`, `inbox_emit.py`)
- `projects/config.yaml` (gitignored — copy from `projects/config.example.yaml`)

## Follow-up message delivery (router ↔ live agent)

A follow-up message in a thread whose agent is still running is queued and handled by the **same session** rather than forking a second process. There are two layers — serialization is universal; mid-run delivery is opt-in:

- **Serialization (universal):** `spawn_engineer` provisions a per-thread inbox file (`<workspace>/.orcai/inbox/<session>.jsonl`, path on `SessionRecord.inbox_path`) for *every* tracked session. `_try_route_event` enqueues to it (and reacts 👀) when the thread's session is `running`/`draining` — no second process is spawned, and the original `session_by_thread` mapping is preserved. On exit, `spawn_engineer` drains any queued messages via a same-session `--resume` continuation.
- **Mid-run delivery (opt-in via `follow_thread`):** `$ORCAI_INBOX` is exported to the agent **only** when `ProjectConfig.follow_thread` is true (config key `follow_thread: true`, default false). So a flag-off agent queues replies and picks them up on the next `--resume` after it finishes, but is never interrupted mid-flight; a flag-on agent additionally gets them injected into the live run.
- **Agent side (example workspace):** the `PostToolUse`/`Stop` hooks (`projects/.orcai/hooks/`) read `$ORCAI_INBOX` and inject queued messages back into the live session; the `check-inbox` skill lets the agent poll explicitly during long waits. All hooks no-op when `$ORCAI_INBOX` is unset (i.e. for flag-off agents).

## Conventions

- Do not commit `*.env`, `config.yaml`, `AGENTS.md`, or `logs/` — all gitignored
- Run the test suite before opening a PR
- Keep skills self-contained: each skill reads its inputs from `$ARGUMENTS` and produces a clear summary output

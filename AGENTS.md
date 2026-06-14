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

A follow-up message in a thread whose agent is still running is delivered to that **running** process rather than forking a second one. The moving parts:

- **Opt-in per agent:** the whole mechanism is gated on `ProjectConfig.follow_thread` (config key `follow_thread: true`, default false). `spawn_engineer` only provisions the inbox (and exports `$ORCAI_INBOX`) for `follow_thread` agents; everything downstream keys off `SessionRecord.inbox_path` being set, so other agents keep the old behavior.
- **Router side (`router/router.py`):** each `follow_thread` session gets a per-thread inbox file (`<workspace>/.orcai/inbox/<session>.jsonl`, path on `SessionRecord.inbox_path`, exported to the agent as `$ORCAI_INBOX`). `_try_route_event` enqueues to the inbox (and reacts 👀) when the thread's session is `running`/`draining`; `spawn_engineer` posts each agent turn live, and on exit drains any queued messages via a same-session `--resume` continuation.
- **Agent side (example workspace):** the `PostToolUse`/`Stop` hooks (`projects/.orcai/hooks/`) read `$ORCAI_INBOX` and inject queued messages back into the live session; the `check-inbox` skill lets the agent poll explicitly during long waits. All hooks no-op when `$ORCAI_INBOX` is unset.

## Conventions

- Do not commit `*.env`, `config.yaml`, `AGENTS.md`, or `logs/` — all gitignored
- Run the test suite before opening a PR
- Keep skills self-contained: each skill reads its inputs from `$ARGUMENTS` and produces a clear summary output

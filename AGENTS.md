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
- `projects/.claude/skills/` — reusable skills invoked by agents
- `projects/config.yaml` (gitignored — copy from `projects/config.example.yaml`)

## Conventions

- Do not commit `*.env`, `config.yaml`, `AGENTS.md`, or `logs/` — all gitignored
- Run the test suite before opening a PR
- Keep skills self-contained: each skill reads its inputs from `$ARGUMENTS` and produces a clear summary output

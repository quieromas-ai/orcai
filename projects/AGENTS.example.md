# AGENTS.md

Copy this file to `AGENTS.md` in this workspace and customize it. The agent reads `AGENTS.md` for host and project context.

## Fill in for your environment

- **Host**: OS and anything relevant (e.g. Linux server, local macOS) — avoid sharing secrets or internal hostnames you do not want in logs.
- **User layout**: Where repositories are cloned and how paths relate to the workspace root from `router/config.yaml`.
- **Tooling**: Which CLIs are installed (`gh`, `az`, language runtimes) and how the agent should authenticate (tokens via `.env`, `az devops login`, etc.).
- **Workflow**: Branching conventions, worktrees, where to store plans, review rules, and who to @mention in Slack summaries.

## Example bullets (replace with your own)

- Target repos live under `<path>/worktrees/` (or your convention).
- Use `gh` for GitHub issues and PRs; use `az` for Azure DevOps when applicable.
- Store planning documents under `.claude/plans/` (or your team standard).

## Follow-up messages during a run

Follow-up Slack messages in the same thread are delivered to the **already-running** agent
instead of starting a second process. The router appends them to the file named in
`$ORCAI_INBOX`; the workspace hooks then surface them to you automatically — after each tool
call (`PostToolUse`) and again if you try to stop with messages still pending (`Stop`).

To stay responsive during long or asynchronous work, **do not block in one long call** — run
the wait as a poll loop (e.g. launch the work in the background, then repeatedly `sleep 20` and
check status), so queued messages reach you promptly. You can also check explicitly at any time
with the `check-inbox` skill. Treat any new message as a fresh instruction: acknowledge it and
adapt your current plan.

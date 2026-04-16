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

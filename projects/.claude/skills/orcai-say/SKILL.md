---
name: orcai-say
description: Post a proactive Slack update (or escalation DM) to the user mid-run. The router relays it through the bot identity. Use to report milestones during a long task, or --dm to escalate a blocker.
allowed-tools: Bash
---

# Send a Slack update (orcai-say)

All your Slack output goes through the **orcai router**, which posts with the bot identity — never
via the claude.ai Slack MCP (`mcp__claude_ai_Slack__*`). That MCP is a *different* Slack identity
and cannot post into this bot's threads or DMs (it fails with `channel_not_found`).

The router exports `$ORCAI_SAY` (absolute path to the helper) for the run — use it directly; do
**not** rely on `$CLAUDE_PROJECT_DIR` (it is unset in this run).

Post a brief progress update into the current Slack thread:

```bash
python3 "$ORCAI_SAY" "1/3 — pre-flight done; pulling open PRs next"
```

Escalate a blocker as a direct message to the user (add `--dm`):

```bash
python3 "$ORCAI_SAY" --dm "Blocked on X — need a product decision."
```

Guidance:
- One short message per milestone (work kicked off, a phase done, a PR opened, a run finished) —
  not a play-by-play, not per task.
- Do **not** repeat your final answer; the router posts that automatically when you finish.
- Safe to call anytime: it is a no-op when `$ORCAI_OUTBOX` is unset (i.e. outside a router run).

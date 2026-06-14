---
name: check-inbox
description: Poll for new Slack messages the router queued while a long task runs. Use during long or async waits to pick up follow-up instructions from the user mid-run.
allowed-tools: Bash
---

# Check inbox

While you work, follow-up Slack messages from the user are queued by the router to the file
named in the `$ORCAI_INBOX` environment variable (one JSON object `{"ts","user","text"}` per
line). They are also delivered automatically after each tool call and before you stop, but you
can check explicitly at any time — especially while waiting on a long background job.

Run:

```bash
if [ -n "$ORCAI_INBOX" ] && [ -s "$ORCAI_INBOX" ]; then
  cat "$ORCAI_INBOX"
  : > "$ORCAI_INBOX"   # mark as read so it isn't delivered again
else
  echo "(no new messages)"
fi
```

If there are new messages, treat them as fresh instructions from the user: acknowledge them,
adapt your current plan, and continue. If empty, carry on with your task.

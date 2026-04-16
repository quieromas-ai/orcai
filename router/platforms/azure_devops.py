"""Azure DevOps Slack message parsing."""

from typing import Any

from router.platforms.messaging import ProjectConfigLike, _first_match, extract_slack_message_text

_TASK_ID_RE = r"\bTask #?(\d+)\b"


class AzureDevOpsMessageParser:
    """Parser for Azure DevOps Slack notifications.

    Azure Boards puts the event summary in the top-level ``text`` field and the
    work item title in ``attachments[0].title`` (or ``fallback``).  The generic
    extractor short-circuits on a non-empty ``text`` and therefore misses the
    task number / title.  This parser combines both so the agent receives the
    full context it needs.
    """

    def extract_message_text(self, slack_message: dict[str, Any]) -> str:
        text = (slack_message.get("text") or "").strip()

        attachments = slack_message.get("attachments") or []
        if attachments:
            att = attachments[0]
            # Azure DevOps stores the work item title in `title`; fall back to
            # `fallback` which also contains it when `title` is absent.
            title = (att.get("title") or att.get("fallback") or "").strip()
            pretext = (att.get("pretext") or "").strip()
            body = (att.get("text") or "").strip()
            parts = [p for p in [title, pretext, body] if p]
            if parts:
                extra = "\n".join(parts)
                text = f"{text}\n{extra}".strip() if text else extra

        if not text:
            # Fall back to Block Kit sections (same as generic extractor).
            text = extract_slack_message_text(slack_message)

        return text

    def should_forward(
        self, slack_message: dict[str, Any], project_config: ProjectConfigLike
    ) -> bool:
        """Forward all messages; Azure-specific filters can be added later."""
        return True

    def extract_ticket_id(self, slack_message: dict[str, Any]) -> str | None:
        """Return the work item ID from the Slack message, or None if not found."""
        attachments = slack_message.get("attachments") or []
        if attachments:
            att = attachments[0]
            title_link = att.get("title_link") or ""
            result = _first_match(r"/(\d+)$", title_link) or _first_match(
                r"[?&]id=(\d+)", title_link
            )
            if result:
                return result
            title = att.get("title") or att.get("fallback") or ""
            result = _first_match(_TASK_ID_RE, title)
            if result:
                return result
        return _first_match(_TASK_ID_RE, (slack_message.get("text") or "").strip())


def get_parser() -> AzureDevOpsMessageParser:
    """Return the Azure DevOps message parser instance."""
    return AzureDevOpsMessageParser()

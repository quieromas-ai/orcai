"""GitHub-specific Slack message parsing (PR/issue events from GitHub Slack integration)."""

from typing import Any

from router.platforms.messaging import ProjectConfigLike, _first_match, extract_slack_message_text


class GitHubMessageParser:
    """Extract and filter Slack messages from GitHub integration (attachments)."""

    def extract_message_text(self, slack_message: dict[str, Any]) -> str:
        return extract_slack_message_text(slack_message)

    def should_forward(
        self, slack_message: dict[str, Any], project_config: ProjectConfigLike
    ) -> bool:
        """Forward unless project config defines filters (e.g. labels, component, status)."""
        # No filter keys on project config yet; always forward.
        return True

    def extract_ticket_id(self, slack_message: dict[str, Any]) -> str | None:
        """Return the issue/PR number from the Slack message, or None if not found."""
        attachments = slack_message.get("attachments") or []
        if attachments:
            result = _first_match(r"/(?:issues|pull)/(\d+)", attachments[0].get("title_link") or "")
            if result:
                return result
        return _first_match(r"#(\d+)", (slack_message.get("text") or "").strip())


def get_parser() -> GitHubMessageParser:
    """Return the GitHub message parser instance."""
    return GitHubMessageParser()

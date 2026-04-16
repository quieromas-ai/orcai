"""Slack-native message parser for direct @mentions and DMs."""

from typing import Any

from router.platforms.messaging import ProjectConfigLike, extract_slack_message_text


class SlackMessageParser:
    """Extract and filter native Slack messages (not GitHub/ADO integrations)."""

    def extract_message_text(self, slack_message: dict[str, Any]) -> str:
        return extract_slack_message_text(slack_message)

    def should_forward(
        self, slack_message: dict[str, Any], project_config: ProjectConfigLike
    ) -> bool:
        return True

    def extract_ticket_id(self, slack_message: dict[str, Any]) -> str | None:
        """Use thread root timestamp as deduplication key for conversation threads."""
        return slack_message.get("thread_ts") or slack_message.get("ts") or None


def get_parser() -> SlackMessageParser:
    """Return the Slack native message parser instance."""
    return SlackMessageParser()

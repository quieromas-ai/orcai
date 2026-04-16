"""Message parser interface and factory for platform-specific Slack message handling."""

import importlib
import logging
import re
from typing import Any, Literal, Protocol, cast


def _first_match(pattern: str, text: str) -> str | None:
    """Return capture group 1 of the first regex match, or None."""
    m = re.search(pattern, text)
    return m.group(1) if m else None


def extract_slack_message_text(slack_message: dict[str, Any]) -> str:
    """Extract forwarding text: top-level text → attachment → Block Kit sections."""
    text = (slack_message.get("text") or "").strip()
    if not text:
        attachments = slack_message.get("attachments") or []
        if attachments:
            attachment = attachments[0]
            pretext = attachment.get("pretext", "")
            body = attachment.get("text", "")
            text = f"{pretext}\n{body}".strip() if pretext or body else ""
    if not text:
        blocks = slack_message.get("blocks") or []
        parts = [
            block["text"]["text"]
            for block in blocks
            if block.get("type") == "section"
            and isinstance(block.get("text"), dict)
            and block["text"].get("text")
        ]
        text = "\n".join(parts).strip()
    return text

logger = logging.getLogger("router.platforms")


Platform = Literal["slack", "github", "azure_devops"]


class ProjectConfigLike(Protocol):
    """Minimal project config shape required by MessageParser.should_forward."""

    name: str
    platform: Platform


class MessageParser(Protocol):
    """Interface for extracting and filtering Slack messages per platform (GitHub, Azure DevOps)."""

    def extract_message_text(self, slack_message: dict[str, Any]) -> str:
        """Return the string to forward to the agent; empty if nothing usable."""
        ...

    def should_forward(
        self, slack_message: dict[str, Any], project_config: ProjectConfigLike
    ) -> bool:
        """Return False to drop the message (e.g. filter by label/component/status)."""
        ...

    def extract_ticket_id(self, slack_message: dict[str, Any]) -> str | None:
        """Return a stable ticket identifier (e.g. issue number), or None if not found."""
        ...


def get_parser(platform: str) -> MessageParser:
    """Load and return the MessageParser for the given platform via dynamic import.

    Args:
        platform: Platform identifier, e.g. 'github' or 'azure_devops'.

    Returns:
        MessageParser implementation for the platform.

    Raises:
        ValueError: If the platform is unknown or the module does not expose get_parser().
    """
    normalized = platform.strip().lower()
    if not normalized:
        logger.error("Platform name is empty")
        raise ValueError("Platform name cannot be empty")
    module_name = f"router.platforms.{normalized}"
    try:
        mod = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        logger.error("Unknown platform %r: no module %s", platform, module_name, exc_info=e)
        raise ValueError(f"Unknown platform: {platform!r}") from e
    if not hasattr(mod, "get_parser"):
        logger.error("Platform module %s has no get_parser()", module_name)
        raise ValueError(f"Platform module {module_name!r} does not expose get_parser()")
    return cast(MessageParser, mod.get_parser())

"""Platform-specific message parsing for Slack events (GitHub, Azure DevOps)."""

from router.platforms.messaging import MessageParser, Platform, get_parser

__all__ = ["MessageParser", "Platform", "get_parser"]

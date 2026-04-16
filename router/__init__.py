"""Router package: Slack-to-agent relay with platform-specific message parsing."""

from router.router import (
    ProjectConfig,
    SessionRecord,
    load_log_level,
    load_projects,
    resolve_channels,
    spawn_engineer,
    supervise_slack,
)

__all__ = [
    "ProjectConfig",
    "SessionRecord",
    "load_log_level",
    "load_projects",
    "resolve_channels",
    "spawn_engineer",
    "supervise_slack",
]

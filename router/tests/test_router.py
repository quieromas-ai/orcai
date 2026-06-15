import asyncio
import json
import logging
import os
import sys
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from datetime import datetime as real_datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from router import (
    ProjectConfig,
    SessionRecord,
    load_log_level,
    load_projects,
    resolve_channels,
    spawn_engineer,
    supervise_slack,
)
from router.router import (
    SLACK_MAX_LENGTH,
    DailyDirectoryFileHandler,
    _add_reaction,
    _append_to_inbox,
    _extract_result,
    _inbox_messages,
    _is_unchanged_message_edit,
    _outbox_path_for,
    _resolve_mentions,
    _session_id_from_log,
    _try_route_event,
    fetch_thread_context,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def write_router_config(path: Path, workspaces: list[str], log_level: str = "INFO") -> str:
    cfg = {"log_level": log_level, "workspaces": workspaces}
    config_file = path / "router_config.yaml"
    config_file.write_text(yaml.dump(cfg))
    return str(config_file)


def write_project_config(workspace: Path, **overrides: object) -> str:
    workspace.mkdir(parents=True, exist_ok=True)
    raw: dict[object, object] = {
        "project": {
            "name": overrides.get("name", "my-project"),
            "platform": overrides.get("platform", "github"),
        },
        "slack": {"channels": overrides.get("channels", ["#general"])},
        "agent": {
            "name": overrides.get("agent_name", "engineer"),
            "backend": overrides.get("backend", "claude"),
            "model": overrides.get("model", "claude-sonnet-4-6"),
            "timeout_minutes": overrides.get("timeout_minutes", 60),
        },
    }
    config_file = workspace / "config.yaml"
    config_file.write_text(yaml.dump(raw))
    return str(workspace)


def make_project(**kwargs: object) -> ProjectConfig:
    defaults: dict[str, object] = {
        "name": "proj",
        "workspace": "/tmp/proj",
        "channels": ["general"],
    }
    defaults.update(kwargs)
    return ProjectConfig(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# load_log_level
# ---------------------------------------------------------------------------


def test_load_log_level_info(tmp_path: Path) -> None:
    cfg = write_router_config(tmp_path, [], log_level="INFO")
    assert load_log_level(cfg) == logging.INFO


def test_load_log_level_debug(tmp_path: Path) -> None:
    cfg = write_router_config(tmp_path, [], log_level="DEBUG")
    assert load_log_level(cfg) == logging.DEBUG


def test_load_log_level_unknown_falls_back(tmp_path: Path) -> None:
    cfg = write_router_config(tmp_path, [], log_level="BOGUS")
    assert load_log_level(cfg) == logging.INFO


def test_load_log_level_missing_key(tmp_path: Path) -> None:
    config_file = tmp_path / "router_config.yaml"
    config_file.write_text(yaml.dump({"workspaces": []}))
    assert load_log_level(str(config_file)) == logging.INFO


# ---------------------------------------------------------------------------
# load_projects
# ---------------------------------------------------------------------------


def test_load_projects_single_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    write_project_config(workspace, name="myproj", channels=["#dev"])
    cfg = write_router_config(tmp_path, [str(workspace)])
    projects = load_projects(cfg)
    assert len(projects) == 1
    p = projects[0]
    assert p.name == "myproj"
    assert p.workspace == str(workspace)
    assert p.channels == ["dev"]  # # stripped


def test_load_projects_channel_hash_stripped(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    write_project_config(workspace, channels=["#alpha", "beta", "#gamma"])
    cfg = write_router_config(tmp_path, [str(workspace)])
    projects = load_projects(cfg)
    assert projects[0].channels == ["alpha", "beta", "gamma"]


def test_load_projects_missing_config_skipped(tmp_path: Path) -> None:
    missing = tmp_path / "no-config"
    missing.mkdir()
    workspace = tmp_path / "valid"
    write_project_config(workspace, name="valid")
    cfg = write_router_config(tmp_path, [str(missing), str(workspace)])
    projects = load_projects(cfg)
    assert len(projects) == 1
    assert projects[0].name == "valid"


def test_load_projects_defaults_applied(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "config.yaml").write_text(yaml.dump({"project": {}, "slack": {}, "agent": {}}))
    cfg = write_router_config(tmp_path, [str(workspace)])
    projects = load_projects(cfg)
    assert len(projects) == 1
    p = projects[0]
    assert p.platform == "github"
    assert p.agent_name == "engineer"
    assert p.backend == "claude"
    assert p.model == "claude-sonnet-4-6"
    assert p.timeout_minutes == 60


def test_load_projects_platform_from_config(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    write_project_config(workspace, name="ado", platform="azure_devops")
    cfg = write_router_config(tmp_path, [str(workspace)])
    projects = load_projects(cfg)
    assert len(projects) == 1
    assert projects[0].platform == "azure_devops"


def test_load_projects_no_workspaces(tmp_path: Path) -> None:
    cfg = write_router_config(tmp_path, [])
    assert load_projects(cfg) == []


# ---------------------------------------------------------------------------
# resolve_channels
# ---------------------------------------------------------------------------


def _slack_page(channels: list[dict[str, str]], next_cursor: str = "") -> dict[str, object]:
    return {
        "channels": channels,
        "response_metadata": {"next_cursor": next_cursor},
    }


async def test_resolve_channels_empty_projects() -> None:
    client = AsyncMock()
    result = await resolve_channels(client, [])
    assert result == {}
    client.conversations_list.assert_not_called()


async def test_resolve_channels_single_page() -> None:
    client = AsyncMock()
    client.conversations_list.return_value = _slack_page([{"name": "general", "id": "C001"}])
    project = make_project(channels=["general"])
    result = await resolve_channels(client, [project])
    assert result == {"C001": project}


async def test_resolve_channels_paginated() -> None:
    client = AsyncMock()
    client.conversations_list.side_effect = [
        _slack_page([{"name": "alpha", "id": "C001"}], next_cursor="tok1"),
        _slack_page([{"name": "beta", "id": "C002"}]),
    ]
    p1 = make_project(name="p1", channels=["alpha"])
    p2 = make_project(name="p2", channels=["beta"])
    result = await resolve_channels(client, [p1, p2])
    assert result == {"C001": p1, "C002": p2}
    assert client.conversations_list.call_count == 2


async def test_resolve_channels_channel_not_found() -> None:
    client = AsyncMock()
    client.conversations_list.return_value = _slack_page([{"name": "other", "id": "C999"}])
    project = make_project(channels=["missing"])
    result = await resolve_channels(client, [project])
    assert result == {}


# ---------------------------------------------------------------------------
# _resolve_mentions
# ---------------------------------------------------------------------------


def test_resolve_mentions_replaces_known_name() -> None:
    result = _resolve_mentions("Done! @reviewer please check", {"reviewer": "U123"})
    assert result == "Done! <@U123> please check"


def test_resolve_mentions_leaves_unknown_name_unchanged() -> None:
    result = _resolve_mentions("ping @stranger", {"reviewer": "U123"})
    assert result == "ping @stranger"


def test_resolve_mentions_replaces_multiple_names() -> None:
    result = _resolve_mentions(
        "@engineer opened PR, @reviewer please check",
        {"engineer": "U001", "reviewer": "U002"},
    )
    assert result == "<@U001> opened PR, <@U002> please check"


def test_resolve_mentions_empty_map_is_noop() -> None:
    text = "ping @engineer"
    assert _resolve_mentions(text, {}) == text


def test_resolve_mentions_no_at_signs_is_noop() -> None:
    text = "no mentions here"
    assert _resolve_mentions(text, {"engineer": "U001"}) == text


def test_resolve_mentions_same_name_twice() -> None:
    result = _resolve_mentions("@engineer and @engineer again", {"engineer": "U001"})
    assert result == "<@U001> and <@U001> again"


def test_resolve_mentions_existing_slack_id_not_double_processed() -> None:
    # <@U999> already in text — the U999 token is not in the map so it stays untouched
    result = _resolve_mentions("<@U999> see also @reviewer", {"reviewer": "U123"})
    assert result == "<@U999> see also <@U123>"


# ---------------------------------------------------------------------------
# spawn_engineer
# ---------------------------------------------------------------------------


async def _async_lines(data: bytes) -> AsyncIterator[bytes]:
    for line in data.splitlines(keepends=True):
        yield line


def _make_proc(stdout: bytes, returncode: int, timeout: bool = False) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.stdout = _async_lines(stdout)
    proc.wait = AsyncMock()
    return proc


def _stream_json(result_text: str) -> bytes:
    lines = [
        json.dumps({"type": "text", "text": "intermediate"}),
        json.dumps({"type": "result", "result": result_text}),
    ]
    return "\n".join(lines).encode()


async def test_spawn_engineer_success_posts_result(tmp_path: Path) -> None:
    stdout = _stream_json("All done!")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(project, "do something", "C001", "12345.0", slack_client, semaphore)

    # The result is now streamed live; there is no separate "picked up" text post.
    assert slack_client.chat_postMessage.call_count == 1
    result_kwargs = slack_client.chat_postMessage.call_args_list[0].kwargs
    assert result_kwargs["text"] == "All done!"
    assert result_kwargs["thread_ts"] == "12345.0"


async def test_spawn_engineer_resolves_mentions_in_result(tmp_path: Path) -> None:
    stdout = _stream_json("PR opened, @reviewer please check")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path), mentions={"reviewer": "U999"})
    semaphore = asyncio.Semaphore(1)

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(project, "do something", "C001", "12345.0", slack_client, semaphore)

    result_kwargs = slack_client.chat_postMessage.call_args_list[0].kwargs
    assert result_kwargs["text"] == "PR opened, <@U999> please check"


async def test_spawn_engineer_does_not_self_ack(tmp_path: Path) -> None:
    # The pickup reaction is emitted by the routing layer (_try_route_event), NOT by
    # spawn_engineer. spawn_engineer must not react on its own, otherwise a routed message
    # would be acked twice (and the drain re-spawn would re-ack an already-acked message).
    stdout = _stream_json("done")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path), agent_name="mybot")
    semaphore = asyncio.Semaphore(1)

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(
            project, "do something", "C001", "99.0", slack_client, semaphore, event_ts="trig.1"
        )

    slack_client.reactions_add.assert_not_called()
    posts = [c.kwargs.get("text", "") for c in slack_client.chat_postMessage.call_args_list]
    assert all("Picked up" not in t for t in posts)


async def test_spawn_engineer_pickup_message_no_thread_ts(tmp_path: Path) -> None:
    stdout = _stream_json("done")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(project, "do something", "C001", None, slack_client, semaphore)

    pickup_kwargs = slack_client.chat_postMessage.call_args_list[0].kwargs
    assert "thread_ts" not in pickup_kwargs


async def test_spawn_engineer_sets_inbox_path_on_record(tmp_path: Path) -> None:
    stdout = _stream_json_with_session("done", "uuid-7")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path), agent_name="engineer", follow_thread=True)
    semaphore = asyncio.Semaphore(1)
    sessions: dict[str, Any] = {}
    session_by_thread: dict[str, str] = {}
    session_counter: dict[str, int] = {}

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(
            project,
            "do something",
            "C001",
            "99.0",
            slack_client,
            semaphore,
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter=session_counter,
        )

    rec = next(iter(sessions.values()))
    assert rec.inbox_path.endswith(".jsonl")
    assert str(tmp_path) in rec.inbox_path
    assert "/.orcai/inbox/" in rec.inbox_path


async def test_spawn_engineer_no_banner_or_pickup_without_tracking(tmp_path: Path) -> None:
    stdout = _stream_json("done")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path), agent_name="engineer")
    semaphore = asyncio.Semaphore(1)

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(project, "do something", "C001", "99.0", slack_client, semaphore)

    # Without session tracking: only the streamed result, no pickup text, no session banner.
    posts = [c.kwargs.get("text", "") for c in slack_client.chat_postMessage.call_args_list]
    assert posts == ["done"]


async def test_spawn_engineer_log_written_to_workspace(tmp_path: Path) -> None:
    stdout = _stream_json("done")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(project, "do something", "C001", None, slack_client, semaphore)

    log_files = list((tmp_path / "logs").rglob("*.log"))
    assert len(log_files) == 1
    assert log_files[0].parent.parent == tmp_path / "logs"


async def test_spawn_engineer_no_output_posts_fallback(tmp_path: Path) -> None:
    stdout = json.dumps({"type": "text", "text": "nothing"}).encode()
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(project, "do something", "C001", None, slack_client, semaphore)

    text = slack_client.chat_postMessage.call_args.kwargs["text"]
    assert "no output" in text


async def test_spawn_engineer_failure_posts_error(tmp_path: Path) -> None:
    proc = _make_proc(b"", returncode=1)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(project, "do something", "C001", None, slack_client, semaphore)

    text = slack_client.chat_postMessage.call_args.kwargs["text"]
    assert "exit code 1" in text


async def test_spawn_engineer_truncates_long_result(tmp_path: Path) -> None:
    long_text = "x" * 4000
    stdout = _stream_json(long_text)
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(project, "do something", "C001", None, slack_client, semaphore)

    text = slack_client.chat_postMessage.call_args.kwargs["text"]
    assert "truncated" in text
    assert len(text) <= 3930


async def test_spawn_engineer_timeout_kills_and_posts_error(tmp_path: Path) -> None:
    proc = _make_proc(b"", returncode=-1, timeout=True)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path), timeout_minutes=1)
    semaphore = asyncio.Semaphore(1)

    with (
        patch("router.router.asyncio.create_subprocess_exec", return_value=proc),
        patch("router.router.asyncio.wait_for", side_effect=asyncio.TimeoutError),
    ):
        await spawn_engineer(project, "do something", "C001", None, slack_client, semaphore)

    proc.kill.assert_called_once()
    text = slack_client.chat_postMessage.call_args.kwargs["text"]
    assert "exit code" in text


async def test_spawn_engineer_unexpected_error_kills_and_posts_error(tmp_path: Path) -> None:
    proc = _make_proc(b"", returncode=-1)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)

    with (
        patch("router.router.asyncio.create_subprocess_exec", return_value=proc),
        patch(
            "router.router.asyncio.wait_for",
            side_effect=ValueError("Separator is found, but chunk is longer than limit"),
        ),
    ):
        await spawn_engineer(project, "do something", "C001", None, slack_client, semaphore)

    proc.kill.assert_called_once()
    text = slack_client.chat_postMessage.call_args.kwargs["text"]
    assert "exit code" in text


async def test_spawn_engineer_unexpected_error_releases_ticket_lock(tmp_path: Path) -> None:
    proc = _make_proc(b"", returncode=-1)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)
    active_tickets: dict[str, str] = {"myproject:537": "ts-1"}

    with (
        patch("router.router.asyncio.create_subprocess_exec", return_value=proc),
        patch(
            "router.router.asyncio.wait_for",
            side_effect=ValueError("chunk too long"),
        ),
    ):
        await spawn_engineer(
            project,
            "do something",
            "C001",
            None,
            slack_client,
            semaphore,
            ticket_key="myproject:537",
            active_tickets=active_tickets,
        )

    assert "myproject:537" not in active_tickets


# ---------------------------------------------------------------------------
# supervise_slack
# ---------------------------------------------------------------------------


async def test_supervise_slack_exits_if_already_shutdown() -> None:
    handler = AsyncMock()
    shutdown_event = asyncio.Event()
    shutdown_event.set()

    await supervise_slack(handler, shutdown_event)

    handler.start_async.assert_not_called()


async def test_supervise_slack_cancelled_error_exits_cleanly() -> None:
    handler = AsyncMock()
    handler.start_async.side_effect = asyncio.CancelledError
    shutdown_event = asyncio.Event()

    await supervise_slack(handler, shutdown_event)

    handler.start_async.assert_called_once()


async def test_supervise_slack_restarts_on_exception() -> None:
    call_count = 0
    shutdown_event = asyncio.Event()

    async def flaky_start() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")
        shutdown_event.set()

    handler = AsyncMock()
    handler.start_async.side_effect = flaky_start

    with patch.object(sys.modules["asyncio"], "sleep", new_callable=AsyncMock):
        await supervise_slack(handler, shutdown_event)

    assert call_count == 2


# ---------------------------------------------------------------------------
# ProjectConfig dataclass
# ---------------------------------------------------------------------------


def test_project_config_defaults() -> None:
    p = ProjectConfig(name="x", workspace="/x", channels=[])
    assert p.platform == "github"
    assert p.agent_name == "engineer"
    assert p.backend == "claude"
    assert p.model == "claude-sonnet-4-6"
    assert p.timeout_minutes == 60


def test_project_config_custom_values() -> None:
    p = ProjectConfig(
        name="x",
        workspace="/x",
        channels=["a"],
        agent_name="ops",
        backend="cursor",
        model="gpt-4",
        timeout_minutes=30,
    )
    assert p.agent_name == "ops"
    assert p.backend == "cursor"
    assert p.model == "gpt-4"
    assert p.timeout_minutes == 30


# ---------------------------------------------------------------------------
# _is_unchanged_message_edit
# ---------------------------------------------------------------------------


def _channel_map(channel: str = "C001", platform: str = "github") -> dict[str, ProjectConfig]:
    return {channel: make_project(platform=platform)}


def _attachment_msg(pretext: str, body: str = "", color: str = "36a64f") -> dict[str, object]:
    return {
        "text": "",
        "attachments": [{"pretext": pretext, "text": body, "color": color}],
    }


def test_unchanged_edit_same_text_is_skipped() -> None:
    # GitHub changes only the attachment colour (green→purple) on PR merge.
    # The pretext/body text is identical in both inner and previous messages.
    inner = _attachment_msg("Pull request opened by github-user", "Some PR body", color="6f42c1")
    prev = _attachment_msg("Pull request opened by github-user", "Some PR body", color="36a64f")
    assert _is_unchanged_message_edit(inner, prev, _channel_map(), "C001") is True


def test_changed_text_is_not_skipped() -> None:
    # GitHub edited the issue body — content differs.
    inner = _attachment_msg("Issue updated by github-user", "New description")
    prev = _attachment_msg("Issue updated by github-user", "Old description")
    assert _is_unchanged_message_edit(inner, prev, _channel_map(), "C001") is False


def test_thread_broadcast_is_never_skipped() -> None:
    # The actual merge notification arrives as a thread_broadcast — always process it.
    inner = {
        "subtype": "thread_broadcast",
        "text": "",
        "attachments": [{"pretext": "Pull request merged by github-user", "text": ""}],
    }
    prev = _attachment_msg("Pull request merged by github-user", "")
    assert _is_unchanged_message_edit(inner, prev, _channel_map(), "C001") is False


def test_unknown_channel_is_not_skipped() -> None:
    # If the channel isn't in the map we can't determine the platform — fall through.
    inner = _attachment_msg("Pull request opened by github-user", "body", color="6f42c1")
    prev = _attachment_msg("Pull request opened by github-user", "body", color="36a64f")
    assert _is_unchanged_message_edit(inner, prev, _channel_map(), "C_UNKNOWN") is False


def test_empty_previous_message_is_not_skipped() -> None:
    # No previous_message in the event (edge case) — don't drop it.
    inner = _attachment_msg("Pull request opened by github-user", "body")
    assert _is_unchanged_message_edit(inner, {}, _channel_map(), "C001") is False


# ---------------------------------------------------------------------------
# _try_route_event — deduplication via seen_ts deque
# ---------------------------------------------------------------------------


def _bot_event(ts: str, channel: str = "C001") -> dict[str, object]:
    """A minimal bot message event that passes all filters."""
    return {
        "channel": channel,
        "ts": ts,
        "bot_id": "B_GITHUB",
        "text": "",
        "attachments": [{"pretext": "PR opened by github-user", "text": "body"}],
    }


def _mock_create_task(coro: object) -> MagicMock:
    """Close the coroutine so Python doesn't warn about it never being awaited."""
    if hasattr(coro, "close"):
        coro.close()
    return MagicMock()


async def test_duplicate_ts_spawns_engineer_only_once() -> None:
    channel_map = {"C001": make_project(platform="github")}
    seen_ts: deque[str] = deque(maxlen=1000)
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)
    tasks: set[asyncio.Task[None]] = set()
    # Event with no ticket ID (no attachments with title_link) — falls back to ts dedup
    event = {"channel": "C001", "ts": "1234567890.000001", "bot_id": "B_GITHUB", "text": "hello"}

    with patch(
        "router.router.asyncio.create_task", side_effect=_mock_create_task
    ) as mock_create_task:
        await _try_route_event(
            event,
            event,
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            {},
        )
        await _try_route_event(
            event,
            event,
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            {},
        )

    mock_create_task.assert_called_once()


async def test_seen_ts_deque_does_not_exceed_maxlen() -> None:
    channel_map = {"C001": make_project(platform="github")}
    seen_ts: deque[str] = deque(maxlen=5)
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(10)
    tasks: set[asyncio.Task[None]] = set()

    with patch(
        "router.router.asyncio.create_task", side_effect=_mock_create_task
    ) as mock_create_task:
        for i in range(7):
            # No ticket ID — plain text messages fall back to ts dedup
            event = {
                "channel": "C001",
                "ts": f"10000000{i}.000001",
                "bot_id": "B_GITHUB",
                "text": "hello",
            }
            await _try_route_event(
                event,
                event,
                channel_map,
                "B_SELF",
                "U_SELF",
                slack_client,
                semaphore,
                tasks,
                seen_ts,
                {},
            )

    assert len(seen_ts) == 5
    assert mock_create_task.call_count == 7


# ---------------------------------------------------------------------------
# _try_route_event — cross-agent bot filtering (all_bot_ids)
# ---------------------------------------------------------------------------


async def test_message_from_sibling_agent_is_dropped() -> None:
    """A 'Picked up by *team_leader*...' message (bot_id=B_TEAM_LEADER) must
    not trigger the engineer when B_TEAM_LEADER is in all_bot_ids."""
    channel_map = {"C001": make_project(platform="github")}
    seen_ts: deque[str] = deque(maxlen=1000)
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)
    tasks: set[asyncio.Task[None]] = set()

    sibling_bot_id = "B_TEAM_LEADER"
    own_bot_id = "B_ENGINEER"
    all_bots = {own_bot_id, sibling_bot_id}

    event = {
        "channel": "C001",
        "ts": "111.0",
        "bot_id": sibling_bot_id,
        "text": "Picked up by *team_leader*...",
    }

    with patch(
        "router.router.asyncio.create_task", side_effect=_mock_create_task
    ) as mock_create_task:
        await _try_route_event(
            event,
            event,
            channel_map,
            own_bot_id,
            "U_ENGINEER",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            {},
            all_bot_ids=all_bots,
        )

    mock_create_task.assert_not_called()


async def test_app_mention_from_sibling_agent_is_forwarded() -> None:
    """An app_mention from a sibling bot (e.g. team_leader → @engineer) must NOT
    be dropped by the all_bot_ids filter — it is intentional cross-agent delegation."""
    channel_map = {"C001": make_project(platform="azure_devops")}
    seen_ts: deque[str] = deque(maxlen=1000)
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)
    tasks: set[asyncio.Task[None]] = set()

    sibling_bot_id = "B_TEAM_LEADER"
    own_bot_id = "B_ENGINEER"
    all_bots = {own_bot_id, sibling_bot_id}

    event = {
        "channel": "C001",
        "ts": "333.0",
        "bot_id": sibling_bot_id,
        "text": "<@U_ENGINEER> Work item #633 — implement DELETE /missions/...",
    }

    with patch(
        "router.router.asyncio.create_task", side_effect=_mock_create_task
    ) as mock_create_task:
        await _try_route_event(
            event,
            event,
            channel_map,
            own_bot_id,
            "U_ENGINEER",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            {},
            is_app_mention=True,
            all_bot_ids=all_bots,
        )

    mock_create_task.assert_called_once()


async def test_message_from_external_bot_still_forwarded() -> None:
    """A bot message from an external integration (e.g. GitHub) must still
    reach the engineer when its bot_id is NOT in all_bot_ids."""
    channel_map = {"C001": make_project(platform="github")}
    seen_ts: deque[str] = deque(maxlen=1000)
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)
    tasks: set[asyncio.Task[None]] = set()

    all_bots = {"B_ENGINEER", "B_TEAM_LEADER"}
    event = _bot_event("222.0")  # bot_id="B_GITHUB", not in all_bots

    with patch(
        "router.router.asyncio.create_task", side_effect=_mock_create_task
    ) as mock_create_task:
        await _try_route_event(
            event,
            event,
            channel_map,
            "B_ENGINEER",
            "U_ENGINEER",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            {},
            all_bot_ids=all_bots,
        )

    mock_create_task.assert_called_once()


# ---------------------------------------------------------------------------
# _try_route_event — per-ticket in-memory locking
# ---------------------------------------------------------------------------


def _issue_event(ts: str, issue_num: str, channel: str = "C001") -> dict[str, object]:
    """A bot message event referencing a specific GitHub issue."""
    return {
        "channel": channel,
        "ts": ts,
        "bot_id": "B_GITHUB",
        "text": "",
        "attachments": [
            {
                "pretext": f"Issue #{issue_num} updated",
                "text": "body",
                "title_link": f"https://github.com/org/repo/issues/{issue_num}",
            }
        ],
    }


async def test_second_event_for_same_ticket_while_active_is_dropped() -> None:
    channel_map = {"C001": make_project(platform="github")}
    seen_ts: deque[str] = deque(maxlen=1000)
    active_tickets: dict[str, str] = {}
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)
    tasks: set[asyncio.Task[None]] = set()

    with patch(
        "router.router.asyncio.create_task", side_effect=_mock_create_task
    ) as mock_create_task:
        await _try_route_event(
            _issue_event("ts1", "42"),
            _issue_event("ts1", "42"),
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            active_tickets,
        )
        # Second event for same ticket — different ts but ticket still active
        await _try_route_event(
            _issue_event("ts2", "42"),
            _issue_event("ts2", "42"),
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            active_tickets,
        )

    mock_create_task.assert_called_once()


async def test_same_ticket_id_in_different_projects_both_proceed() -> None:
    proj_a = make_project(name="proj-a", platform="github")
    proj_b = make_project(name="proj-b", platform="github")
    channel_map = {"C001": proj_a, "C002": proj_b}
    seen_ts: deque[str] = deque(maxlen=1000)
    active_tickets: dict[str, str] = {}
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)
    tasks: set[asyncio.Task[None]] = set()

    with patch(
        "router.router.asyncio.create_task", side_effect=_mock_create_task
    ) as mock_create_task:
        await _try_route_event(
            _issue_event("ts1", "13", channel="C001"),
            _issue_event("ts1", "13", channel="C001"),
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            active_tickets,
        )
        await _try_route_event(
            _issue_event("ts2", "13", channel="C002"),
            _issue_event("ts2", "13", channel="C002"),
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            active_tickets,
        )

    assert mock_create_task.call_count == 2


async def test_lock_released_after_spawn_engineer_finishes(tmp_path: Path) -> None:
    stdout = _stream_json("done")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)
    active_tickets: dict[str, str] = {"proj:42": "ts1"}

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(
            project,
            "do something",
            "C001",
            None,
            slack_client,
            semaphore,
            ticket_key="proj:42",
            active_tickets=active_tickets,
        )

    assert "proj:42" not in active_tickets


# ---------------------------------------------------------------------------
# DailyDirectoryFileHandler
# ---------------------------------------------------------------------------


def test_daily_handler_creates_dated_directory(tmp_path: Path) -> None:
    handler = DailyDirectoryFileHandler(str(tmp_path))
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
    handler.emit(record)
    handler.close()
    log_files = list(tmp_path.rglob("router.log"))
    assert len(log_files) == 1
    assert log_files[0].read_text().strip() == "hello"


def test_daily_handler_rotates_on_date_change(tmp_path: Path) -> None:
    handler = DailyDirectoryFileHandler(str(tmp_path))
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("test", logging.INFO, "", 0, "day1", (), None)
    handler.emit(record)

    # Simulate date change by patching datetime.now to return a different day
    with patch("router.router.datetime") as mock_dt:
        mock_dt.now.return_value = real_datetime(2099, 12, 31, 12, 0, 0)
        record2 = logging.LogRecord("test", logging.INFO, "", 0, "day2", (), None)
        handler.emit(record2)
    handler.close()

    log_files = sorted(tmp_path.rglob("router.log"))
    assert len(log_files) == 2
    dates = {f.parent.name for f in log_files}
    assert "2099-12-31" in dates


# ---------------------------------------------------------------------------
# load_projects — multi-agent agents: schema
# ---------------------------------------------------------------------------


def write_multi_agent_config(workspace: Path, agents: list[dict[str, object]]) -> str:
    workspace.mkdir(parents=True, exist_ok=True)
    raw: dict[str, object] = {
        "project": {"name": "multi-proj", "platform": "github"},
        "agents": agents,
    }
    (workspace / "config.yaml").write_text(yaml.dump(raw))
    return str(workspace)


def test_load_projects_multi_agent_schema_expands_to_multiple_configs(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    write_multi_agent_config(
        workspace,
        agents=[
            {
                "name": "engineer",
                "platform": "github",
                "slack": {
                    "channels": ["#eng-chan"],
                    "bot_token_env": "ENG_BOT",
                    "app_token_env": "ENG_APP",
                },
                "backend": "claude",
                "model": "claude-sonnet-4-6",
                "timeout_minutes": 30,
            },
            {
                "name": "pm",
                "platform": "slack",
                "slack": {
                    "channels": ["#pm-chan"],
                    "bot_token_env": "PM_BOT",
                    "app_token_env": "PM_APP",
                },
            },
        ],
    )
    # Write workspace .env with tokens
    (workspace / ".env").write_text(
        "ENG_BOT=xoxb-eng\nENG_APP=xapp-eng\nPM_BOT=xoxb-pm\nPM_APP=xapp-pm\n"
    )
    cfg = write_router_config(tmp_path, [str(workspace)])
    projects = load_projects(cfg)

    assert len(projects) == 2
    eng = next(p for p in projects if p.agent_name == "engineer")
    pm = next(p for p in projects if p.agent_name == "pm")

    assert eng.channels == ["eng-chan"]
    assert eng.platform == "github"
    assert eng.slack_bot_token == "xoxb-eng"
    assert eng.slack_app_token == "xapp-eng"
    assert eng.timeout_minutes == 30

    assert pm.channels == ["pm-chan"]
    assert pm.platform == "slack"
    assert pm.slack_bot_token == "xoxb-pm"
    assert pm.slack_app_token == "xapp-pm"
    assert pm.timeout_minutes == 60  # default


def test_load_projects_old_schema_uses_env_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "proj"
    write_project_config(workspace, name="old-proj", channels=["#dev"])
    cfg = write_router_config(tmp_path, [str(workspace)])
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-global")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-global")
    projects = load_projects(cfg)
    assert len(projects) == 1
    assert projects[0].slack_bot_token == "xoxb-global"
    assert projects[0].slack_app_token == "xapp-global"


# ---------------------------------------------------------------------------
# fetch_thread_context
# ---------------------------------------------------------------------------


async def test_fetch_thread_context_returns_formatted_history() -> None:
    ts_root = "1700000000.000001"
    ts_reply = "1700000060.000002"
    ts_trigger = "1700000120.000003"

    slack_client = AsyncMock()
    slack_client.conversations_replies.return_value = {
        "messages": [
            {"ts": ts_root, "user": "U001", "text": "What is the status?"},
            {"ts": ts_reply, "user": "U002", "text": "Still working on it."},
            {"ts": ts_trigger, "user": "U001", "text": "Can you finish today?"},
        ]
    }
    slack_client.users_info.return_value = {
        "user": {"profile": {"display_name": "alice", "real_name": "Alice"}}
    }

    user_cache: dict[str, str] = {}
    result = await fetch_thread_context(
        slack_client, "C001", ts_root, ts_trigger, "channel", user_cache
    )

    assert "=== Conversation History ===" in result
    assert "What is the status?" in result
    assert "Still working on it." in result
    # Triggering message excluded
    assert "Can you finish today?" not in result


async def test_fetch_thread_context_returns_empty_when_only_trigger_message() -> None:
    ts_trigger = "1700000000.000001"
    slack_client = AsyncMock()
    slack_client.conversations_replies.return_value = {
        "messages": [{"ts": ts_trigger, "user": "U001", "text": "Hello"}]
    }
    result = await fetch_thread_context(slack_client, "C001", ts_trigger, ts_trigger, "channel", {})
    assert result == ""


async def test_fetch_thread_context_dm_uses_history_api() -> None:
    slack_client = AsyncMock()
    # conversations.history returns newest-first (Slack API default)
    slack_client.conversations_history.return_value = {
        "messages": [
            {"ts": "1700000120.000003", "user": "U001", "text": "Trigger msg"},
            {"ts": "1700000060.000002", "user": "U001", "text": "Earlier message"},
        ]
    }
    slack_client.users_info.return_value = {
        "user": {"profile": {"display_name": "bob", "real_name": "Bob"}}
    }

    result = await fetch_thread_context(slack_client, "D001", None, "1700000120.000003", "im", {})

    assert "=== Conversation History ===" in result
    assert "Earlier message" in result
    slack_client.conversations_history.assert_called_once()
    slack_client.conversations_replies.assert_not_called()


async def test_fetch_thread_context_sorts_messages_by_ts() -> None:
    """Output order is chronological even if the API returns messages shuffled."""
    ts_root = "1700000000.000001"
    ts_reply = "1700000060.000002"
    ts_trigger = "1700000120.000003"

    slack_client = AsyncMock()
    slack_client.conversations_replies.return_value = {
        "messages": [
            {"ts": ts_trigger, "user": "U001", "text": "Can you finish today?"},
            {"ts": ts_reply, "user": "U002", "text": "Still working on it."},
            {"ts": ts_root, "user": "U001", "text": "What is the status?"},
        ]
    }
    slack_client.users_info.return_value = {
        "user": {"profile": {"display_name": "alice", "real_name": "Alice"}}
    }

    result = await fetch_thread_context(slack_client, "C001", ts_root, ts_trigger, "channel", {})

    assert "Can you finish today?" not in result
    assert result.index("What is the status?") < result.index("Still working on it.")


async def test_fetch_thread_context_dm_sorts_by_ts_after_excluding_trigger() -> None:
    slack_client = AsyncMock()
    slack_client.conversations_history.return_value = {
        "messages": [
            {"ts": "1700000120.000003", "user": "U001", "text": "Trigger"},
            {"ts": "1700000090.000002", "user": "U001", "text": "Middle"},
            {"ts": "1700000060.000001", "user": "U001", "text": "First"},
        ]
    }
    slack_client.users_info.return_value = {
        "user": {"profile": {"display_name": "bob", "real_name": "Bob"}}
    }

    result = await fetch_thread_context(slack_client, "D001", None, "1700000120.000003", "im", {})

    assert result.index("First") < result.index("Middle")


async def test_fetch_thread_context_uses_user_cache() -> None:
    ts_root = "1700000000.000001"
    ts_trigger = "1700000060.000002"
    slack_client = AsyncMock()
    slack_client.conversations_replies.return_value = {
        "messages": [
            {"ts": ts_root, "user": "U001", "text": "Hello"},
        ]
    }
    slack_client.users_info.return_value = {"user": {"profile": {"display_name": "charlie"}}}

    user_cache: dict[str, str] = {}
    await fetch_thread_context(slack_client, "C001", ts_root, ts_trigger, "channel", user_cache)
    await fetch_thread_context(slack_client, "C001", ts_root, ts_trigger, "channel", user_cache)

    # users_info called only once despite two fetches — cache hit on second
    slack_client.users_info.assert_called_once()


async def test_fetch_thread_context_no_thread_ts_channel_returns_empty() -> None:
    slack_client = AsyncMock()
    result = await fetch_thread_context(slack_client, "C001", None, "ts1", "channel", {})
    assert result == ""


# ---------------------------------------------------------------------------
# _try_route_event — DM routing via project_override
# ---------------------------------------------------------------------------


async def test_dm_event_routes_to_project_override() -> None:
    """DM events use project_override when channel not in channel_map."""
    dm_project = make_project(name="dm-proj", platform="slack")
    channel_map: dict[str, ProjectConfig] = {}  # DM channel not in map
    seen_ts: deque[str] = deque(maxlen=1000)
    active_tickets: dict[str, str] = {}
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)
    tasks: set[asyncio.Task[None]] = set()

    dm_event = {
        "channel": "D001",
        "channel_type": "im",
        "ts": "1700000000.000001",
        "user": "U001",
        "text": "Can you help me?",
    }

    with patch(
        "router.router.asyncio.create_task", side_effect=_mock_create_task
    ) as mock_create_task:
        await _try_route_event(
            dm_event,
            dm_event,
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            active_tickets,
            is_app_mention=True,
            channel_type="im",
            project_override=dm_project,
        )

    mock_create_task.assert_called_once()


async def test_dm_event_dropped_without_project_override() -> None:
    """DM channel not in map and no override → dropped."""
    channel_map: dict[str, ProjectConfig] = {}
    seen_ts: deque[str] = deque(maxlen=1000)
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)
    tasks: set[asyncio.Task[None]] = set()

    dm_event = {"channel": "D001", "ts": "1700000000.000001", "user": "U001", "text": "hi"}

    with patch(
        "router.router.asyncio.create_task", side_effect=_mock_create_task
    ) as mock_create_task:
        await _try_route_event(
            dm_event,
            dm_event,
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            {},
        )

    mock_create_task.assert_not_called()


# ---------------------------------------------------------------------------
# _extract_result — tuple return
# ---------------------------------------------------------------------------


def test_extract_result_returns_session_id() -> None:
    line = json.dumps({"type": "result", "result": "done", "session_id": "uuid-123"})
    result_text, session_id = _extract_result(line)
    assert result_text == "done"
    assert session_id == "uuid-123"


def test_extract_result_no_session_id_returns_empty_string() -> None:
    line = json.dumps({"type": "result", "result": "done"})
    result_text, session_id = _extract_result(line)
    assert result_text == "done"
    assert session_id == ""


def test_extract_result_no_result_line_returns_empty_tuple() -> None:
    result_text, session_id = _extract_result("")
    assert result_text == ""
    assert session_id == ""


def test_extract_result_picks_last_result_line() -> None:
    lines = "\n".join(
        [
            json.dumps({"type": "result", "result": "first", "session_id": "s1"}),
            json.dumps({"type": "text", "text": "intermediate"}),
            json.dumps({"type": "result", "result": "last", "session_id": "s2"}),
        ]
    )
    result_text, session_id = _extract_result(lines)
    assert result_text == "last"
    assert session_id == "s2"


# ---------------------------------------------------------------------------
# _session_id_from_log
# ---------------------------------------------------------------------------


def test_session_id_from_log_reads_uuid(tmp_path: Path) -> None:
    log = tmp_path / "session-1.log"
    log.write_text(json.dumps({"type": "result", "result": "done", "session_id": "abc-xyz"}))
    assert _session_id_from_log(str(log)) == "abc-xyz"


def test_session_id_from_log_missing_file(tmp_path: Path) -> None:
    assert _session_id_from_log(str(tmp_path / "nonexistent.log")) == ""


def test_session_id_from_log_no_result_event(tmp_path: Path) -> None:
    log = tmp_path / "session-1.log"
    log.write_text(json.dumps({"type": "text", "text": "intermediate"}))
    assert _session_id_from_log(str(log)) == ""


# ---------------------------------------------------------------------------
# spawn_engineer — session tracking
# ---------------------------------------------------------------------------


def _stream_json_with_session(result_text: str, session_id: str) -> bytes:
    lines = [
        json.dumps({"type": "text", "text": "working"}),
        json.dumps({"type": "result", "result": result_text, "session_id": session_id}),
    ]
    return "\n".join(lines).encode()


async def test_spawn_engineer_log_path_uses_session_number(tmp_path: Path) -> None:
    stdout = _stream_json_with_session("done", "uuid-1")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)
    sessions: dict[str, SessionRecord] = {}
    session_by_thread: dict[str, str] = {}
    session_counter: dict[str, int] = {}

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(
            project,
            "do something",
            "C001",
            "ts1",
            slack_client,
            semaphore,
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter=session_counter,
        )

    log_files = list((tmp_path / "logs").rglob("*.log"))
    assert len(log_files) == 1
    assert log_files[0].name == "session-engineer-1.log"


async def test_spawn_engineer_creates_session_record(tmp_path: Path) -> None:
    stdout = _stream_json_with_session("done", "uuid-42")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)
    sessions: dict[str, SessionRecord] = {}
    session_by_thread: dict[str, str] = {}
    session_counter: dict[str, int] = {}

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(
            project,
            "do something",
            "C001",
            "ts1",
            slack_client,
            semaphore,
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter=session_counter,
        )

    assert len(sessions) == 1
    rec = next(iter(sessions.values()))
    assert rec.number == 1
    assert rec.claude_session_id == "uuid-42"
    assert rec.state == "idle"
    assert rec.channel_id == "C001"
    assert rec.thread_ts == "ts1"


async def test_spawn_engineer_posts_banner_on_success(tmp_path: Path) -> None:
    stdout = _stream_json_with_session("All done!", "uuid-99")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)
    sessions: dict[str, SessionRecord] = {}
    session_by_thread: dict[str, str] = {}
    session_counter: dict[str, int] = {}

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(
            project,
            "do something",
            "C001",
            "ts1",
            slack_client,
            semaphore,
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter=session_counter,
        )

    # streamed result + banner (no separate pickup text post)
    assert slack_client.chat_postMessage.call_count == 2
    banner_text: str = slack_client.chat_postMessage.call_args_list[1].kwargs["text"]
    assert "Session" in banner_text
    assert "/1" in banner_text
    assert "reply here" in banner_text


async def test_spawn_engineer_no_banner_on_resume(tmp_path: Path) -> None:
    # A resume (thread reply / DM #ref) carries resume_session_id → the "session saved" banner
    # must NOT be re-posted on every turn (it floods the thread); it shows once at creation only.
    stdout = _stream_json_with_session("continued", "uuid-99")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(
            project, "more", "C001", "ts1", slack_client, semaphore,
            resume_session_id="uuid-old",
            sessions={}, session_by_thread={}, session_counter={},
        )

    texts = [c.kwargs.get("text", "") for c in slack_client.chat_postMessage.call_args_list]
    assert not any("saved" in t for t in texts)  # no banner on resume
    assert slack_client.chat_postMessage.call_count == 1  # just the streamed result


async def test_spawn_engineer_no_banner_on_failure(tmp_path: Path) -> None:
    proc = _make_proc(b"", returncode=1)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)
    sessions: dict[str, SessionRecord] = {}
    session_by_thread: dict[str, str] = {}
    session_counter: dict[str, int] = {}

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(
            project,
            "do something",
            "C001",
            None,
            slack_client,
            semaphore,
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter=session_counter,
        )

    # error only (no pickup text, no banner)
    assert slack_client.chat_postMessage.call_count == 1


async def test_spawn_engineer_passes_resume_to_cmd(tmp_path: Path) -> None:
    stdout = _stream_json_with_session("continued", "new-uuid")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)
    captured_cmd: list[str] = []

    async def capture(*args: object, **kwargs: object) -> object:
        captured_cmd.extend(str(a) for a in args)
        return proc

    with patch("router.router.asyncio.create_subprocess_exec", side_effect=capture):
        await spawn_engineer(
            project,
            "continue work",
            "C001",
            None,
            slack_client,
            semaphore,
            resume_session_id="old-uuid",
        )

    assert "old-uuid" in captured_cmd


# ---------------------------------------------------------------------------
# _try_route_event — session resume paths
# ---------------------------------------------------------------------------


def _make_session(
    ref: str,
    project_name: str = "proj",
    channel_id: str = "C001",
    thread_ts: str = "ts-root",
    session_id: str = "uuid-resume",
    state: str = "idle",
    inbox_path: str = "",
) -> SessionRecord:
    date_str, num_str = ref.split("/", 1)
    return SessionRecord(
        number=int(num_str),
        date_str=date_str,
        project_name=project_name,
        claude_session_id=session_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        created_at=datetime(2026, 4, 5, tzinfo=timezone.utc),
        state=state,
        inbox_path=inbox_path,
    )


async def test_try_route_event_thread_reply_resumes() -> None:
    """Thread reply in a slack-platform channel resumes the session."""
    channel_map = {"C001": make_project(name="proj", platform="slack")}
    session_ref = "2026-04-05/1"
    sessions = {session_ref: _make_session(session_ref)}
    session_by_thread = {"C001:ts-root": session_ref}
    session_counter: dict[str, int] = {}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    # Human @mention reply in the thread
    event = {
        "channel": "C001",
        "ts": "ts-reply",
        "thread_ts": "ts-root",
        "user": "U001",
        "text": "<@U_SELF> what about edge cases?",
    }

    with patch("router.router.spawn_engineer", new_callable=AsyncMock) as mock_spawn:
        with patch("router.router.asyncio.create_task", side_effect=_mock_create_task):
            await _try_route_event(
                event,
                event,
                channel_map,
                "B_SELF",
                "U_SELF",
                slack_client,
                semaphore,
                tasks,
                seen_ts,
                {},
                sessions=sessions,
                session_by_thread=session_by_thread,
                session_counter=session_counter,
            )

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.kwargs["resume_session_id"] == "uuid-resume"


async def test_try_route_event_dm_thread_reply_resumes() -> None:
    """Thread reply in a DM channel auto-resumes the session."""
    dm_project = make_project(name="proj", platform="slack")
    session_ref = "2026-04-05/1"
    sessions = {session_ref: _make_session(session_ref, channel_id="D001", thread_ts="ts-root")}
    session_by_thread = {"D001:ts-root": session_ref}
    session_counter: dict[str, int] = {}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    event = {
        "channel": "D001",
        "channel_type": "im",
        "ts": "ts-reply",
        "thread_ts": "ts-root",
        "user": "U001",
        "text": "how are you?",
    }

    with patch("router.router.spawn_engineer", new_callable=AsyncMock) as mock_spawn:
        with patch("router.router.asyncio.create_task", side_effect=_mock_create_task):
            await _try_route_event(
                event,
                event,
                {},
                "B_SELF",
                "U_SELF",
                slack_client,
                semaphore,
                tasks,
                seen_ts,
                {},
                is_app_mention=True,
                channel_type="im",
                project_override=dm_project,
                sessions=sessions,
                session_by_thread=session_by_thread,
                session_counter=session_counter,
            )

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.kwargs["resume_session_id"] == "uuid-resume"


async def test_try_route_event_dm_fresh_message_no_resume() -> None:
    """A fresh DM (no thread_ts in event) does NOT auto-resume even if a session exists."""
    dm_project = make_project(name="proj", platform="slack")
    session_ref = "2026-04-05/1"
    sessions = {session_ref: _make_session(session_ref, channel_id="D001", thread_ts="ts-old")}
    session_by_thread = {"D001:ts-old": session_ref}
    session_counter: dict[str, int] = {}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    event = {
        "channel": "D001",
        "channel_type": "im",
        "ts": "ts-new",  # fresh message, no thread_ts
        "user": "U001",
        "text": "start fresh",
    }

    with patch("router.router.spawn_engineer", new_callable=AsyncMock) as mock_spawn:
        with patch("router.router.asyncio.create_task", side_effect=_mock_create_task):
            await _try_route_event(
                event,
                event,
                {},
                "B_SELF",
                "U_SELF",
                slack_client,
                semaphore,
                tasks,
                seen_ts,
                {},
                is_app_mention=True,
                channel_type="im",
                project_override=dm_project,
                sessions=sessions,
                session_by_thread=session_by_thread,
                session_counter=session_counter,
            )

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.kwargs["resume_session_id"] is None


async def test_try_route_event_thread_reply_no_resume_for_github_platform() -> None:
    """Thread replies in github-platform channels are NOT auto-resumed."""
    channel_map = {"C001": make_project(name="proj", platform="github")}
    session_ref = "2026-04-05/1"
    sessions = {session_ref: _make_session(session_ref)}
    session_by_thread = {"C001:ts-root": session_ref}
    session_counter: dict[str, int] = {}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    event = {
        "channel": "C001",
        "ts": "ts-reply",
        "thread_ts": "ts-root",
        "bot_id": "B_GITHUB",
        "text": "",
        "attachments": [{"pretext": "PR reviewed by github-user", "text": "body"}],
    }

    with patch("router.router.spawn_engineer", new_callable=AsyncMock) as mock_spawn:
        with patch("router.router.asyncio.create_task", side_effect=_mock_create_task):
            await _try_route_event(
                event,
                event,
                channel_map,
                "B_SELF",
                "U_SELF",
                slack_client,
                semaphore,
                tasks,
                seen_ts,
                {},
                sessions=sessions,
                session_by_thread=session_by_thread,
                session_counter=session_counter,
            )

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.kwargs["resume_session_id"] is None


async def test_try_route_event_thread_reply_resumes_for_azure_devops_platform() -> None:
    """Human @mention (app_mention) in an azure_devops-platform channel resumes the session."""
    channel_map = {"C001": make_project(name="proj", platform="azure_devops")}
    session_ref = "2026-04-05/1"
    sessions = {session_ref: _make_session(session_ref)}
    session_by_thread = {"C001:ts-root": session_ref}
    session_counter: dict[str, int] = {}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    event = {
        "channel": "C001",
        "ts": "ts-reply",
        "thread_ts": "ts-root",
        "user": "U001",
        "text": "<@U_SELF> what is the status?",
    }

    with patch("router.router.spawn_engineer", new_callable=AsyncMock) as mock_spawn:
        with patch("router.router.asyncio.create_task", side_effect=_mock_create_task):
            await _try_route_event(
                event,
                event,
                channel_map,
                "B_SELF",
                "U_SELF",
                slack_client,
                semaphore,
                tasks,
                seen_ts,
                {},
                is_app_mention=True,
                sessions=sessions,
                session_by_thread=session_by_thread,
                session_counter=session_counter,
            )

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.kwargs["resume_session_id"] == "uuid-resume"


async def test_try_route_event_dm_hash_prefix_resumes_from_memory() -> None:
    """DM with #date/N prefix resumes the in-memory session."""
    dm_project = make_project(name="proj", platform="slack")
    session_ref = "2026-04-05/1"
    sessions = {session_ref: _make_session(session_ref)}
    session_by_thread: dict[str, str] = {}
    session_counter: dict[str, int] = {}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    event = {
        "channel": "D001",
        "ts": "ts-new",
        "user": "U001",
        "text": "#2026-04-05/1 can you also add logging?",
    }

    with patch("router.router.spawn_engineer", new_callable=AsyncMock) as mock_spawn:
        with patch("router.router.asyncio.create_task", side_effect=_mock_create_task):
            await _try_route_event(
                event,
                event,
                {},
                "B_SELF",
                "U_SELF",
                slack_client,
                semaphore,
                tasks,
                seen_ts,
                {},
                is_app_mention=True,
                channel_type="im",
                project_override=dm_project,
                sessions=sessions,
                session_by_thread=session_by_thread,
                session_counter=session_counter,
            )

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.kwargs["resume_session_id"] == "uuid-resume"
    # Prompt strips the #ref prefix
    assert mock_spawn.call_args.args[1] == "can you also add logging?"


async def test_try_route_event_dm_hash_prefix_recovers_from_log(tmp_path: Path) -> None:
    """DM with #date/N prefix recovers session_id from log file when not in memory."""
    # Create the log file the router would have written
    log_dir = tmp_path / "logs" / "2026-04-05"
    log_dir.mkdir(parents=True)
    (log_dir / "session-engineer-1.log").write_text(
        json.dumps({"type": "result", "result": "done", "session_id": "recovered-uuid"})
    )

    dm_project = make_project(name="proj", platform="slack", workspace=str(tmp_path))
    sessions: dict[str, SessionRecord] = {}  # empty — simulates restart
    session_by_thread: dict[str, str] = {}
    session_counter: dict[str, int] = {}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    event = {
        "channel": "D001",
        "ts": "ts-new",
        "user": "U001",
        "text": "#2026-04-05/1 fix the edge case",
    }

    with patch("router.router.spawn_engineer", new_callable=AsyncMock) as mock_spawn:
        with patch("router.router.asyncio.create_task", side_effect=_mock_create_task):
            await _try_route_event(
                event,
                event,
                {},
                "B_SELF",
                "U_SELF",
                slack_client,
                semaphore,
                tasks,
                seen_ts,
                {},
                is_app_mention=True,
                channel_type="im",
                project_override=dm_project,
                sessions=sessions,
                session_by_thread=session_by_thread,
                session_counter=session_counter,
            )

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.kwargs["resume_session_id"] == "recovered-uuid"
    assert mock_spawn.call_args.args[1] == "fix the edge case"


# ---------------------------------------------------------------------------
# _try_route_event — threading matrix: top-level vs existing thread (inbox routing)
# ---------------------------------------------------------------------------


async def test_top_level_message_new_thread_spawns_new() -> None:
    """A top-level slack message (no existing session) spawns a new agent, no inbox write."""
    channel_map = {"C001": make_project(name="proj", platform="slack")}
    sessions: dict[str, SessionRecord] = {}
    session_by_thread: dict[str, str] = {}
    session_counter: dict[str, int] = {}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    event = {"channel": "C001", "ts": "ts-top", "user": "U001", "text": "<@U_SELF> start a task"}

    with patch("router.router.asyncio.create_task", side_effect=_mock_create_task) as mock_ct:
        await _try_route_event(
            event,
            event,
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            {},
            is_app_mention=True,
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter=session_counter,
        )

    mock_ct.assert_called_once()  # spawned a new agent
    # Pickup is acked at the routing layer for every spawned message (not just the
    # follow_thread queue path), on the triggering message's ts.
    slack_client.reactions_add.assert_called_once()
    assert slack_client.reactions_add.call_args.kwargs["name"] == "eyes"
    assert slack_client.reactions_add.call_args.kwargs["timestamp"] == "ts-top"


async def test_thread_reply_running_session_enqueues_not_spawn(tmp_path: Path) -> None:
    """A reply to a thread whose agent is still running is queued to the inbox, not forked."""
    inbox = str(tmp_path / "inbox.jsonl")
    session_ref = "2026-04-05/1"
    rec = _make_session(session_ref, state="running", inbox_path=inbox)
    channel_map = {"C001": make_project(name="proj", platform="slack")}
    sessions = {session_ref: rec}
    session_by_thread = {"C001:ts-root": session_ref}
    session_counter: dict[str, int] = {}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    event = {
        "channel": "C001",
        "ts": "ts-reply",
        "thread_ts": "ts-root",
        "user": "U001",
        "text": "<@U_SELF> also handle edge cases",
    }

    with patch("router.router.asyncio.create_task", side_effect=_mock_create_task) as mock_ct:
        await _try_route_event(
            event,
            event,
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            {},
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter=session_counter,
        )

    mock_ct.assert_not_called()  # NOT spawned — queued instead
    slack_client.reactions_add.assert_called_once()
    assert slack_client.reactions_add.call_args.kwargs["name"] == "eyes"
    assert slack_client.reactions_add.call_args.kwargs["timestamp"] == "ts-reply"
    queued = _inbox_messages(inbox)
    assert len(queued) == 1
    # mention is stripped before queueing
    assert queued[0]["text"] == "also handle edge cases"
    assert queued[0]["user"] == "U001"


async def test_thread_reply_draining_session_enqueues(tmp_path: Path) -> None:
    """A 'draining' session is treated as busy — replies are queued, not forked."""
    inbox = str(tmp_path / "inbox.jsonl")
    session_ref = "2026-04-05/1"
    rec = _make_session(session_ref, state="draining", inbox_path=inbox)
    channel_map = {"C001": make_project(name="proj", platform="slack")}
    sessions = {session_ref: rec}
    session_by_thread = {"C001:ts-root": session_ref}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    event = {
        "channel": "C001",
        "ts": "ts-reply2",
        "thread_ts": "ts-root",
        "user": "U002",
        "text": "<@U_SELF> one more thing",
    }

    with patch("router.router.asyncio.create_task", side_effect=_mock_create_task) as mock_ct:
        await _try_route_event(
            event,
            event,
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            {},
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter={},
        )

    mock_ct.assert_not_called()
    assert len(_inbox_messages(inbox)) == 1
    # The queued message is acked with a reaction on the triggering message.
    slack_client.reactions_add.assert_called_once()
    assert slack_client.reactions_add.call_args.kwargs["timestamp"] == "ts-reply2"


async def test_thread_reply_idle_session_resumes_not_enqueues(tmp_path: Path) -> None:
    """An idle session uses the existing --resume path and does NOT write to the inbox."""
    inbox = str(tmp_path / "inbox.jsonl")
    session_ref = "2026-04-05/1"
    rec = _make_session(session_ref, state="idle", inbox_path=inbox)
    channel_map = {"C001": make_project(name="proj", platform="slack")}
    sessions = {session_ref: rec}
    session_by_thread = {"C001:ts-root": session_ref}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    event = {
        "channel": "C001",
        "ts": "ts-reply",
        "thread_ts": "ts-root",
        "user": "U001",
        "text": "<@U_SELF> continue please",
    }

    with patch("router.router.spawn_engineer", new_callable=AsyncMock) as mock_spawn:
        with patch("router.router.asyncio.create_task", side_effect=_mock_create_task):
            await _try_route_event(
                event,
                event,
                channel_map,
                "B_SELF",
                "U_SELF",
                slack_client,
                semaphore,
                tasks,
                seen_ts,
                {},
                sessions=sessions,
                session_by_thread=session_by_thread,
                session_counter={},
            )

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.kwargs["resume_session_id"] == "uuid-resume"
    assert _inbox_messages(inbox) == []  # idle path never queues


async def test_running_session_without_follow_thread_acks_and_spawns(tmp_path: Path) -> None:
    """Regression: a thread reply to a running agent that has follow_thread OFF (no inbox)
    is NOT queued — it re-spawns — and must still be acked with a reaction at the routing
    layer. Previously the only routing-layer ack was the follow_thread (inbox) busy path,
    so flag-off agents never got the 👀 on follow-ups (and a flag-on agent's came from the
    pre-semaphore queue ack), making the reaction appear "only when the flag is true"."""
    session_ref = "2026-04-05/1"
    # inbox_path="" == follow_thread disabled — the busy/queue branch is skipped.
    rec = _make_session(session_ref, state="running", inbox_path="")
    channel_map = {"C001": make_project(name="proj", platform="slack")}
    sessions = {session_ref: rec}
    session_by_thread = {"C001:ts-root": session_ref}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    event = {
        "channel": "C001",
        "ts": "ts-reply",
        "thread_ts": "ts-root",
        "user": "U001",
        "text": "<@U_SELF> any update?",
    }

    with patch("router.router.asyncio.create_task", side_effect=_mock_create_task) as mock_ct:
        await _try_route_event(
            event,
            event,
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            {},
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter={},
        )

    mock_ct.assert_called_once()  # re-spawned (not queued — no inbox)
    slack_client.reactions_add.assert_called_once()
    assert slack_client.reactions_add.call_args.kwargs["name"] == "eyes"
    assert slack_client.reactions_add.call_args.kwargs["timestamp"] == "ts-reply"


async def test_idle_thread_reply_resume_acks_reaction(tmp_path: Path) -> None:
    """The idle-session --resume path also acks at the routing layer (it flows through the
    same create_task site, not the follow_thread queue branch)."""
    session_ref = "2026-04-05/1"
    rec = _make_session(session_ref, state="idle", inbox_path="")
    channel_map = {"C001": make_project(name="proj", platform="slack")}
    sessions = {session_ref: rec}
    session_by_thread = {"C001:ts-root": session_ref}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    event = {
        "channel": "C001",
        "ts": "ts-reply",
        "thread_ts": "ts-root",
        "user": "U001",
        "text": "<@U_SELF> continue please",
    }

    with patch("router.router.spawn_engineer", new_callable=AsyncMock) as mock_spawn:
        with patch("router.router.asyncio.create_task", side_effect=_mock_create_task):
            await _try_route_event(
                event,
                event,
                channel_map,
                "B_SELF",
                "U_SELF",
                slack_client,
                semaphore,
                tasks,
                seen_ts,
                {},
                sessions=sessions,
                session_by_thread=session_by_thread,
                session_counter={},
            )

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.kwargs["resume_session_id"] == "uuid-resume"
    slack_client.reactions_add.assert_called_once()
    assert slack_client.reactions_add.call_args.kwargs["timestamp"] == "ts-reply"


async def test_dm_message_acks_reaction() -> None:
    """A DM routed via project_override is acked on its triggering ts."""
    dm_project = make_project(name="dm-proj", platform="slack")
    channel_map: dict[str, ProjectConfig] = {}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    dm_event = {
        "channel": "D001",
        "channel_type": "im",
        "ts": "1700000000.000001",
        "user": "U001",
        "text": "Can you help me?",
    }

    with patch("router.router.asyncio.create_task", side_effect=_mock_create_task) as mock_ct:
        await _try_route_event(
            dm_event,
            dm_event,
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            {},
            is_app_mention=True,
            channel_type="im",
            project_override=dm_project,
        )

    mock_ct.assert_called_once()
    slack_client.reactions_add.assert_called_once()
    assert slack_client.reactions_add.call_args.kwargs["timestamp"] == "1700000000.000001"


async def test_ack_independent_of_concurrency() -> None:
    """The ack is emitted by _try_route_event BEFORE the spawn task, so a fully-occupied
    concurrency semaphore (e.g. a long-running agent holding every slot) cannot delay it."""
    channel_map = {"C001": make_project(name="proj", platform="slack")}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()  # zero free slots — a spawn would block here

    event = {"channel": "C001", "ts": "ts-top", "user": "U001", "text": "<@U_SELF> go"}

    with patch("router.router.asyncio.create_task", side_effect=_mock_create_task) as mock_ct:
        await _try_route_event(
            event,
            event,
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            {},
            is_app_mention=True,
            sessions={},
            session_by_thread={},
            session_counter={},
        )

    mock_ct.assert_called_once()  # spawn was scheduled (and would block on the semaphore)
    slack_client.reactions_add.assert_called_once()  # but the ack already fired
    assert slack_client.reactions_add.call_args.kwargs["timestamp"] == "ts-top"


async def test_dropped_event_does_not_ack() -> None:
    """An event the router drops (not @mentioned, not a bot message) is never acked."""
    channel_map = {"C001": make_project(name="proj", platform="slack")}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    # Plain channel message, no @mention of the bot → dropped before any spawn/ack.
    event = {"channel": "C001", "ts": "ts-top", "user": "U001", "text": "just chatting"}

    with patch("router.router.asyncio.create_task", side_effect=_mock_create_task) as mock_ct:
        await _try_route_event(
            event,
            event,
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            {},
            is_app_mention=False,
            sessions={},
            session_by_thread={},
            session_counter={},
        )

    mock_ct.assert_not_called()
    slack_client.reactions_add.assert_not_called()


async def test_new_top_level_while_other_thread_running_spawns(tmp_path: Path) -> None:
    """A new top-level message spawns even when a DIFFERENT thread's agent is running."""
    inbox = str(tmp_path / "inbox.jsonl")
    busy_ref = "2026-04-05/1"
    busy = _make_session(busy_ref, state="running", thread_ts="ts-other", inbox_path=inbox)
    channel_map = {"C001": make_project(name="proj", platform="slack")}
    sessions = {busy_ref: busy}
    session_by_thread = {"C001:ts-other": busy_ref}  # the busy thread is a different root
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    # A brand-new top-level message → its own thread_ts == its ts, no session there.
    event = {"channel": "C001", "ts": "ts-new", "user": "U001", "text": "<@U_SELF> separate task"}

    with patch("router.router.asyncio.create_task", side_effect=_mock_create_task) as mock_ct:
        await _try_route_event(
            event,
            event,
            channel_map,
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            {},
            is_app_mention=True,
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter={},
        )

    mock_ct.assert_called_once()  # spawned, not queued onto the other thread
    assert _inbox_messages(inbox) == []


async def test_dm_hash_ref_running_session_enqueues(tmp_path: Path) -> None:
    """A DM '#ref <msg>' targeting a running named session is queued (text stripped)."""
    inbox = str(tmp_path / "inbox.jsonl")
    session_ref = "2026-04-05/1"
    rec = _make_session(
        session_ref, state="running", channel_id="C001", thread_ts="ts-root", inbox_path=inbox
    )
    dm_project = make_project(name="proj", platform="slack")
    sessions = {session_ref: rec}
    session_by_thread = {"C001:ts-root": session_ref}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    slack_client = AsyncMock()
    semaphore = asyncio.Semaphore(3)

    event = {
        "channel": "D001",
        "channel_type": "im",
        "ts": "ts-dm",
        "user": "U001",
        "text": "#2026-04-05/1 add logging too",
    }

    with patch("router.router.asyncio.create_task", side_effect=_mock_create_task) as mock_ct:
        await _try_route_event(
            event,
            event,
            {},
            "B_SELF",
            "U_SELF",
            slack_client,
            semaphore,
            tasks,
            seen_ts,
            {},
            is_app_mention=True,
            channel_type="im",
            project_override=dm_project,
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter={},
        )

    mock_ct.assert_not_called()
    queued = _inbox_messages(inbox)
    assert len(queued) == 1
    assert queued[0]["text"] == "add logging too"  # #ref prefix stripped


# ---------------------------------------------------------------------------
# spawn_engineer — exit-time resume-drain
# ---------------------------------------------------------------------------


async def test_spawn_engineer_drains_inbox_on_exit_resumes(tmp_path: Path) -> None:
    """A message queued during the run is drained afterward via a same-session resume."""
    project = make_project(workspace=str(tmp_path), agent_name="engineer", follow_thread=True)
    semaphore = asyncio.Semaphore(2)
    sessions: dict[str, SessionRecord] = {}
    session_by_thread: dict[str, str] = {}
    session_counter: dict[str, int] = {}

    cmds: list[list[str]] = []
    outs = [
        _stream_json_with_session("first answer", "uuid-1"),
        _stream_json_with_session("second answer", "uuid-2"),
    ]

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        cmds.append([str(a) for a in args])
        if len(cmds) == 1:
            # While the first run is "in flight", a follow-up is queued into its inbox.
            rec = next(iter(sessions.values()))
            _append_to_inbox(rec.inbox_path, "U001", "follow up", "ts-2")
        else:
            # The drain run must reuse the SAME record (no new daily number) and be running.
            assert len(sessions) == 1
            assert next(iter(sessions.values())).state == "running"
        data = outs.pop(0) if outs else b""
        return _make_proc(data, returncode=0)

    with patch("router.router.asyncio.create_subprocess_exec", side_effect=fake_exec):
        await spawn_engineer(
            project,
            "initial task",
            "C001",
            "ts-root",
            slack_client := AsyncMock(),
            semaphore,
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter=session_counter,
        )

    assert len(cmds) == 2  # initial run + one drain run
    assert "uuid-1" in cmds[1]  # drain resumed the just-finished session
    assert len(sessions) == 1  # same record reused, no new session minted
    rec = next(iter(sessions.values()))
    assert rec.state == "idle"  # drained to completion
    assert rec.claude_session_id == "uuid-2"
    assert _inbox_messages(rec.inbox_path) == []  # inbox emptied
    # both answers reached the thread (one per turn)
    posts = [c.kwargs.get("text", "") for c in slack_client.chat_postMessage.call_args_list]
    assert "first answer" in posts
    assert "second answer" in posts


async def test_spawn_engineer_drains_inbox_fresh_prompt_when_no_session_id(tmp_path: Path) -> None:
    """If the run crashed without a session id, the queued message drains as a fresh prompt."""
    project = make_project(workspace=str(tmp_path), agent_name="engineer", follow_thread=True)
    semaphore = asyncio.Semaphore(2)
    sessions: dict[str, SessionRecord] = {}
    session_by_thread: dict[str, str] = {}
    session_counter: dict[str, int] = {}

    cmds: list[list[str]] = []
    # First run: failure, no session_id. Second (drain) run: success.
    outs = [b"", _stream_json_with_session("recovered", "uuid-2")]
    rcs = [1, 0]

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        cmds.append([str(a) for a in args])
        if len(cmds) == 1:
            rec = next(iter(sessions.values()))
            _append_to_inbox(rec.inbox_path, "U001", "retry please", "ts-2")
        data = outs.pop(0) if outs else b""
        return _make_proc(data, returncode=rcs.pop(0) if rcs else 0)

    with patch("router.router.asyncio.create_subprocess_exec", side_effect=fake_exec):
        await spawn_engineer(
            project,
            "initial task",
            "C001",
            "ts-root",
            AsyncMock(),
            semaphore,
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter=session_counter,
        )

    assert len(cmds) == 2
    # No session id survived the crash → drain runs as a fresh prompt (no resume arg appended).
    # cmds[1] is the bash+runner+positional args with NO trailing session id.
    assert cmds[1][-1] != "uuid-1"
    rec = next(iter(sessions.values()))
    assert rec.state == "idle"
    assert _inbox_messages(rec.inbox_path) == []


async def test_spawn_engineer_no_drain_when_inbox_empty(tmp_path: Path) -> None:
    """With an empty inbox the record ends idle and no second run is scheduled."""
    project = make_project(workspace=str(tmp_path), agent_name="engineer", follow_thread=True)
    semaphore = asyncio.Semaphore(2)
    sessions: dict[str, SessionRecord] = {}
    session_by_thread: dict[str, str] = {}
    session_counter: dict[str, int] = {}

    cmds: list[list[str]] = []

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        cmds.append([str(a) for a in args])
        return _make_proc(_stream_json_with_session("done", "uuid-1"), returncode=0)

    with patch("router.router.asyncio.create_subprocess_exec", side_effect=fake_exec):
        await spawn_engineer(
            project,
            "task",
            "C001",
            "ts-root",
            AsyncMock(),
            semaphore,
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter=session_counter,
        )

    assert len(cmds) == 1  # no drain run
    assert next(iter(sessions.values())).state == "idle"


async def test_spawn_engineer_no_inbox_path_when_follow_thread_off(tmp_path: Path) -> None:
    """A default (follow_thread off) agent provisions no inbox and sets no ORCAI_INBOX env."""
    project = make_project(workspace=str(tmp_path), agent_name="engineer")  # follow_thread=False
    semaphore = asyncio.Semaphore(2)
    sessions: dict[str, SessionRecord] = {}
    session_by_thread: dict[str, str] = {}
    session_counter: dict[str, int] = {}

    cmds: list[list[str]] = []
    envs: list[Any] = []

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        cmds.append([str(a) for a in args])
        envs.append(kwargs.get("env"))
        return _make_proc(_stream_json_with_session("done", "uuid-1"), returncode=0)

    with patch("router.router.asyncio.create_subprocess_exec", side_effect=fake_exec):
        await spawn_engineer(
            project,
            "task",
            "C001",
            "ts-root",
            AsyncMock(),
            semaphore,
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter=session_counter,
        )

    rec = next(iter(sessions.values()))
    assert rec.inbox_path == ""  # no inbox provisioned
    # env is always set now (it carries the Slack coordinates); ORCAI_INBOX must be absent.
    assert envs[0] is not None
    assert "ORCAI_INBOX" not in envs[0]
    assert len(cmds) == 1  # no drain
    assert rec.state == "idle"


def test_load_projects_parses_follow_thread(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    write_multi_agent_config(
        workspace,
        agents=[
            {
                "name": "qplus-manager",
                "platform": "slack",
                "follow_thread": True,
                "slack": {
                    "channels": ["#qplus"],
                    "bot_token_env": "M_BOT",
                    "app_token_env": "M_APP",
                },
            },
            {
                "name": "engineer",
                "slack": {
                    "channels": ["#eng"],
                    "bot_token_env": "E_BOT",
                    "app_token_env": "E_APP",
                },
            },
        ],
    )
    (workspace / ".env").write_text("M_BOT=x\nM_APP=y\nE_BOT=x\nE_APP=y\n")
    cfg = write_router_config(tmp_path, [str(workspace)])

    projects = load_projects(cfg)

    mgr = next(p for p in projects if p.agent_name == "qplus-manager")
    eng = next(p for p in projects if p.agent_name == "engineer")
    assert mgr.follow_thread is True
    assert eng.follow_thread is False  # default off


# ---------------------------------------------------------------------------
# _add_reaction
# ---------------------------------------------------------------------------


async def test_add_reaction_calls_api_on_success() -> None:
    client = AsyncMock()
    await _add_reaction(client, "C001", "trig.1", "eyes")
    client.reactions_add.assert_called_once_with(channel="C001", name="eyes", timestamp="trig.1")


async def test_add_reaction_noop_when_ts_empty() -> None:
    client = AsyncMock()
    await _add_reaction(client, "C001", "", "eyes")
    client.reactions_add.assert_not_called()


async def test_add_reaction_logs_warning_on_failure(caplog: pytest.LogCaptureFixture) -> None:
    # Regression guard: a missing reactions:write scope must surface at WARNING, not be
    # swallowed at DEBUG (the original silent-failure bug), and must not raise.
    client = AsyncMock()
    client.reactions_add.side_effect = Exception("missing_scope")
    with caplog.at_level(logging.WARNING, logger="router"):
        await _add_reaction(client, "C001", "trig.1", "eyes")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Failed to add reaction" in r.getMessage() for r in warnings)


# ---------------------------------------------------------------------------
# spawn_engineer — outbox env + relay (all proactive Slack via the bot token)
# ---------------------------------------------------------------------------


_EXEC = "router.router.asyncio.create_subprocess_exec"


def _exec_capture(
    captured: dict[str, Any], proc: Any, outbox_lines: list[dict[str, Any]] | None = None
) -> Any:
    """create_subprocess_exec stub: record env, and (optionally) write outbox lines mid-run so
    the router's drain loop relays them."""

    async def fake_exec(*args: object, **kwargs: object) -> Any:
        env = cast("dict[str, str]", kwargs.get("env") or {})
        captured["env"] = env
        captured["outbox"] = env.get("ORCAI_OUTBOX", "")
        if captured["outbox"] and outbox_lines:
            os.makedirs(os.path.dirname(captured["outbox"]), exist_ok=True)
            with open(captured["outbox"], "a", encoding="utf-8") as f:
                for m in outbox_lines:
                    f.write(json.dumps(m) + "\n")
        return proc

    return fake_exec


class _LiveProc:
    """Process stub whose returncode stays None for `alive_polls` reads, then 0 — so the outbox
    drain loop iterates a few times before the agent 'exits'."""

    def __init__(self, stdout_bytes: bytes, alive_polls: int) -> None:
        self.stdout = _async_lines(stdout_bytes)
        self.kill = MagicMock()
        self._reads = 0
        self._alive = alive_polls

    @property
    def returncode(self) -> int | None:
        self._reads += 1
        return None if self._reads <= self._alive else 0

    async def wait(self) -> int:
        return 0


async def test_spawn_exports_outbox_env(tmp_path: Path) -> None:
    proc = _make_proc(_stream_json("done"), returncode=0)
    project = make_project(workspace=str(tmp_path), follow_thread=True)
    semaphore = asyncio.Semaphore(1)
    sessions: dict[str, SessionRecord] = {}
    captured: dict[str, Any] = {}

    with patch(_EXEC, side_effect=_exec_capture(captured, proc)):
        await spawn_engineer(
            project, "msg", "C001", "ts-root", AsyncMock(), semaphore,
            sessions=sessions, session_by_thread={}, session_counter={},
        )

    ref = next(iter(sessions))
    assert captured["env"]["ORCAI_OUTBOX"] == _outbox_path_for(str(tmp_path), ref)
    # ORCAI_SAY is an absolute path to the helper so the skill never needs $CLAUDE_PROJECT_DIR.
    assert captured["env"]["ORCAI_SAY"] == os.path.join(
        str(tmp_path), ".orcai", "hooks", "outbox_say.py"
    )
    assert "ORCAI_INBOX" in captured["env"]  # follow_thread on → inbox also provisioned


async def test_no_outbox_env_when_untracked(tmp_path: Path) -> None:
    # No session tracking → no stable session ref → no outbox.
    proc = _make_proc(_stream_json("done"), returncode=0)
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)
    captured: dict[str, Any] = {}

    with patch(_EXEC, side_effect=_exec_capture(captured, proc)):
        await spawn_engineer(project, "msg", "C001", "ts-root", AsyncMock(), semaphore)

    assert "ORCAI_OUTBOX" not in captured["env"]


async def test_outbox_progress_posted_to_thread(tmp_path: Path) -> None:
    proc = _make_proc(_stream_json("final"), returncode=0)
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)
    slack = AsyncMock()
    captured: dict[str, Any] = {}
    lines = [{"text": "1/3 working", "dm": False}]

    with patch(_EXEC, side_effect=_exec_capture(captured, proc, lines)):
        await spawn_engineer(
            project, "msg", "C001", "ts-root", slack, semaphore, trigger_user="U777",
            sessions={}, session_by_thread={}, session_counter={},
        )

    progress = [
        c for c in slack.chat_postMessage.call_args_list if c.kwargs.get("text") == "1/3 working"
    ]
    assert len(progress) == 1
    assert progress[0].kwargs["channel"] == "C001"
    assert progress[0].kwargs["thread_ts"] == "ts-root"


async def test_outbox_dm_posted_to_user(tmp_path: Path) -> None:
    proc = _make_proc(_stream_json("final"), returncode=0)
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)
    slack = AsyncMock()
    captured: dict[str, Any] = {}
    lines = [{"text": "blocked, need input", "dm": True}]

    with patch(_EXEC, side_effect=_exec_capture(captured, proc, lines)):
        await spawn_engineer(
            project, "msg", "C001", "ts-root", slack, semaphore, trigger_user="U777",
            sessions={}, session_by_thread={}, session_counter={},
        )

    dm = [
        c for c in slack.chat_postMessage.call_args_list
        if c.kwargs.get("text") == "blocked, need input"
    ]
    assert len(dm) == 1
    assert dm[0].kwargs["channel"] == "U777"  # escalation DM to the triggering user
    assert "thread_ts" not in dm[0].kwargs


async def test_outbox_mentions_resolved_and_truncated(tmp_path: Path) -> None:
    proc = _make_proc(_stream_json("final"), returncode=0)
    project = make_project(workspace=str(tmp_path), mentions={"reviewer": "U999"})
    semaphore = asyncio.Semaphore(1)
    slack = AsyncMock()
    captured: dict[str, Any] = {}
    lines = [{"text": "@reviewer " + "x" * 5000, "dm": False}]

    with patch(_EXEC, side_effect=_exec_capture(captured, proc, lines)):
        await spawn_engineer(
            project, "msg", "C001", "ts-root", slack, semaphore,
            sessions={}, session_by_thread={}, session_counter={},
        )

    posted = [
        c.kwargs["text"]
        for c in slack.chat_postMessage.call_args_list
        if "<@U999>" in c.kwargs.get("text", "")
    ]
    assert posted
    assert posted[0].endswith("... (truncated)")
    assert len(posted[0]) <= SLACK_MAX_LENGTH + len("\n... (truncated)")


async def test_outbox_no_duplicate_posts(tmp_path: Path) -> None:
    # A proc that stays alive for several poll cycles → the drain loop iterates; the line must
    # still be posted exactly once (offset tracking).
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)
    slack = AsyncMock()
    proc = _LiveProc(_stream_json("final"), alive_polls=3)
    captured: dict[str, Any] = {}
    lines = [{"text": "once only", "dm": False}]

    with patch(_EXEC, side_effect=_exec_capture(captured, proc, lines)):
        with patch("router.router.asyncio.sleep", new_callable=AsyncMock):
            await spawn_engineer(
                project, "m", "C001", "ts-root", slack, semaphore,
                sessions={}, session_by_thread={}, session_counter={},
            )

    once = [c for c in slack.chat_postMessage.call_args_list if c.kwargs.get("text") == "once only"]
    assert len(once) == 1


async def test_outbox_post_error_does_not_kill_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    proc = _make_proc(_stream_json("final"), returncode=0)
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)
    slack = AsyncMock()
    slack.chat_postMessage.side_effect = Exception("slack down")
    captured: dict[str, Any] = {}
    lines = [{"text": "progress", "dm": False}]

    with caplog.at_level(logging.WARNING, logger="router"):
        with patch(_EXEC, side_effect=_exec_capture(captured, proc, lines)):
            await spawn_engineer(  # must not raise despite the post failure
                project, "msg", "C001", "ts-root", slack, semaphore,
                sessions={}, session_by_thread={}, session_counter={},
            )

    assert any("Failed to relay outbox message" in r.getMessage() for r in caplog.records)


async def test_outbox_cleared_on_exit(tmp_path: Path) -> None:
    proc = _make_proc(_stream_json("final"), returncode=0)
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)
    captured: dict[str, Any] = {}
    lines = [{"text": "hi", "dm": False}]

    with patch(_EXEC, side_effect=_exec_capture(captured, proc, lines)):
        await spawn_engineer(
            project, "msg", "C001", "ts-root", AsyncMock(), semaphore,
            sessions={}, session_by_thread={}, session_counter={},
        )

    assert captured["outbox"]
    assert not os.path.exists(captured["outbox"])  # removed after the run


# ---------------------------------------------------------------------------
# _try_route_event — triggering user threaded to spawn
# ---------------------------------------------------------------------------


async def test_route_event_passes_trigger_user_to_spawn() -> None:
    channel_map = {"C001": make_project(name="proj", platform="slack")}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    semaphore = asyncio.Semaphore(3)
    event = {"channel": "C001", "ts": "t1", "user": "U123", "text": "<@U_SELF> do work"}

    with patch("router.router.spawn_engineer", new_callable=AsyncMock) as mock_spawn:
        with patch("router.router.asyncio.create_task", side_effect=_mock_create_task):
            await _try_route_event(
                event, event, channel_map, "B_SELF", "U_SELF",
                AsyncMock(), semaphore, tasks, seen_ts, {},
            )

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.kwargs["trigger_user"] == "U123"


async def test_route_event_trigger_user_falls_back_to_username() -> None:
    channel_map = {"C001": make_project(name="proj", platform="slack")}
    seen_ts: deque[str] = deque(maxlen=1000)
    tasks: set[asyncio.Task[None]] = set()
    semaphore = asyncio.Semaphore(3)
    # No "user" key (e.g. an integration post) → fall back to "username".
    event = {"channel": "C001", "ts": "t2", "username": "integration-bot", "text": "<@U_SELF> ping"}

    with patch("router.router.spawn_engineer", new_callable=AsyncMock) as mock_spawn:
        with patch("router.router.asyncio.create_task", side_effect=_mock_create_task):
            await _try_route_event(
                event, event, channel_map, "B_SELF", "U_SELF",
                AsyncMock(), semaphore, tasks, seen_ts, {},
            )

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.kwargs["trigger_user"] == "integration-bot"


# ---------------------------------------------------------------------------
# spawn_engineer — drain continuation keeps its outbox
# ---------------------------------------------------------------------------


async def test_drain_continuation_provisions_outbox(tmp_path: Path) -> None:
    project = make_project(workspace=str(tmp_path), agent_name="engineer", follow_thread=True)
    semaphore = asyncio.Semaphore(2)
    sessions: dict[str, SessionRecord] = {}
    session_by_thread: dict[str, str] = {}
    session_counter: dict[str, int] = {}
    cmds: list[list[str]] = []
    envs: list[Any] = []
    outs = [
        _stream_json_with_session("first", "uuid-1"),
        _stream_json_with_session("second", "uuid-2"),
    ]

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        cmds.append([str(a) for a in args])
        envs.append(kwargs.get("env"))
        if len(cmds) == 1:
            rec = next(iter(sessions.values()))
            _append_to_inbox(rec.inbox_path, "U001", "follow up", "ts-2")
        return _make_proc(outs.pop(0) if outs else b"", returncode=0)

    with patch("router.router.asyncio.create_subprocess_exec", side_effect=fake_exec):
        await spawn_engineer(
            project,
            "initial",
            "C001",
            "ts-root",
            AsyncMock(),
            semaphore,
            trigger_user="U777",
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter=session_counter,
        )

    assert len(cmds) == 2  # initial + drain
    # The drain continuation reuses the same session, so it keeps the same outbox + inbox.
    assert envs[1]["ORCAI_OUTBOX"] == envs[0]["ORCAI_OUTBOX"]
    assert "ORCAI_INBOX" in envs[1]


async def test_final_result_still_posted_once(tmp_path: Path) -> None:
    # Agent-side proactive posting is additive: the router still posts the final result once.
    proc = _make_proc(_stream_json("the answer"), returncode=0)
    project = make_project(workspace=str(tmp_path))
    semaphore = asyncio.Semaphore(1)
    slack_client = AsyncMock()

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(
            project, "q", "C001", "ts-root", slack_client, semaphore, trigger_user="U1"
        )

    posts = [
        c for c in slack_client.chat_postMessage.call_args_list
        if c.kwargs.get("text") == "the answer"
    ]
    assert len(posts) == 1
    assert posts[0].kwargs["thread_ts"] == "ts-root"

import asyncio
import json
import logging
import sys
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from datetime import datetime as real_datetime
from pathlib import Path
from typing import Any
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
    DailyDirectoryFileHandler,
    _extract_result,
    _is_unchanged_message_edit,
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

    assert slack_client.chat_postMessage.call_count == 2
    result_kwargs = slack_client.chat_postMessage.call_args_list[1].kwargs
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

    result_kwargs = slack_client.chat_postMessage.call_args_list[1].kwargs
    assert result_kwargs["text"] == "PR opened, <@U999> please check"


async def test_spawn_engineer_sends_pickup_message_in_thread(tmp_path: Path) -> None:
    stdout = _stream_json("done")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path), agent_name="mybot")
    semaphore = asyncio.Semaphore(1)

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(project, "do something", "C001", "99.0", slack_client, semaphore)

    pickup_kwargs = slack_client.chat_postMessage.call_args_list[0].kwargs
    assert "mybot" in pickup_kwargs["text"]
    assert pickup_kwargs["channel"] == "C001"
    assert pickup_kwargs["thread_ts"] == "99.0"


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


async def test_spawn_engineer_pickup_message_includes_session_ref(tmp_path: Path) -> None:
    stdout = _stream_json("done")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path), agent_name="engineer")
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

    pickup_text = slack_client.chat_postMessage.call_args_list[0].kwargs["text"]
    assert "engineer" in pickup_text
    assert "`#" in pickup_text  # session ref included


async def test_spawn_pickup_no_session_ref_without_tracking(tmp_path: Path) -> None:
    stdout = _stream_json("done")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    project = make_project(workspace=str(tmp_path), agent_name="engineer")
    semaphore = asyncio.Semaphore(1)

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(project, "do something", "C001", "99.0", slack_client, semaphore)

    pickup_text = slack_client.chat_postMessage.call_args_list[0].kwargs["text"]
    assert "engineer" in pickup_text
    assert "`#" not in pickup_text  # no session ref when tracking not configured


async def test_spawn_engineer_pickup_message_sent_before_result(tmp_path: Path) -> None:
    stdout = _stream_json("result text")
    proc = _make_proc(stdout, returncode=0)
    slack_client = AsyncMock()
    call_order: list[str] = []

    async def record_call(**kwargs: object) -> None:
        call_order.append(str(kwargs.get("text", "")))

    slack_client.chat_postMessage.side_effect = record_call
    project = make_project(workspace=str(tmp_path), agent_name="engineer")
    semaphore = asyncio.Semaphore(1)

    with patch("router.router.asyncio.create_subprocess_exec", return_value=proc):
        await spawn_engineer(project, "do something", "C001", "ts1", slack_client, semaphore)

    assert len(call_order) == 2
    assert "engineer" in call_order[0]
    assert call_order[1] == "result text"


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

    # pickup + result + banner
    assert slack_client.chat_postMessage.call_count == 3
    banner_text: str = slack_client.chat_postMessage.call_args_list[2].kwargs["text"]
    assert "Session" in banner_text
    assert "/1" in banner_text
    assert "reply here" in banner_text


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

    # pickup + error only, no banner
    assert slack_client.chat_postMessage.call_count == 2


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

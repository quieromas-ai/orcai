import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, cast

import yaml
from dotenv import dotenv_values, load_dotenv
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from router.platforms import Platform, get_parser

ROUTER_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_RUNNER = os.path.join(ROUTER_DIR, "agent_runner.sh")
ROUTER_CONFIG = os.path.join(ROUTER_DIR, "config.yaml")
SLACK_MAX_LENGTH = 3900
OUTBOX_POLL_SECONDS = 1.5  # how often the router relays the agent's queued proactive messages
_MENTION_RE = re.compile(r"@(\w+)")
_DEFAULT_BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"
_DEFAULT_APP_TOKEN_ENV = "SLACK_APP_TOKEN"

logger = logging.getLogger("router")


class DailyDirectoryFileHandler(logging.Handler):
    """File handler that rotates into a new YYYY-MM-DD directory at midnight UTC."""

    def __init__(self, base_dir: str, filename: str = "router.log") -> None:
        super().__init__()
        self._base_dir = base_dir
        self._filename = filename
        self._current_date: str = ""
        self._stream: Any = None

    def _open_for_date(self, date_str: str) -> None:
        if self._stream is not None:
            self._stream.close()
        log_dir = os.path.join(self._base_dir, date_str)
        os.makedirs(log_dir, exist_ok=True)
        self._stream = open(os.path.join(log_dir, self._filename), "a")  # noqa: SIM115
        self._current_date = date_str

    def emit(self, record: logging.LogRecord) -> None:
        self.acquire()
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != self._current_date:
                self._open_for_date(today)
            if self._stream is None:
                return
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)
        finally:
            self.release()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
        super().close()


@dataclass
class ProjectConfig:
    name: str
    workspace: str
    channels: list[str]
    platform: Platform = "github"
    agent_name: str = "engineer"
    backend: str = "claude"
    model: str = "claude-sonnet-4-6"
    timeout_minutes: int = 60
    follow_thread: bool = False  # opt-in: deliver follow-ups MID-RUN (serialize is universal)
    slack_bot_token: str = ""
    slack_app_token: str = field(default="", repr=False)
    mentions: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass
class SessionRecord:
    number: int
    date_str: str  # "YYYY-MM-DD"
    project_name: str
    claude_session_id: str  # empty while running
    channel_id: str
    thread_ts: str
    created_at: datetime
    state: str  # "running" | "draining" | "idle"
    inbox_path: str = ""  # per-session router→live-agent IPC file (empty if untracked)

    @property
    def ref(self) -> str:
        return f"{self.date_str}/{self.number}"

    @classmethod
    def parse_ref(cls, ref: str) -> tuple[str, int]:
        """Parse 'YYYY-MM-DD/N' format into (date_str, number)."""
        date_str, num_str = ref.split("/", 1)
        return date_str, int(num_str)


def _parse_channels(cfg: dict[str, Any]) -> list[str]:
    return [ch.lstrip("#") for ch in cfg.get("channels", [])]


def _read_yaml(path: str) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)  # type: ignore[no-any-return]


def load_log_level(router_config_path: str) -> int:
    router_cfg = _read_yaml(router_config_path)
    level_name = router_cfg.get("log_level", "INFO").upper()
    return getattr(logging, level_name, logging.INFO)


def load_projects(router_config_path: str) -> list[ProjectConfig]:
    router_cfg = _read_yaml(router_config_path)
    workspace_paths: list[str] = router_cfg.get("workspaces", [])
    projects: list[ProjectConfig] = []
    for workspace in workspace_paths:
        config_path = os.path.join(workspace, "config.yaml")
        if not os.path.isfile(config_path):
            logger.warning("No config.yaml at %s, skipping", workspace)
            continue
        raw = _read_yaml(config_path)
        project_cfg = raw.get("project", {})
        project_name = project_cfg.get("name", os.path.basename(workspace))
        default_platform = project_cfg.get("platform", "github")

        if "agents" in raw:
            # New multi-agent schema: each entry in agents: list becomes one ProjectConfig.
            # Slack tokens are read from the workspace .env by env var name.
            workspace_env = dotenv_values(os.path.join(workspace, ".env"))
            for agent_entry in raw["agents"]:
                slack_cfg = agent_entry.get("slack", {})
                bot_env = slack_cfg.get("bot_token_env", _DEFAULT_BOT_TOKEN_ENV)
                app_env = slack_cfg.get("app_token_env", _DEFAULT_APP_TOKEN_ENV)
                projects.append(
                    ProjectConfig(
                        name=project_name,
                        workspace=workspace,
                        channels=_parse_channels(slack_cfg),
                        platform=agent_entry.get("platform", default_platform),
                        agent_name=agent_entry.get("name", "engineer"),
                        backend=agent_entry.get("backend", "claude"),
                        model=agent_entry.get("model", "claude-sonnet-4-6"),
                        timeout_minutes=agent_entry.get("timeout_minutes", 60),
                        follow_thread=agent_entry.get("follow_thread", False),
                        slack_bot_token=workspace_env.get(bot_env) or os.environ.get(bot_env, ""),
                        slack_app_token=workspace_env.get(app_env) or os.environ.get(app_env, ""),
                    )
                )
        else:
            # Old schema (backward compat): single agent: + slack: at root.
            # Slack tokens come from the global router .env.
            slack_cfg = raw.get("slack", {})
            agent_cfg = raw.get("agent", {})
            projects.append(
                ProjectConfig(
                    name=project_name,
                    workspace=workspace,
                    channels=_parse_channels(slack_cfg),
                    platform=default_platform,
                    agent_name=agent_cfg.get("name", "engineer"),
                    backend=agent_cfg.get("backend", "claude"),
                    model=agent_cfg.get("model", "claude-sonnet-4-6"),
                    timeout_minutes=agent_cfg.get("timeout_minutes", 60),
                    follow_thread=agent_cfg.get("follow_thread", False),
                    slack_bot_token=os.environ.get(_DEFAULT_BOT_TOKEN_ENV, ""),
                    slack_app_token=os.environ.get(_DEFAULT_APP_TOKEN_ENV, ""),
                )
            )
    unique_names = list(dict.fromkeys(p.name for p in projects))
    logger.info(
        "Loaded %d project(s), %d agent(s): %s",
        len(unique_names),
        len(projects),
        [p.name for p in projects],
    )
    return projects


def _extract_result(stdout_text: str) -> tuple[str, str]:
    """Return (result_text, session_id) from stream-json agent output."""
    for line in reversed(stdout_text.splitlines()):
        try:
            data = json.loads(line)
            if data.get("type") == "result":
                return data.get("result", ""), data.get("session_id", "")
        except json.JSONDecodeError:
            continue
    return "", ""


def _result_text_from_line(line: bytes) -> str:
    """Return the result text if this stream-json line is a non-empty 'result' event, else ''.

    Used to post each completed agent turn as it streams — so when the Stop hook keeps one
    process alive across several queued messages, every answer reaches the thread (not just
    the last 'result' that _extract_result would pick).
    """
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if isinstance(data, dict) and data.get("type") == "result":
        return data.get("result", "") or ""
    return ""


def _session_log_name(agent_name: str, session_number: int) -> str:
    """Return the log filename for a numbered agent session."""
    return f"session-{agent_name}-{session_number}.log"


def _session_id_from_log(log_path: str) -> str:
    """Read a session log file and extract the claude session_id from the result event."""
    try:
        with open(log_path, errors="replace") as f:
            content = f.read()
        _, session_id = _extract_result(content)
        return session_id
    except OSError:
        return ""


def _inbox_path_for(workspace: str, session_ref: str) -> str:
    """Filesystem path of the per-session inbox file (router→live-agent IPC channel)."""
    safe = session_ref.replace("/", "-")
    return os.path.join(workspace, ".orcai", "inbox", f"{safe}.jsonl")


def _append_to_inbox(inbox_path: str, user: str, text: str, ts: str) -> None:
    """Append one queued Slack message to the session inbox (atomic JSONL line append)."""
    os.makedirs(os.path.dirname(inbox_path), exist_ok=True)
    line = json.dumps({"ts": ts, "user": user, "text": text}, ensure_ascii=False)
    with open(inbox_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _inbox_messages(inbox_path: str) -> list[dict[str, str]]:
    """Read queued inbox messages (empty list if the file is missing or empty)."""
    if not inbox_path:
        return []
    try:
        with open(inbox_path, encoding="utf-8", errors="replace") as f:
            raw_lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return []
    out: list[dict[str, str]] = []
    for ln in raw_lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _clear_inbox(inbox_path: str) -> None:
    """Remove the inbox file once its messages have been claimed."""
    if inbox_path:
        with contextlib.suppress(OSError):
            os.remove(inbox_path)


async def _add_reaction(slack_client: AsyncWebClient, channel: str, ts: str, name: str) -> None:
    """Best-effort emoji reaction ack (used instead of a text 'picked up' message)."""
    if not ts:
        return
    try:
        await slack_client.reactions_add(channel=channel, name=name, timestamp=ts)
    except Exception as e:
        # WARNING (not DEBUG): a missing `reactions:write` scope or revoked token silently
        # disables the ack reaction — it must be visible at the router's default INFO level.
        # Surface the Slack error code (e.g. missing_scope, already_reacted, not_in_channel)
        # since the SDK's default message only reports the HTTP status.
        resp = getattr(e, "response", None)
        code = ""
        if resp is not None:
            with contextlib.suppress(Exception):
                code = f" ({resp['error']})"
        logger.warning("Failed to add reaction :%s: in %s: %s%s", name, channel, e, code)


def _outbox_path_for(workspace: str, session_ref: str) -> str:
    """Filesystem path of the per-session outbox file (live-agent → router → Slack channel).

    Mirror of the inbox: the agent appends proactive messages here via the `orcai-say` skill and
    the router relays each one to Slack with the bot token — so every message comes from the one
    bot identity and reaches the bot's own DMs/channels (which a separate MCP identity cannot).
    """
    safe = session_ref.replace("/", "-")
    return os.path.join(workspace, ".orcai", "outbox", f"{safe}.jsonl")


def _outbox_messages(outbox_path: str) -> list[dict[str, Any]]:
    """Parsed outbox messages ({'text','dm'} per line).

    Skips a trailing line with no newline — that is a concurrent append still in flight, picked
    up on the next poll once complete.
    """
    if not outbox_path:
        return []
    try:
        with open(outbox_path, encoding="utf-8", errors="replace") as f:
            data = f.read()
    except OSError:
        return []
    if not data.endswith("\n"):
        data = data[: data.rfind("\n") + 1]
    out: list[dict[str, Any]] = []
    for ln in data.splitlines():
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _clear_outbox(outbox_path: str) -> None:
    """Remove the outbox file once the run is finished."""
    if outbox_path:
        with contextlib.suppress(OSError):
            os.remove(outbox_path)


def _slack_ts_sort_key(msg: dict[str, Any]) -> float:
    """Parse Slack message ts for chronological ordering (oldest first)."""
    ts_raw = msg.get("ts")
    if ts_raw is None:
        return 0.0
    try:
        return float(ts_raw)
    except (ValueError, TypeError):
        return 0.0


async def fetch_thread_context(
    slack_client: AsyncWebClient,
    channel_id: str,
    thread_ts: str | None,
    event_ts: str,
    channel_type: str,
    user_cache: dict[str, str],
) -> str:
    """Fetch and format conversation history as a preamble for the agent prompt.

    For DMs uses conversations.history (flat conversation).
    For channel threads uses conversations.replies.
    Messages are ordered by ts ascending regardless of API default order.
    The triggering message (event_ts) is excluded to avoid duplication.
    """
    try:
        if channel_type == "im":
            result = await slack_client.conversations_history(channel=channel_id, limit=20)
            try:
                raw_messages: list[dict[str, Any]] = list(result["messages"])
            except (KeyError, TypeError):
                raw_messages = []
        else:
            if not thread_ts:
                return ""
            result = await slack_client.conversations_replies(
                channel=channel_id, ts=thread_ts, limit=50
            )
            try:
                raw_messages = list(result["messages"])
            except (KeyError, TypeError):
                raw_messages = []

        messages = [m for m in raw_messages if m.get("ts") != event_ts]
        messages.sort(key=_slack_ts_sort_key)
        if not messages:
            return ""

        # Resolve all uncached human users in parallel
        unknown_users = {
            m["user"] for m in messages if m.get("user") and m["user"] not in user_cache
        }
        if unknown_users:
            user_results = await asyncio.gather(
                *[slack_client.users_info(user=uid) for uid in unknown_users],
                return_exceptions=True,
            )
            for uid, res in zip(unknown_users, user_results):
                if isinstance(res, Exception):
                    user_cache[uid] = uid
                else:
                    try:
                        body = cast(dict[str, Any], res)
                        profile: dict[str, Any] = body["user"]["profile"]
                        user_cache[uid] = (
                            profile.get("display_name") or profile.get("real_name") or uid
                        )
                    except (KeyError, TypeError):
                        user_cache[uid] = uid

        lines = ["=== Conversation History ==="]
        for msg in messages:
            ts_raw = msg.get("ts") or "0"
            try:
                ts_float = float(ts_raw)
            except (ValueError, TypeError):
                ts_float = 0.0
            dt_str = datetime.fromtimestamp(ts_float, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

            user = msg.get("user")
            user_id = user or msg.get("bot_id", "")
            if not user and user_id and user_id not in user_cache:
                user_cache[user_id] = msg.get("username") or user_id
            name = user_cache.get(user_id, user_id) if user_id else "unknown"

            text = (msg.get("text") or "").strip()
            if text:
                lines.append(f"[{dt_str}] @{name}: {text}")

        if len(lines) <= 1:
            return ""
        lines.append("")
        return "\n".join(lines) + "\n"
    except Exception as e:
        logger.warning("Failed to fetch thread context for channel %s: %s", channel_id, e)
        return ""


def _build_reply_text(project_name: str, exit_code: int, result_text: str, log_path: str) -> str:
    """Build the Slack reply text based on agent outcome."""
    if exit_code == 0 and result_text:
        if len(result_text) > SLACK_MAX_LENGTH:
            result_text = result_text[:SLACK_MAX_LENGTH] + "\n... (truncated)"
        return result_text
    if exit_code == 0:
        return f"Agent completed for project *{project_name}* but produced no output."
    return f"Agent failed for project *{project_name}* (exit code {exit_code}). Log: `{log_path}`"


def _resolve_mentions(text: str, mentions: dict[str, str]) -> str:
    """Replace @name tokens with <@SLACK_USER_ID> for known agents."""
    if not mentions:
        return text

    def _replace(m: re.Match[str]) -> str:
        user_id = mentions.get(m.group(1))
        return f"<@{user_id}>" if user_id else m.group(0)

    return _MENTION_RE.sub(_replace, text)


async def resolve_channels(
    client: AsyncWebClient, projects: list[ProjectConfig]
) -> dict[str, ProjectConfig]:
    needed: set[str] = set()
    for p in projects:
        needed.update(p.channels)
    if not needed:
        return {}

    name_to_id: dict[str, str] = {}
    cursor: str | None = None
    try:
        while True:
            kwargs: dict[str, Any] = {"types": "public_channel,private_channel", "limit": 1000}
            if cursor:
                kwargs["cursor"] = cursor
            result = await client.conversations_list(**kwargs)
            for ch in result.get("channels") or []:
                if ch["name"] in needed:
                    name_to_id[ch["name"]] = ch["id"]
            if name_to_id.keys() >= needed:
                break
            cursor = (result.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
    except SlackApiError as e:
        logger.warning(
            "conversations.list failed (%s) — channel mapping unavailable for these projects: %s",
            e.response.get("error", str(e)),
            [p.name for p in projects],
        )

    channel_map: dict[str, ProjectConfig] = {}
    for p in projects:
        for ch_name in p.channels:
            ch_id = name_to_id.get(ch_name)
            if ch_id:
                channel_map[ch_id] = p
                logger.info("Mapped #%s (%s) → project '%s'", ch_name, ch_id, p.name)
            else:
                logger.warning("Channel #%s not found in workspace (project '%s')", ch_name, p.name)
    return channel_map


async def spawn_engineer(
    project: ProjectConfig,
    message_text: str,
    channel_id: str,
    thread_ts: str | None,
    slack_client: AsyncWebClient,
    semaphore: asyncio.Semaphore,
    ticket_key: str | None = None,
    active_tickets: dict[str, str] | None = None,
    channel_type: str = "channel",
    event_ts: str = "",
    trigger_user: str = "",
    user_cache: dict[str, str] | None = None,
    sessions: dict[str, SessionRecord] | None = None,
    session_by_thread: dict[str, str] | None = None,
    session_counter: dict[str, int] | None = None,
    resume_session_id: str | None = None,
    drain_session_ref: str | None = None,
) -> None:
    drain_batch: list[dict[str, str]] = []
    drain_resume_id: str | None = None
    drain_ref: str | None = None
    try:
        async with semaphore:
            now = datetime.now(timezone.utc)
            date_str = now.strftime("%Y-%m-%d")
            log_dir = os.path.join(project.workspace, "logs", date_str)
            os.makedirs(log_dir, exist_ok=True)

            session_ref: str | None = None
            session_number: int = 0
            inbox_path: str = ""
            is_drain = False
            if drain_session_ref is not None and sessions is not None and (
                drain_session_ref in sessions
            ):
                # Continuation of an in-flight thread conversation: reuse the SAME record,
                # do NOT mint a new daily session number or remap session_by_thread.
                is_drain = True
                session_ref = drain_session_ref
                rec = sessions[session_ref]
                session_number = rec.number
                inbox_path = rec.inbox_path or _inbox_path_for(project.workspace, session_ref)
                rec.inbox_path = inbox_path
                rec.state = "running"
            elif (
                sessions is not None
                and session_counter is not None
                and session_by_thread is not None
            ):
                counter_key = f"{project.name}:{date_str}"
                session_counter[counter_key] = session_counter.get(counter_key, 0) + 1
                session_number = session_counter[counter_key]
                session_ref = f"{date_str}/{session_number}"
                # Per-thread serialization queue — provisioned for every tracked session so a
                # reply to a still-running agent is queued (and resumed after exit) instead of
                # forking a second parallel process. Mid-run delivery of this queue is opt-in
                # via follow_thread (ORCAI_INBOX, set below); serialization itself is universal.
                inbox_path = _inbox_path_for(project.workspace, session_ref)
                sessions[session_ref] = SessionRecord(
                    number=session_number,
                    date_str=date_str,
                    project_name=project.name,
                    claude_session_id="",
                    channel_id=channel_id,
                    thread_ts=thread_ts or "",
                    created_at=now,
                    state="running",
                    inbox_path=inbox_path,
                )
                if thread_ts:
                    session_by_thread[f"{channel_id}:{thread_ts}"] = session_ref

            # Outbox is provisioned for any tracked session so the agent can post proactive
            # updates the router relays via the bot token (independent of inbox/follow_thread).
            outbox_path = _outbox_path_for(project.workspace, session_ref) if session_ref else ""

            log_name = (
                # Distinct log per drain cycle so the canonical session log isn't clobbered.
                f"{_session_log_name(project.agent_name, session_number)[:-4]}-{now:%H%M%S}.log"
                if is_drain
                else _session_log_name(project.agent_name, session_number)
                if session_ref
                else now.strftime("%H%M%S") + ".log"
            )
            log_path = os.path.join(log_dir, log_name)

            context = await fetch_thread_context(
                slack_client, channel_id, thread_ts, event_ts, channel_type, user_cache or {}
            )
            if context:
                full_prompt = f"{context}=== Current Message ===\n{message_text}"
            else:
                full_prompt = message_text

            cmd = [
                "bash",
                AGENT_RUNNER,
                project.agent_name,
                project.backend,
                project.model,
                project.workspace,
                full_prompt,
            ]
            if resume_session_id:
                cmd.append(resume_session_id)

            logger.info(
                "Spawning %s for project '%s' (backend=%s, model=%s, session=%s)",
                project.agent_name,
                project.name,
                project.backend,
                project.model,
                session_ref or "none",
            )

            result_text = ""
            exit_code = -1
            claude_session_id = ""

            # Expose the inbox path only when follow_thread is on (router→agent: poll skill +
            # PostToolUse/Stop hooks read follow-ups MID-RUN). The inbox file is provisioned for
            # every session for serialization/drain, but flag-off agents must not get mid-run
            # injection — so ORCAI_INBOX is gated on the flag. The outbox path (agent→router: the
            # `orcai-say` skill relays proactive messages via the bot token) is unconditional.
            run_env = {**os.environ}
            if inbox_path and project.follow_thread:
                run_env["ORCAI_INBOX"] = inbox_path
            if outbox_path:
                run_env["ORCAI_OUTBOX"] = outbox_path
                # Absolute path to the outbox helper so the `orcai-say` skill never depends on
                # $CLAUDE_PROJECT_DIR (which is unset inside headless `claude -p` agent runs).
                run_env["ORCAI_SAY"] = os.path.join(
                    project.workspace, ".orcai", "hooks", "outbox_say.py"
                )

            with open(log_path, "wb") as log_file:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=log_file,
                    cwd=project.workspace,
                    env=run_env,
                    limit=10 * 1024 * 1024,  # 10 MB — prevents ValueError on long JSON lines
                )
                stdout_lines: list[bytes] = []
                posted_count = 0

                async def _stream_stdout() -> None:
                    nonlocal posted_count
                    assert proc.stdout is not None
                    async for line in proc.stdout:
                        log_file.write(line)
                        log_file.flush()
                        stdout_lines.append(line)
                        # Post each completed agent turn as it arrives (one reply per answered
                        # message when the Stop hook keeps the process alive across queued msgs).
                        turn_text = _result_text_from_line(line)
                        if not turn_text:
                            continue
                        turn_text = _resolve_mentions(turn_text, project.mentions)
                        if len(turn_text) > SLACK_MAX_LENGTH:
                            turn_text = turn_text[:SLACK_MAX_LENGTH] + "\n... (truncated)"
                        turn_kwargs: dict[str, Any] = {"channel": channel_id, "text": turn_text}
                        if thread_ts:
                            turn_kwargs["thread_ts"] = thread_ts
                        try:
                            await slack_client.chat_postMessage(**turn_kwargs)
                            posted_count += 1
                        except Exception as e:
                            logger.error("Failed to post Slack turn: %s", e)

                posted_outbox = 0

                async def _flush_outbox() -> None:
                    # Relay each new queued message via the BOT token: in-thread (dm falsy) or as
                    # an escalation DM to the triggering user (dm truthy). Advance the offset before
                    # posting so a failed post is not retried forever.
                    nonlocal posted_outbox
                    msgs = _outbox_messages(outbox_path)
                    for m in msgs[posted_outbox:]:
                        posted_outbox += 1
                        text = _resolve_mentions(str(m.get("text", "")), project.mentions)
                        if not text:
                            continue
                        if len(text) > SLACK_MAX_LENGTH:
                            text = text[:SLACK_MAX_LENGTH] + "\n... (truncated)"
                        if m.get("dm"):
                            target = trigger_user or channel_id
                            out_kwargs: dict[str, Any] = {"channel": target, "text": text}
                        else:
                            out_kwargs = {"channel": channel_id, "text": text}
                            if thread_ts:
                                out_kwargs["thread_ts"] = thread_ts
                        if not out_kwargs["channel"]:
                            continue
                        try:
                            await slack_client.chat_postMessage(**out_kwargs)
                        except Exception as e:
                            logger.warning("Failed to relay outbox message: %s", e)

                async def _drain_outbox() -> None:
                    # Poll the outbox while the agent runs, then flush once more after it exits.
                    if not outbox_path:
                        return
                    while proc.returncode is None:
                        await _flush_outbox()
                        await asyncio.sleep(OUTBOX_POLL_SECONDS)
                    await _flush_outbox()

                try:
                    await asyncio.wait_for(
                        asyncio.gather(_stream_stdout(), proc.wait(), _drain_outbox()),
                        timeout=project.timeout_minutes * 60,
                    )
                    exit_code = proc.returncode if proc.returncode is not None else -1
                    stdout_text = b"".join(stdout_lines).decode("utf-8", errors="replace")
                    result_text, claude_session_id = _extract_result(stdout_text)
                    result_text = _resolve_mentions(result_text, project.mentions)

                except asyncio.TimeoutError:
                    logger.warning(
                        "Agent timed out after %dm for project '%s', killing",
                        project.timeout_minutes,
                        project.name,
                    )
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
                except Exception as e:
                    logger.error(
                        "Unexpected error streaming agent output for project '%s': %s — killing",
                        project.name,
                        e,
                    )
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()

            # The outbox was fully relayed during the run (final flush in _drain_outbox); remove
            # it so a resumed/drained continuation of this session starts from a clean file.
            _clear_outbox(outbox_path)

            # ---- Race-safe post-run transition: decided synchronously, BEFORE any await, so
            # there is never a moment where the record reads "idle" while its inbox is
            # non-empty. A message arriving during the Slack posts below therefore sees
            # "draining" and enqueues instead of forking a parallel resume. ----
            if sessions is not None and session_ref in sessions:
                rec = sessions[session_ref]
                rec.claude_session_id = claude_session_id or rec.claude_session_id
                pending = _inbox_messages(rec.inbox_path)
                if pending:
                    _clear_inbox(rec.inbox_path)  # claim the queue synchronously
                    drain_batch = pending
                    drain_resume_id = rec.claude_session_id or None
                    drain_ref = session_ref
                    rec.state = "draining"
                else:
                    rec.state = "idle"

            # Post a fallback/error only when nothing was streamed to the thread (the success
            # path already posted each turn live in _stream_stdout).
            if posted_count == 0:
                reply_text = _build_reply_text(project.name, exit_code, result_text, log_path)
                reply_kwargs: dict[str, Any] = {"channel": channel_id, "text": reply_text}
                if thread_ts:
                    reply_kwargs["thread_ts"] = thread_ts
                try:
                    await slack_client.chat_postMessage(**reply_kwargs)
                except Exception as e:
                    logger.error("Failed to post Slack response: %s", e)

            # Show the "session saved — reply/DM to continue" banner only ONCE, when the session
            # is first created — not on every resume/thread-reply (that floods the thread). A
            # resume always carries resume_session_id; a fresh session does not. Also skipped
            # mid-drain (not drain_batch).
            if (
                exit_code == 0
                and session_ref
                and claude_session_id
                and not drain_batch
                and resume_session_id is None
            ):
                banner_kwargs: dict[str, Any] = {
                    "channel": channel_id,
                    "text": (
                        f"Session `#{session_ref}` saved"
                        f" — reply here or DM `#{session_ref}` to continue"
                    ),
                }
                if thread_ts:
                    banner_kwargs["thread_ts"] = thread_ts
                try:
                    await slack_client.chat_postMessage(**banner_kwargs)
                except Exception as e:
                    logger.error("Failed to post session banner: %s", e)

            logger.info(
                "Agent finished for project '%s' (exit_code=%d, log=%s)",
                project.name,
                exit_code,
                log_path,
            )

        # Drain queued messages AFTER releasing the semaphore (so the continuation can
        # re-acquire its own slot — avoids a one-slot deadlock). Resumes the SAME session;
        # falls back to a fresh prompt (thread history still supplied) when no session id
        # survived a crash. Recurses until the inbox is empty and the record goes idle.
        if drain_batch and drain_ref is not None:
            newest = drain_batch[-1]
            logger.info(
                "Draining %d queued message(s) for session #%s", len(drain_batch), drain_ref
            )
            await spawn_engineer(
                project,
                newest.get("text", ""),
                channel_id,
                thread_ts,
                slack_client,
                semaphore,
                channel_type=channel_type,
                event_ts=newest.get("ts", ""),
                trigger_user=trigger_user,
                user_cache=user_cache,
                sessions=sessions,
                session_by_thread=session_by_thread,
                session_counter=session_counter,
                resume_session_id=drain_resume_id,
                drain_session_ref=drain_ref,
            )
    finally:
        if ticket_key and active_tickets is not None:
            active_tickets.pop(ticket_key, None)


def _is_unchanged_message_edit(
    inner: dict[str, Any],
    prev: dict[str, Any],
    channel_map: dict[str, ProjectConfig],
    channel: str,
) -> bool:
    """Return True when a message_changed event carries no new text content.

    GitHub fires message_changed for metadata-only updates (e.g. recolouring
    the root PR message from green to purple on merge).  Those have identical
    visible text in the inner and previous messages.  Thread-broadcast messages
    always represent new content, so they are never considered unchanged.
    """
    if inner.get("subtype") == "thread_broadcast":
        return False
    try:
        parser = get_parser(channel_map[channel].platform)
        return parser.extract_message_text(inner) == parser.extract_message_text(prev)
    except (KeyError, ValueError):
        return False


async def _try_route_event(
    event: dict[str, Any],
    source: dict[str, Any],
    channel_map: dict[str, ProjectConfig],
    own_bot_id: str,
    own_user_id: str,
    slack_client: AsyncWebClient,
    semaphore: asyncio.Semaphore,
    tasks_ref: set[asyncio.Task[None]],
    seen_ts: deque[str],
    active_tickets: dict[str, str],
    *,
    is_app_mention: bool = False,
    channel_type: str = "channel",
    user_cache: dict[str, str] | None = None,
    project_override: ProjectConfig | None = None,
    sessions: dict[str, SessionRecord] | None = None,
    session_by_thread: dict[str, str] | None = None,
    session_counter: dict[str, int] | None = None,
    all_bot_ids: set[str] | None = None,
) -> None:
    """Shared routing logic for message and app_mention events. May spawn engineer task."""
    channel = event.get("channel", "")
    project = project_override or channel_map.get(channel)
    if not project:
        logger.info("Dropped event: channel %s not in map", channel)
        return

    source_bot_id = source.get("bot_id", "")
    if source_bot_id and all_bot_ids and source_bot_id in all_bot_ids and not is_app_mention:
        return
    if own_bot_id and source_bot_id == own_bot_id:
        return

    try:
        parser = get_parser(project.platform)
    except ValueError as e:
        logger.error(
            "Failed to get message parser for platform %r (project %s): %s",
            project.platform,
            project.name,
            e,
        )
        return

    text = parser.extract_message_text(source)
    if not text:
        logger.info("Dropped event in #%s: no usable text", channel)
        return

    if not parser.should_forward(source, project):
        logger.info(
            "Dropped event in #%s: should_forward=False for project %s",
            channel,
            project.name,
        )
        return

    if not is_app_mention:
        is_bot_message = "bot_id" in source
        mention = f"<@{own_user_id}>"
        is_mentioned = mention in text
        if not is_bot_message and not is_mentioned:
            logger.info("Dropped event in #%s: not a bot message and not @mentioned", channel)
            return
        if is_mentioned:
            text = text.replace(mention, "").strip()
    else:
        mention = f"<@{own_user_id}>"
        text = text.replace(mention, "").strip()

    msg_ts = event.get("ts", "")
    ticket_id = parser.extract_ticket_id(source)
    if ticket_id is not None:
        ticket_key = f"{project.name}:{ticket_id}"
        if ticket_key in active_tickets:
            logger.info("Dropped event for active ticket %s", ticket_key)
            return
    else:
        ticket_key = None
        if msg_ts and msg_ts in seen_ts:
            logger.info("Dropped duplicate event in #%s (ts=%s)", channel, msg_ts)
            return
        if msg_ts:
            seen_ts.append(msg_ts)

    thread_ts = event.get("thread_ts") or event.get("ts")
    logger.info("Message in #%s for project '%s': %s", channel, project.name, text[:100])

    resume_session_id: str | None = None

    # Serialize per thread: if this thread's agent is still alive (running or draining its
    # inbox), deliver the message via the session inbox file so the live process picks it up
    # mid-run — instead of forking a second parallel claude process. Decided BEFORE the
    # idle-resume / DM-#ref paths below, which only handle the not-running cases.
    if thread_ts and sessions is not None and session_by_thread is not None:
        busy_ref = session_by_thread.get(f"{channel}:{thread_ts}")
        queued_text = text
        if channel_type == "im":
            dm_busy = re.match(r"^#(\d{4}-\d{2}-\d{2}/\d+)\s+(.*)", text, re.DOTALL)
            if dm_busy and sessions.get(dm_busy.group(1)):
                busy_ref = dm_busy.group(1)
                queued_text = dm_busy.group(2).strip()
        if busy_ref:
            busy_rec = sessions.get(busy_ref)
            if busy_rec and busy_rec.state in ("running", "draining") and busy_rec.inbox_path:
                queued_user = source.get("user", "") or source.get("username", "")
                _append_to_inbox(busy_rec.inbox_path, queued_user, queued_text, msg_ts)
                await _add_reaction(slack_client, channel, msg_ts, "eyes")
                logger.info(
                    "Thread #%s busy (%s) — queued message to inbox", busy_ref, busy_rec.state
                )
                return

    # app_mention events (human @mentions) always auto-resume in threads regardless of platform.
    # Plain message events in github/ADO channels may be integration bot posts — only auto-resume
    # those when platform is "slack". DM thread replies are also auto-resumed; fresh DMs are not.
    if (
        (channel_type != "im" or bool(event.get("thread_ts")))
        and (is_app_mention or project.platform == "slack")
        and thread_ts
        and session_by_thread is not None
        and sessions is not None
    ):
        existing_ref = session_by_thread.get(f"{channel}:{thread_ts}")
        if existing_ref:
            rec = sessions.get(existing_ref)
            if rec and rec.state == "idle" and rec.claude_session_id:
                resume_session_id = rec.claude_session_id
                logger.info("Thread auto-resume session #%s → %s", existing_ref, resume_session_id)

    # DM starting with "#YYYY-MM-DD/N <message>" resumes a named session.
    # Falls back to reading the log file when the record is not in memory (e.g. after restart).
    if channel_type == "im" and sessions is not None:
        dm_match = re.match(r"^#(\d{4}-\d{2}-\d{2}/\d+)\s+(.*)", text, re.DOTALL)
        if dm_match:
            target_ref = dm_match.group(1)
            remainder = dm_match.group(2).strip()
            rec = sessions.get(target_ref)
            if rec and rec.state == "idle" and rec.claude_session_id:
                resume_session_id = rec.claude_session_id
                text = remainder
                logger.info("DM explicit resume #%s", target_ref)
            else:
                date_part, num_part = SessionRecord.parse_ref(target_ref)
                log_path = os.path.join(
                    project.workspace,
                    "logs",
                    date_part,
                    _session_log_name(project.agent_name, num_part),
                )
                recovered_id = await asyncio.to_thread(_session_id_from_log, log_path)
                if recovered_id:
                    resume_session_id = recovered_id
                    text = remainder
                    logger.info("Log-file recovery for #%s → %s", target_ref, recovered_id)
                else:
                    logger.info("No session found for #%s — treating as new message", target_ref)

    trigger_user = source.get("user", "") or source.get("username", "")
    # Acknowledge pickup with a reaction on the triggering message (instead of a text post),
    # here at the routing layer — BEFORE the spawn task and its concurrency semaphore — so it
    # fires immediately for every routed message regardless of `follow_thread` or how many
    # slots a long-running agent is holding. The busy/queue path above acks separately and
    # returns, so a message is acked at exactly one of the two sites, never both.
    await _add_reaction(slack_client, channel, msg_ts, "eyes")
    task = asyncio.create_task(
        spawn_engineer(
            project,
            text,
            channel,
            thread_ts,
            slack_client,
            semaphore,
            ticket_key=ticket_key,
            active_tickets=active_tickets,
            channel_type=channel_type,
            event_ts=msg_ts,
            trigger_user=trigger_user,
            user_cache=user_cache,
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter=session_counter,
            resume_session_id=resume_session_id,
        )
    )
    if ticket_key and msg_ts:
        active_tickets[ticket_key] = msg_ts
    tasks_ref.add(task)
    task.add_done_callback(tasks_ref.discard)


def _register_handlers(
    app: AsyncApp,
    channel_map: dict[str, ProjectConfig],
    dm_project: ProjectConfig | None,
    own_bot_id: str,
    own_user_id: str,
    semaphore: asyncio.Semaphore,
    tasks_ref: set[asyncio.Task[None]],
    seen_ts: deque[str],
    active_tickets: dict[str, str],
    user_cache: dict[str, str],
    sessions: dict[str, SessionRecord],
    session_by_thread: dict[str, str],
    session_counter: dict[str, int],
    all_bot_ids: set[str] | None = None,
) -> None:
    """Register Slack event handlers on the given app instance (one per token group)."""

    @app.middleware
    async def log_all_events(payload: dict[str, Any], next: Any) -> None:
        event_type = payload.get("event", {}).get("type")
        logger.debug("Incoming payload type=%s event_type=%s", payload.get("type"), event_type)
        await next()

    @app.event("message")
    async def handle_message(event: dict[str, Any], ack: Any) -> None:
        await ack()
        logger.debug("handle_message event=%s", event)
        channel = event.get("channel", "")
        channel_type = event.get("channel_type", "")
        logger.info("Event received: message channel=%s", channel)
        subtype = event.get("subtype")
        if subtype == "message_deleted":
            return
        if own_bot_id and event.get("bot_id") == own_bot_id:
            return

        if channel_type == "im":
            if dm_project is None:
                return
            await _try_route_event(
                event,
                event,
                channel_map,
                own_bot_id,
                own_user_id,
                app.client,
                semaphore,
                tasks_ref,
                seen_ts,
                active_tickets,
                is_app_mention=True,
                channel_type=channel_type,
                user_cache=user_cache,
                project_override=dm_project,
                sessions=sessions,
                session_by_thread=session_by_thread,
                session_counter=session_counter,
                all_bot_ids=all_bot_ids,
            )
            return

        if subtype == "message_changed":
            source = event["message"]
            prev = event.get("previous_message", {})
            if _is_unchanged_message_edit(source, prev, channel_map, channel):
                return
        else:
            source = event

        await _try_route_event(
            event,
            source,
            channel_map,
            own_bot_id,
            own_user_id,
            app.client,
            semaphore,
            tasks_ref,
            seen_ts,
            active_tickets,
            is_app_mention=False,
            channel_type=channel_type,
            user_cache=user_cache,
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter=session_counter,
            all_bot_ids=all_bot_ids,
        )

    @app.event("app_mention")
    async def handle_app_mention(event: dict[str, Any], ack: Any) -> None:
        await ack()
        logger.debug("handle_app_mention event=%s", event)
        logger.info("Event received: app_mention channel=%s", event.get("channel"))
        channel_type = event.get("channel_type", "channel")
        await _try_route_event(
            event,
            event,
            channel_map,
            own_bot_id,
            own_user_id,
            app.client,
            semaphore,
            tasks_ref,
            seen_ts,
            active_tickets,
            is_app_mention=True,
            channel_type=channel_type,
            user_cache=user_cache,
            sessions=sessions,
            session_by_thread=session_by_thread,
            session_counter=session_counter,
            all_bot_ids=all_bot_ids,
        )


async def supervise_slack(
    handler: AsyncSocketModeHandler,
    shutdown_event: asyncio.Event,
) -> None:
    backoff = 1
    while not shutdown_event.is_set():
        try:
            await handler.start_async()  # type: ignore[no-untyped-call]
            backoff = 1
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Slack handler crashed: %s. Restarting in %ds", e, backoff)
        if shutdown_event.is_set():
            return
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


async def main() -> None:
    # Load router/.env into os.environ so single-agent schema tokens are available.
    load_dotenv(dotenv_path=os.path.join(ROUTER_DIR, ".env"))

    arg_parser = argparse.ArgumentParser(description="Slack-to-agent router")
    arg_parser.add_argument("--config", default=ROUTER_CONFIG, help="Path to router config.yaml")
    args = arg_parser.parse_args()
    config_path = args.config

    # Logging setup
    try:
        router_cfg = _read_yaml(config_path)
    except Exception as e:
        print(f"WARNING: Could not read {config_path}: {e}; using defaults", file=sys.stderr)
        router_cfg = {}
    log_level = getattr(logging, router_cfg.get("log_level", "INFO").upper(), logging.INFO)
    log_dir = router_cfg.get("log_dir", os.path.join(ROUTER_DIR, "logs"))
    max_concurrent = int(router_cfg.get("max_concurrent", 3))
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(
        level=log_level,
        format=fmt,
        handlers=[DailyDirectoryFileHandler(log_dir), logging.StreamHandler()],
    )

    logger.info("Router starting up")

    # Graceful shutdown
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    # Load projects from router config
    projects = load_projects(config_path)
    if not projects:
        logger.error("No valid projects configured in %s. Exiting.", config_path)
        return

    # Group projects by (bot_token, app_token) — one Slack App per unique token pair.
    token_groups: dict[tuple[str, str], list[ProjectConfig]] = {}
    for p in projects:
        key = (p.slack_bot_token, p.slack_app_token)
        token_groups.setdefault(key, []).append(p)

    semaphore = asyncio.Semaphore(max_concurrent)
    _tasks: set[asyncio.Task[None]] = set()
    supervision_tasks: list[asyncio.Task[None]] = []
    handlers: list[AsyncSocketModeHandler] = []

    # Build one AsyncApp per token group upfront, then resolve all bot identities
    # in parallel so each handler can filter messages from *any* of our own agents.
    valid_groups = {
        key: (AsyncApp(token=key[0]), projs)
        for key, projs in token_groups.items()
        if key[0] and key[1]
    }
    invalid_groups = {
        key: projs for key, projs in token_groups.items() if not (key[0] and key[1])
    }

    auth_results = await asyncio.gather(
        *[app.client.auth_test() for app, _ in valid_groups.values()],
        return_exceptions=True,
    )

    token_identities: dict[tuple[str, str], tuple[str, str]] = {}
    agent_mention_map: dict[str, str] = {}
    for (key, (_, group_projs)), result in zip(valid_groups.items(), auth_results):
        if isinstance(result, BaseException):
            logger.error("Failed to resolve bot ID for token group: %s", result)
            token_identities[key] = ("", "")
        else:
            bot_id = result.get("bot_id", "")
            uid = result.get("user_id", "")
            token_identities[key] = (bot_id, uid)
            if uid:
                for p in group_projs:
                    agent_mention_map[p.agent_name] = uid

    all_bot_ids: set[str] = {bid for bid, _ in token_identities.values() if bid}
    logger.info("Known router bot_ids: %s", all_bot_ids)

    for p in projects:
        p.mentions = agent_mention_map
    logger.info("Agent mention map: %s", agent_mention_map)

    for key, projs in invalid_groups.items():
        logger.error("Missing Slack tokens for projects %s — skipping", [p.name for p in projs])

    for (bot_token, app_token), (app, group_projects) in valid_groups.items():
        key = (bot_token, app_token)
        own_bot_id, own_user_id = token_identities.get(key, ("", ""))
        if own_bot_id or own_user_id:
            logger.info(
                "Bot identity for [%s]: bot_id=%s user_id=%s",
                ", ".join(p.agent_name for p in group_projects),
                own_bot_id or "(not set)",
                own_user_id or "(not set)",
            )

        channel_map = await resolve_channels(app.client, group_projects)
        if not channel_map:
            logger.warning("No channels resolved for projects %s", [p.name for p in group_projects])

        dm_project: ProjectConfig | None = next(
            (p for p in group_projects if p.platform == "slack"),
            group_projects[0],
        )

        _seen_ts: deque[str] = deque(maxlen=1000)
        _active_tickets: dict[str, str] = {}
        _user_cache: dict[str, str] = {}
        _sessions: dict[str, SessionRecord] = {}
        _session_by_thread: dict[str, str] = {}
        _session_counter: dict[str, int] = {}

        _register_handlers(
            app,
            channel_map,
            dm_project,
            own_bot_id,
            own_user_id,
            semaphore,
            _tasks,
            _seen_ts,
            _active_tickets,
            _user_cache,
            _sessions,
            _session_by_thread,
            _session_counter,
            all_bot_ids=all_bot_ids,
        )

        handler = AsyncSocketModeHandler(app, app_token)
        handlers.append(handler)
        supervision_tasks.append(asyncio.create_task(supervise_slack(handler, shutdown_event)))

    if not supervision_tasks:
        logger.error("No Slack apps started. Exiting.")
        return

    unique_project_names = list(dict.fromkeys(p.name for p in projects))
    logger.info(
        "Router ready — %d Slack app(s), %d project(s), %d agent(s)",
        len(supervision_tasks),
        len(unique_project_names),
        len(projects),
    )

    try:
        await shutdown_event.wait()
    finally:
        shutdown_event.set()
        for t in supervision_tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*supervision_tasks, return_exceptions=True)
        for h in handlers:
            with contextlib.suppress(Exception):
                await h.close_async()  # type: ignore[no-untyped-call]
        logger.info("Router shut down cleanly")


if __name__ == "__main__":
    asyncio.run(main())

"""Tests for the workspace inbox hook scripts (orcai/projects/.orcai/hooks).

These shell scripts are the router→live-agent delivery reinforcement: the Stop hook keeps a
finishing agent alive to handle queued messages, and the PostToolUse hook injects them mid-run.
They are invoked here as real subprocesses (the way Claude Code runs them).
"""
import json
import subprocess
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[2] / "projects" / ".orcai" / "hooks"
DRAIN = HOOKS_DIR / "inbox_drain.sh"
INJECT = HOOKS_DIR / "inbox_inject.sh"
OUTBOX_SAY = HOOKS_DIR / "outbox_say.py"
OUTBOX_WAKE = HOOKS_DIR / "outbox_wake.py"


def _run_say(outbox: str | None, *args: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    if outbox is not None:
        env["ORCAI_OUTBOX"] = outbox
    return subprocess.run(
        ["python3", str(OUTBOX_SAY), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _run(script: Path, inbox: str | None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    if inbox is not None:
        env["ORCAI_INBOX"] = inbox
    return subprocess.run(
        ["bash", str(script)],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _write_inbox(path: Path, *messages: tuple[str, str]) -> None:
    path.write_text(
        "".join(json.dumps({"ts": "1.0", "user": u, "text": t}) + "\n" for u, t in messages)
    )


def test_inbox_drain_hook_blocks_when_nonempty(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.jsonl"
    _write_inbox(inbox, ("alice", "also add logging"), ("bob", "and fix the typo"))

    result = _run(DRAIN, str(inbox))

    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block"
    assert "also add logging" in out["reason"]
    assert "and fix the typo" in out["reason"]
    assert out["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert not inbox.exists() or inbox.read_text() == ""  # claimed/emptied


def test_inbox_drain_hook_silent_when_empty(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.jsonl"
    inbox.write_text("")  # exists but empty

    result = _run(DRAIN, str(inbox))

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_inbox_drain_hook_silent_when_missing(tmp_path: Path) -> None:
    result = _run(DRAIN, str(tmp_path / "nope.jsonl"))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_inbox_drain_hook_noop_when_unset(tmp_path: Path) -> None:
    result = _run(DRAIN, None)  # ORCAI_INBOX unset
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_inbox_inject_hook_emits_additional_context(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.jsonl"
    _write_inbox(inbox, ("carol", "ping"))

    result = _run(INJECT, str(inbox))

    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "ping" in out["hookSpecificOutput"]["additionalContext"]
    assert "decision" not in out  # non-blocking
    assert not inbox.exists() or inbox.read_text() == ""


def test_inbox_inject_hook_noop_when_empty(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.jsonl"
    inbox.write_text("")
    result = _run(INJECT, str(inbox))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_outbox_say_appends_progress_line(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    result = _run_say(str(outbox), "1/3 working")
    assert result.returncode == 0
    rec = json.loads(outbox.read_text().splitlines()[-1])
    assert rec == {"text": "1/3 working", "dm": False}


def test_outbox_say_dm_flag_sets_dm_true(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    result = _run_say(str(outbox), "--dm", "need a decision")
    assert result.returncode == 0
    rec = json.loads(outbox.read_text().splitlines()[-1])
    assert rec == {"text": "need a decision", "dm": True}


def test_outbox_say_joins_multiword_message(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    _run_say(str(outbox), "phase", "two", "done")
    rec = json.loads(outbox.read_text().splitlines()[-1])
    assert rec["text"] == "phase two done"


def test_outbox_say_noop_when_unset(tmp_path: Path) -> None:
    result = _run_say(None, "nothing should happen")  # ORCAI_OUTBOX unset
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def _run_wake(wake: str | None, *args: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    if wake is not None:
        env["ORCAI_WAKE"] = wake
    return subprocess.run(
        ["python3", str(OUTBOX_WAKE), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_outbox_wake_writes_request(tmp_path: Path) -> None:
    wake = tmp_path / "wake.json"
    result = _run_wake(str(wake), "--delay", "1200", "--reason", "r", "--prompt", "p")
    assert result.returncode == 0
    assert json.loads(wake.read_text()) == {
        "delay_seconds": 1200,
        "reason": "r",
        "prompt": "p",
    }


def test_outbox_wake_last_write_wins(tmp_path: Path) -> None:
    wake = tmp_path / "wake.json"
    _run_wake(str(wake), "--delay", "1200", "--reason", "first", "--prompt", "p1")
    _run_wake(str(wake), "--delay", "50", "--reason", "second", "--prompt", "p2")
    # Overwritten, not appended: a single JSON object reflecting only the latest request.
    data = json.loads(wake.read_text())
    assert data == {"delay_seconds": 50, "reason": "second", "prompt": "p2"}


def test_outbox_wake_defaults_blank_reason_prompt(tmp_path: Path) -> None:
    wake = tmp_path / "wake.json"
    result = _run_wake(str(wake), "--delay", "300")
    assert result.returncode == 0
    assert json.loads(wake.read_text()) == {
        "delay_seconds": 300,
        "reason": "",
        "prompt": "",
    }


def test_outbox_wake_noop_when_unset(tmp_path: Path) -> None:
    result = _run_wake(None, "--delay", "300")  # ORCAI_WAKE unset
    assert result.returncode == 0
    assert result.stdout.strip() == ""

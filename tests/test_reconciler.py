"""Tests for state.reconciler — sweeps orphaned state files and resets
stuck WAITING_* statuses when agent_pid is dead."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ephor.state import reconciler


def _write(path: Path, **fields: Any) -> None:
    base: dict[str, Any] = {
        "schema_version": 2,
        "session_id": path.stem,
        "cwd": "/tmp/x",
        "started_at": "2026-04-29T10:00:00Z",
        "status": "IDLE",
        "project_name": "x",
        "last_event": "Stop",
        "last_event_time": "2026-04-29T10:00:00Z",
        "last_event_seq": 1,
        "tool_count": 0,
        "error_count": 0,
        "tmux_session": None,
        "tmux_window": None,
        "tmux_pane": None,
        "agent_pid": None,
        "notification": None,
        "last_summary": "",
    }
    base.update(fields)
    path.write_text(json.dumps(base))


def _iso_seconds_ago(seconds: int) -> str:
    ts = datetime.now(UTC) - timedelta(seconds=seconds)
    return ts.isoformat().replace("+00:00", "Z")


def test_returns_zero_counts_for_missing_dir(tmp_path: Path) -> None:
    result = reconciler.reconcile(tmp_path / "does-not-exist")
    assert result.deleted == 0
    assert result.reset == 0
    assert not result.changed


def test_leaves_live_pid_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A session with a live agent_pid must never be deleted or reset."""
    monkeypatch.setattr(reconciler, "_is_pid_alive", lambda pid: True)
    _write(
        tmp_path / "live.json",
        agent_pid=999,
        status="WAITING_PERMISSION",
        last_event_time=_iso_seconds_ago(3600),
    )
    result = reconciler.reconcile(tmp_path, file_stale_sec=60)
    assert result.deleted == 0
    assert result.reset == 0
    assert (tmp_path / "live.json").exists()
    after = json.loads((tmp_path / "live.json").read_text())
    assert after["status"] == "WAITING_PERMISSION"


def test_deletes_dead_pid_files_past_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dead pid + last event > threshold → unlink."""
    monkeypatch.setattr(reconciler, "_is_pid_alive", lambda pid: False)
    _write(tmp_path / "old.json", agent_pid=42, last_event_time=_iso_seconds_ago(3600))
    _write(tmp_path / "fresh.json", agent_pid=43, last_event_time=_iso_seconds_ago(5))

    result = reconciler.reconcile(tmp_path, file_stale_sec=60)
    assert result.deleted == 1
    assert not (tmp_path / "old.json").exists()
    # Fresh dead-pid files survive — they're inside the grace window.
    assert (tmp_path / "fresh.json").exists()


def test_resets_stuck_waiting_permission_when_pid_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clorch race: PermissionRequest doesn't fire Stop on denial.
    Reset to IDLE, but only when the pid is confirmed dead and we're inside
    the deletion grace window (otherwise the file would be deleted instead)."""
    monkeypatch.setattr(reconciler, "_is_pid_alive", lambda pid: False)
    _write(
        tmp_path / "stuck.json",
        agent_pid=42,
        status="WAITING_PERMISSION",
        last_event_time=_iso_seconds_ago(5),  # inside grace window
        notification={"type": "permission", "tool": "Bash", "redacted_summary": None},
    )

    result = reconciler.reconcile(tmp_path, file_stale_sec=60)
    assert result.reset == 1
    assert result.deleted == 0
    after = json.loads((tmp_path / "stuck.json").read_text())
    assert after["status"] == "IDLE"
    assert after["notification"] is None


def test_resets_waiting_answer_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconciler, "_is_pid_alive", lambda pid: False)
    _write(
        tmp_path / "stuck.json",
        agent_pid=42,
        status="WAITING_ANSWER",
        last_event_time=_iso_seconds_ago(5),
    )
    result = reconciler.reconcile(tmp_path, file_stale_sec=60)
    assert result.reset == 1


def test_does_not_reset_other_statuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ERROR, WORKING, IDLE — none of those should be touched, only WAITING_*."""
    monkeypatch.setattr(reconciler, "_is_pid_alive", lambda pid: False)
    _write(
        tmp_path / "errored.json",
        agent_pid=42,
        status="ERROR",
        last_event_time=_iso_seconds_ago(5),
    )
    result = reconciler.reconcile(tmp_path, file_stale_sec=60)
    assert result.reset == 0
    after = json.loads((tmp_path / "errored.json").read_text())
    assert after["status"] == "ERROR"


def test_skips_files_without_claude_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-pid state files (agent_pid is None) shouldn't be deleted —
    they need a different recovery path (ephor refresh-tmux)."""
    monkeypatch.setattr(reconciler, "_is_pid_alive", lambda pid: False)
    _write(
        tmp_path / "ancient.json",
        agent_pid=None,
        last_event_time=_iso_seconds_ago(86400),
    )
    result = reconciler.reconcile(tmp_path, file_stale_sec=60)
    assert result.deleted == 0
    assert (tmp_path / "ancient.json").exists()


def test_ignores_atomic_write_tempfiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`.tmp.*` files come from concurrent atomic-write writers; never touch."""
    monkeypatch.setattr(reconciler, "_is_pid_alive", lambda pid: False)
    (tmp_path / ".tmp.abc.json").write_text("garbage not even json")
    result = reconciler.reconcile(tmp_path, file_stale_sec=60)
    assert result.deleted == 0
    assert (tmp_path / ".tmp.abc.json").exists()


def test_corrupt_json_is_skipped_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A garbled state file shouldn't blow up reconcile — and shouldn't be
    deleted (could be a half-written hook event in flight)."""
    monkeypatch.setattr(reconciler, "_is_pid_alive", lambda pid: False)
    (tmp_path / "garbled.json").write_text("{not json")
    result = reconciler.reconcile(tmp_path, file_stale_sec=60)
    assert result.deleted == 0
    assert result.reset == 0
    assert (tmp_path / "garbled.json").exists()


def test_falls_back_to_mtime_when_timestamp_unparseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If last_event_time is junk, fall back to file mtime so we don't
    keep a clearly-stale orphan around forever."""
    import os
    import time as _time

    monkeypatch.setattr(reconciler, "_is_pid_alive", lambda pid: False)
    p = tmp_path / "garbage_ts.json"
    _write(p, agent_pid=42, last_event_time="not-a-date")
    # Backdate mtime well past the threshold.
    old = _time.time() - 7200
    os.utime(p, (old, old))

    result = reconciler.reconcile(tmp_path, file_stale_sec=60)
    assert result.deleted == 1


# ---- resume residue cleanup --------------------------------------------


def test_deletes_stale_resume_residue_when_pid_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two state files share a live PID (claude --resume case). The
    older sibling is past the stale threshold → it should be deleted."""
    monkeypatch.setattr(reconciler, "_is_pid_alive", lambda pid: True)
    new = tmp_path / "new.json"
    old = tmp_path / "old.json"
    _write(new, agent_pid=12345, last_event_time=_iso_seconds_ago(5))  # fresh
    _write(old, agent_pid=12345, last_event_time=_iso_seconds_ago(120))  # stale

    result = reconciler.reconcile(tmp_path, file_stale_sec=60)

    assert new.exists(), "winning sibling must survive"
    assert not old.exists(), "stale resume residue must be deleted"
    assert result.deleted == 1


def test_keeps_recent_resume_siblings_until_grace_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Right after a resume, both files are recent. We don't delete
    immediately — give the user time to react / hooks to settle. The
    in-memory dedup in scan() hides the duplicate from the dashboard
    in the meantime."""
    monkeypatch.setattr(reconciler, "_is_pid_alive", lambda pid: True)
    new = tmp_path / "new.json"
    old = tmp_path / "old.json"
    _write(new, agent_pid=12345, last_event_time=_iso_seconds_ago(5))
    _write(old, agent_pid=12345, last_event_time=_iso_seconds_ago(20))  # not yet stale

    result = reconciler.reconcile(tmp_path, file_stale_sec=60)

    assert new.exists()
    assert old.exists()
    assert result.deleted == 0


def test_distinct_live_pids_are_never_treated_as_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two separate claude processes (different PIDs) must both be
    preserved indefinitely — no false-positive residue cleanup."""
    monkeypatch.setattr(reconciler, "_is_pid_alive", lambda pid: True)
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    _write(a, agent_pid=1001, last_event_time=_iso_seconds_ago(1000))  # very old
    _write(b, agent_pid=1002, last_event_time=_iso_seconds_ago(1000))

    result = reconciler.reconcile(tmp_path, file_stale_sec=60)

    assert a.exists()
    assert b.exists()
    assert result.deleted == 0

"""Tests for StateManager scan + sort + corruption tolerance."""

from __future__ import annotations

from pathlib import Path

import pytest

from ephor.constants import AgentStatus
from ephor.state.manager import StateManager
from ephor.state.models import AgentState


def _write_state(directory: Path, sid: str, **kwargs: object) -> Path:
    base = {
        "session_id": sid,
        "cwd": "/tmp/x",
        "started_at": "2026-04-29T10:00:00Z",
    }
    base.update(kwargs)
    state = AgentState(**base)  # type: ignore[arg-type]
    p = directory / f"{sid}.json"
    p.write_text(state.to_json())
    return p


def test_scan_empty_dir_returns_empty_list(tmp_path: Path) -> None:
    assert StateManager(tmp_path).scan() == []


def test_scan_missing_dir_returns_empty_list(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert StateManager(missing).scan() == []


def test_scan_returns_all_state_files(tmp_path: Path) -> None:
    _write_state(tmp_path, "a", status=AgentStatus.WORKING)
    _write_state(tmp_path, "b", status=AgentStatus.IDLE)
    _write_state(tmp_path, "c", status=AgentStatus.WAITING_PERMISSION)
    agents = StateManager(tmp_path).scan()
    assert {a.session_id for a in agents} == {"a", "b", "c"}


def test_scan_sorts_by_last_event_time_desc(tmp_path: Path) -> None:
    _write_state(tmp_path, "old", last_event_time="2026-04-29T08:00:00Z")
    _write_state(tmp_path, "new", last_event_time="2026-04-29T20:00:00Z")
    _write_state(tmp_path, "mid", last_event_time="2026-04-29T12:00:00Z")
    agents = StateManager(tmp_path).scan()
    assert [a.session_id for a in agents] == ["new", "mid", "old"]


def test_scan_skips_corrupt_files(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write_state(tmp_path, "good", status=AgentStatus.WORKING)
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "wrong-schema.json").write_text(
        '{"schema_version": 999, "session_id": "x", "cwd": "/", "started_at": "now"}'
    )
    agents = StateManager(tmp_path).scan()
    assert {a.session_id for a in agents} == {"good"}
    assert any("Skipping corrupt state file" in r.message for r in caplog.records)


def test_scan_skips_atomic_tempfiles(tmp_path: Path) -> None:
    _write_state(tmp_path, "real", status=AgentStatus.IDLE)
    # Simulate an in-flight atomic write.
    (tmp_path / ".tmp.abc123.json").write_text("not_real_yet")
    agents = StateManager(tmp_path).scan()
    assert [a.session_id for a in agents] == ["real"]


def test_get_summary_aggregates(tmp_path: Path) -> None:
    _write_state(tmp_path, "a", status=AgentStatus.WORKING)
    _write_state(tmp_path, "b", status=AgentStatus.WORKING)
    _write_state(tmp_path, "c", status=AgentStatus.WAITING_PERMISSION)
    summary = StateManager(tmp_path).get_summary()
    assert summary.working == 2
    assert summary.waiting_permission == 1
    assert summary.total == 3


# ---------------------------------------------------------------------------
# liveness check (agent_pid → DEAD when process is gone)
# ---------------------------------------------------------------------------


def test_scan_marks_dead_when_pid_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ephor.state import manager as mgr_mod

    _write_state(tmp_path, "ghost", status=AgentStatus.WORKING, agent_pid=99999999)

    # Simulate the pid being dead.
    monkeypatch.setattr(mgr_mod, "_is_pid_alive", lambda pid: False)

    agents = StateManager(tmp_path).scan()
    assert agents[0].status is AgentStatus.DEAD


def test_scan_keeps_status_when_pid_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ephor.state import manager as mgr_mod

    _write_state(tmp_path, "alive", status=AgentStatus.WORKING, agent_pid=42)
    monkeypatch.setattr(mgr_mod, "_is_pid_alive", lambda pid: True)

    agents = StateManager(tmp_path).scan()
    assert agents[0].status is AgentStatus.WORKING


def test_scan_keeps_status_when_pid_unknown(tmp_path: Path) -> None:
    """No agent_pid recorded → can't tell, leave status as-is."""
    _write_state(tmp_path, "noinfo", status=AgentStatus.WORKING)  # agent_pid None
    agents = StateManager(tmp_path).scan()
    assert agents[0].status is AgentStatus.WORKING


def test_is_pid_alive_with_self() -> None:
    """Self-test: our own pid should always read as alive."""
    import os

    from ephor.state.manager import _is_pid_alive

    assert _is_pid_alive(os.getpid()) is True


def test_is_pid_alive_with_zero() -> None:
    from ephor.state.manager import _is_pid_alive

    assert _is_pid_alive(0) is False


def test_is_pid_alive_with_obviously_dead() -> None:
    """A reasonably-large pid that almost certainly doesn't exist."""
    from ephor.state.manager import _is_pid_alive

    # 2^22 is below typical max_pid but generally unused.
    assert _is_pid_alive(4194301) is False


# ---- resume residue (claude --resume reuses a PID, fresh session_id) ----


def test_scan_marks_older_sibling_dead_when_pids_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: claude --resume reuses the parent shell's PID, so the new
    session_id and the old one both reference the same live agent_pid.
    Both render → duplicate rows in the dashboard. The newer sibling
    (latest last_event_time) wins; older siblings get DEAD so the TUI
    hides them."""
    from ephor.state import manager as mgr_mod

    _write_state(
        tmp_path,
        "old-sid",
        status=AgentStatus.IDLE,
        agent_pid=12345,
        last_event_time="2026-05-08T13:00:00Z",
    )
    _write_state(
        tmp_path,
        "new-sid",
        status=AgentStatus.WORKING,
        agent_pid=12345,
        last_event_time="2026-05-08T14:00:00Z",
    )
    monkeypatch.setattr(mgr_mod, "_is_pid_alive", lambda pid: True)

    agents = StateManager(tmp_path).scan()
    by_sid = {a.session_id: a for a in agents}
    assert by_sid["new-sid"].status is AgentStatus.WORKING
    assert by_sid["old-sid"].status is AgentStatus.DEAD


def test_scan_keeps_distinct_pids_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two genuinely separate claude processes (different PIDs) must
    BOTH render — only collisions on the same PID are dedup'd."""
    from ephor.state import manager as mgr_mod

    _write_state(tmp_path, "a", status=AgentStatus.WORKING, agent_pid=1001)
    _write_state(tmp_path, "b", status=AgentStatus.IDLE, agent_pid=1002)
    monkeypatch.setattr(mgr_mod, "_is_pid_alive", lambda pid: True)

    agents = StateManager(tmp_path).scan()
    statuses = {a.session_id: a.status for a in agents}
    assert statuses == {"a": AgentStatus.WORKING, "b": AgentStatus.IDLE}


def test_scan_does_not_dedupe_when_pids_are_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the shared PID is dead, both are already DEAD via the existing
    liveness check — dedup logic must not double-process them."""
    from ephor.state import manager as mgr_mod

    _write_state(
        tmp_path,
        "ghost-old",
        status=AgentStatus.IDLE,
        agent_pid=99999,
        last_event_time="2026-05-08T13:00:00Z",
    )
    _write_state(
        tmp_path,
        "ghost-new",
        status=AgentStatus.IDLE,
        agent_pid=99999,
        last_event_time="2026-05-08T14:00:00Z",
    )
    monkeypatch.setattr(mgr_mod, "_is_pid_alive", lambda pid: False)

    agents = StateManager(tmp_path).scan()
    assert all(a.status is AgentStatus.DEAD for a in agents)


def test_scan_dedup_handles_three_way_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User who's resumed twice in the same shell could leave three
    state files for one PID. Only the most recent one survives."""
    from ephor.state import manager as mgr_mod

    _write_state(tmp_path, "v1", agent_pid=2001, last_event_time="2026-05-08T10:00:00Z")
    _write_state(tmp_path, "v2", agent_pid=2001, last_event_time="2026-05-08T11:00:00Z")
    _write_state(tmp_path, "v3", agent_pid=2001, last_event_time="2026-05-08T12:00:00Z")
    monkeypatch.setattr(mgr_mod, "_is_pid_alive", lambda pid: True)

    agents = StateManager(tmp_path).scan()
    live = [a for a in agents if a.status is not AgentStatus.DEAD]
    assert len(live) == 1
    assert live[0].session_id == "v3"

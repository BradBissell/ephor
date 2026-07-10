"""Tests for tmux navigation. Heavy use of monkeypatch on `subprocess.run`
so we don't depend on a real tmux server during unit tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from ephor.constants import AgentStatus
from ephor.state.models import AgentState
from ephor.tmux import navigator
from ephor.tmux.navigator import JumpResult, jump_to, kill_session


def _agent(**overrides: Any) -> AgentState:
    base = {
        "session_id": "s1",
        "cwd": "/tmp/x",
        "started_at": "2026-04-29T10:00:00Z",
        "status": AgentStatus.WORKING,
        "tmux_session": "work",
        "tmux_window": "claude",
        "tmux_pane": "%1",
    }
    base.update(overrides)
    return AgentState(**base)


@pytest.fixture
def fake_tmux_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(navigator, "has_tmux", lambda: True)


def test_jump_returns_no_tmux_info_when_state_lacks_pane(
    fake_tmux_present: None,
) -> None:
    a = _agent(tmux_session=None, tmux_window=None, tmux_pane=None)
    outcome = jump_to(a)
    assert outcome.result is JumpResult.NO_TMUX_INFO
    assert not outcome.ok


def test_jump_returns_tmux_missing_when_tmux_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(navigator, "has_tmux", lambda: False)
    outcome = jump_to(_agent())
    assert outcome.result is JumpResult.TMUX_MISSING


def test_jump_returns_session_not_found(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        # has-session returns non-zero (session doesn't exist).
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="no")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = jump_to(_agent())
    assert outcome.result is JumpResult.SESSION_NOT_FOUND


def test_jump_ok_with_session_window_and_pane(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = jump_to(_agent(tmux_session="work", tmux_window="claude", tmux_pane="%1"))

    assert outcome.ok
    # has-session, select-window, select-pane in that order.
    # select-window targets the pane_id (not session:windowname) to avoid
    # ambiguity when multiple windows share the same auto-renamed name.
    assert any("has-session" in c for c in calls)
    assert any("select-window" in c and "%1" in c for c in calls)
    assert any("select-pane" in c and "%1" in c for c in calls)


def test_jump_ok_when_pane_missing_still_selects_window(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = jump_to(_agent(tmux_pane=None))
    assert outcome.ok
    assert any("select-window" in c for c in calls)
    assert not any("select-pane" in c for c in calls)


def test_jump_uses_pane_id_avoiding_duplicate_window_name_failure(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: when several windows in a session share the same auto-renamed
    name (e.g. all 'claude'), `select-window -t sess:claude` fails ambiguously
    with 'can't find window: claude'. Targeting the pane_id sidesteps the
    name collision since pane_ids are unique server-wide."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        # Simulate the real-world failure: name-based targeting fails, pane-id
        # targeting succeeds.
        if "select-window" in args and any(a == "work:claude" for a in args):
            return subprocess.CompletedProcess(args, 1, "", "can't find window: claude")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = jump_to(_agent(tmux_session="work", tmux_window="claude", tmux_pane="%24"))

    assert outcome.ok, outcome.detail
    # Must NOT have used the ambiguous name-based target.
    assert not any("select-window" in c and "work:claude" in c for c in calls)
    # Must have used the unambiguous pane-id target.
    assert any("select-window" in c and "%24" in c for c in calls)


def test_jump_failed_when_select_window_errors(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if "has-session" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        if "select-window" in args:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = jump_to(_agent())
    assert outcome.result is JumpResult.FAILED
    assert "boom" in outcome.detail


def test_jump_fails_when_pane_no_longer_hosts_claude_pid(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: pane_id is valid (select-window would succeed) but the
    pane was reused by a different claude. Without validation, the user
    silently lands in the wrong window. With validation, jump_to fails
    with 'can't find pane' so the caller can rediscover."""
    monkeypatch.setattr(navigator, "_pane_hosts_pid", lambda *_: False)

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if "has-session" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        # select-window must NOT be reached when validation fails.
        if "select-window" in args:
            raise AssertionError("select-window was called even though pane validation failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = jump_to(_agent(agent_pid=4242, tmux_pane="%9"))

    assert outcome.result is JumpResult.FAILED
    # Marker that triggers the rediscover path in _is_stale_tmux_ref.
    assert "can't find pane" in outcome.detail.lower()


def test_jump_proceeds_when_pane_hosts_claude_pid(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation passes — select-window runs as before."""
    monkeypatch.setattr(navigator, "_pane_hosts_pid", lambda *_: True)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = jump_to(_agent(agent_pid=4242, tmux_pane="%9"))

    assert outcome.ok
    assert any("select-window" in c and "%9" in c for c in calls)


def test_jump_skips_pane_validation_when_claude_pid_unknown(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When state never recorded a agent_pid, fall back to trusting the
    recorded pane_id (preserves prior behavior for pre-pid state files)."""

    def boom(*_: object) -> bool:
        raise AssertionError("validation should be skipped without agent_pid")

    monkeypatch.setattr(navigator, "_pane_hosts_pid", boom)

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = jump_to(_agent(agent_pid=None, tmux_pane="%9"))
    assert outcome.ok


def test_pid_descends_from_walks_ppid_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_pid_descends_from` walks PPID up to the depth cap, not forever."""
    chain = {100: 50, 50: 10, 10: 1}
    monkeypatch.setattr(navigator, "_read_ppid", lambda pid: chain.get(pid))
    assert navigator._pid_descends_from(100, 10)
    assert navigator._pid_descends_from(100, 50)
    assert navigator._pid_descends_from(100, 100)  # self-match
    assert not navigator._pid_descends_from(100, 999)
    assert not navigator._pid_descends_from(100, 1)  # walks stop at PID <= 1


# ---- kill_session ---------------------------------------------------------


def _write_state_file(directory: Path, sid: str) -> Path:
    p = directory / f"{sid}.json"
    p.write_text("{}")
    return p


def test_kill_session_signals_pid_kills_window_unlinks_state(
    fake_tmux_present: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_file = _write_state_file(tmp_path, "s1")

    signaled: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "ephor.tmux.navigator.os.kill",
        lambda pid, sig: signaled.append((pid, sig)),
    )

    tmux_calls: list[list[str]] = []

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        tmux_calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    agent = _agent(agent_pid=4242)
    outcome = kill_session(agent, tmp_path)

    assert outcome.ok
    assert signaled == [(4242, 15)]  # SIGTERM
    assert any("kill-window" in c for c in tmux_calls)
    # kill-window targets pane_id, not session:windowname.
    assert any("kill-window" in c and "%1" in c for c in tmux_calls)
    assert not any("work:claude" in c for c in tmux_calls)
    assert not state_file.exists()


def test_kill_session_tolerates_already_dead_pid(
    fake_tmux_present: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_state_file(tmp_path, "s1")

    def raise_lookup(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr("ephor.tmux.navigator.os.kill", raise_lookup)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", ""),
    )

    outcome = kill_session(_agent(agent_pid=4242), tmp_path)
    assert outcome.ok


def test_kill_session_treats_missing_tmux_window_as_success(
    fake_tmux_present: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_state_file(tmp_path, "s1")
    monkeypatch.setattr("ephor.tmux.navigator.os.kill", lambda *a: None)

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "can't find window: work:claude")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = kill_session(_agent(agent_pid=4242), tmp_path)
    assert outcome.ok


def test_kill_session_no_pid_no_tmux_just_unlinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sessions without a known pid or tmux info still get the state file removed."""
    state_file = _write_state_file(tmp_path, "s1")
    monkeypatch.setattr(navigator, "has_tmux", lambda: False)
    agent = _agent(agent_pid=None, tmux_session=None, tmux_window=None, tmux_pane=None)
    outcome = kill_session(agent, tmp_path)
    assert outcome.ok
    assert not state_file.exists()


# ---- detect_focused_external_pane ----------------------------------------


def _stub_tmux_responses(
    monkeypatch: pytest.MonkeyPatch, list_clients: str, list_panes: str
) -> None:
    """Monkeypatch subprocess.run so the two tmux queries inside
    detect_focused_external_pane return the given canned outputs."""

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        # args is ["tmux", "list-clients", "-F", ...] or list-panes variant.
        if "list-clients" in args:
            return subprocess.CompletedProcess(args, 0, list_clients, "")
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, list_panes, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_detect_focused_picks_most_recently_active_external_client(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two clients on different sessions; ours has older activity. The
    helper must return the OTHER client's active pane."""
    _stub_tmux_responses(
        monkeypatch,
        list_clients=(
            # tty | focused | activity | session
            "/dev/pts/0||1700000000|ephor-session\n"  # us, older
            "/dev/pts/1||1700000500|work-session\n"  # external, newer
        ),
        list_panes=(
            # session | pane_id | window_active | pane_active
            "ephor-session|%1|1|1\nwork-session|%5|1|1\nwork-session|%6|1|0\n"
        ),
    )
    assert navigator.detect_focused_external_pane(self_pane="%1") == "%5"


def test_detect_focused_prefers_focused_over_recent_activity(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When focus-events are on, an explicitly focused client wins over a
    more-recently-typed-in one."""
    _stub_tmux_responses(
        monkeypatch,
        list_clients=(
            "/dev/pts/0||1700000000|ephor-session\n"
            "/dev/pts/1|1|1700000100|work-session\n"  # focused but quieter
            "/dev/pts/2||1700000900|other-session\n"  # most recent activity
        ),
        list_panes=("ephor-session|%1|1|1\nwork-session|%5|1|1\nother-session|%9|1|1\n"),
    )
    assert navigator.detect_focused_external_pane(self_pane="%1") == "%5"


def test_detect_focused_returns_none_when_only_us(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the only attached client is the ephor TUI itself, no external
    target exists."""
    _stub_tmux_responses(
        monkeypatch,
        list_clients="/dev/pts/0||1700000000|ephor-session\n",
        list_panes="ephor-session|%1|1|1\n",
    )
    assert navigator.detect_focused_external_pane(self_pane="%1") is None


def test_detect_focused_returns_none_when_tmux_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(navigator, "has_tmux", lambda: False)
    assert navigator.detect_focused_external_pane(self_pane="%1") is None


def test_detect_focused_returns_none_when_list_clients_fails(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "tmux: no server")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert navigator.detect_focused_external_pane(self_pane="%1") is None

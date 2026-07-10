"""Tests for the tmux pane discoverer.

Heavy use of monkeypatch since we don't want unit tests depending on a
running tmux server, real /proc, or any actual claude processes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ephor.tmux import discover


def _fake_run_factory(panes_stdout: str = "", pgrep_stdout: str = ""):
    """Return a fake subprocess.run that answers the two commands the
    discoverer issues, ignoring any other call."""

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else ""
        if cmd == "tmux":
            return subprocess.CompletedProcess(args, 0, panes_stdout, "")
        if cmd == "pgrep":
            return subprocess.CompletedProcess(
                args, 0 if pgrep_stdout.strip() else 1, pgrep_stdout, ""
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    return fake_run


def test_discover_returns_empty_when_tmux_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discover, "has_tmux", lambda: False)
    assert discover.discover_panes() == []


def test_discover_returns_empty_when_no_panes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discover, "has_tmux", lambda: True)
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(panes_stdout=""))
    assert discover.discover_panes() == []


def test_discover_returns_empty_when_no_claudes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discover, "has_tmux", lambda: True)
    panes = "1234\twork\tclaude\t%1\n"
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(panes, pgrep_stdout=""))
    assert discover.discover_panes() == []


def test_discover_finds_claude_in_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discover, "has_tmux", lambda: True)
    panes = "1234\twork\tclaude\t%1\n"
    pgrep = "5678\n"
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(panes, pgrep))

    # Fake the proc walk: claude pid 5678 → ppid 1234 (the pane root)
    monkeypatch.setattr(discover, "_read_proc_cwd", lambda pid: "/tmp/projX")
    monkeypatch.setattr(
        discover,
        "_read_proc_ppid",
        lambda pid: 1234 if pid == 5678 else None,
    )

    found = discover.discover_panes()
    assert len(found) == 1
    info = found[0]
    assert info.tmux_session == "work"
    assert info.tmux_window == "claude"
    assert info.tmux_pane == "%1"
    assert info.agent_pid == 5678
    assert info.cwd == "/tmp/projX"


def test_walk_terminates_on_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """If we never find a tmux ancestor, the walk must stop at pid 1."""
    monkeypatch.setattr(discover, "has_tmux", lambda: True)
    panes = "9999\twork\tclaude\t%1\n"  # pane root that nothing matches
    pgrep = "5678\n"
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(panes, pgrep))
    monkeypatch.setattr(discover, "_read_proc_cwd", lambda pid: "/tmp/x")
    # Each ppid lookup decrements toward 1.
    monkeypatch.setattr(discover, "_read_proc_ppid", lambda pid: pid - 1 if pid > 1 else None)

    assert discover.discover_panes() == []


def test_enrich_state_files_writes_tmux_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sd = tmp_path / "sessions"
    sd.mkdir()
    sid = "abc-123"
    initial = {
        "schema_version": 1,
        "session_id": sid,
        "cwd": "/tmp/projX",
        "started_at": "2026-04-29T10:00:00Z",
        "status": "WORKING",
        "project_name": "projX",
        "last_event": "PreToolUse",
        "last_event_time": "2026-04-29T10:00:01Z",
        "last_event_seq": 1,
        "tool_count": 1,
        "error_count": 0,
        "tmux_session": None,
        "tmux_window": None,
        "tmux_pane": None,
        "notification": None,
    }
    (sd / f"{sid}.json").write_text(json.dumps(initial))

    monkeypatch.setattr(
        discover,
        "discover_panes",
        lambda: [
            discover.TmuxPaneInfo(
                tmux_session="work",
                tmux_window="claude",
                tmux_pane="%5",
                agent_pid=5678,
                cwd="/tmp/projX",
            )
        ],
    )

    updated = discover.enrich_state_files(sd)
    assert updated == 1

    after = json.loads((sd / f"{sid}.json").read_text())
    assert after["tmux_session"] == "work"
    assert after["tmux_window"] == "claude"
    assert after["tmux_pane"] == "%5"


def test_enrich_skips_when_already_populated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sd = tmp_path / "sessions"
    sd.mkdir()
    sid = "abc-123"
    initial = {
        "schema_version": 1,
        "session_id": sid,
        "cwd": "/tmp/projX",
        "started_at": "2026-04-29T10:00:00Z",
        "status": "WORKING",
        "project_name": "projX",
        "last_event": "",
        "last_event_time": "",
        "last_event_seq": 0,
        "tool_count": 0,
        "error_count": 0,
        "tmux_session": "fresher-session",
        "tmux_window": "fresher-window",
        "tmux_pane": "%99",
        "notification": None,
    }
    (sd / f"{sid}.json").write_text(json.dumps(initial))

    monkeypatch.setattr(
        discover,
        "discover_panes",
        lambda: [
            discover.TmuxPaneInfo(
                tmux_session="stale",
                tmux_window="stale",
                tmux_pane="%1",
                agent_pid=1,
                cwd="/tmp/projX",
            )
        ],
    )

    updated = discover.enrich_state_files(sd)
    assert updated == 0  # no clobber

    after = json.loads((sd / f"{sid}.json").read_text())
    assert after["tmux_session"] == "fresher-session"  # unchanged


def test_enrich_no_match_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setattr(discover, "discover_panes", lambda: [])
    assert discover.enrich_state_files(sd) == 0


# ---------------------------------------------------------------------------
# pid-priority matching (handles same-cwd ambiguity)
# ---------------------------------------------------------------------------


def _make_state(directory: Path, sid: str, **overrides: object) -> Path:
    base = {
        "schema_version": 1,
        "session_id": sid,
        "cwd": "/tmp/sharedcwd",
        "started_at": "2026-04-29T10:00:00Z",
        "status": "WORKING",
        "project_name": "shared",
        "last_event": "PreToolUse",
        "last_event_time": "2026-04-29T10:00:01Z",
        "last_event_seq": 1,
        "tool_count": 1,
        "error_count": 0,
        "tmux_session": None,
        "tmux_window": None,
        "tmux_pane": None,
        "agent_pid": None,
        "notification": None,
    }
    base.update(overrides)
    p = directory / f"{sid}.json"
    p.write_text(json.dumps(base))
    return p


def test_enrich_prefers_pid_match_over_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two state files share a cwd but each records a distinct agent_pid.
    The discoverer's two distinct pids should map to the right state files."""
    sd = tmp_path / "sessions"
    sd.mkdir()
    _make_state(sd, "sid-A", agent_pid=100)
    _make_state(sd, "sid-B", agent_pid=200)

    monkeypatch.setattr(
        discover,
        "discover_panes",
        lambda: [
            discover.TmuxPaneInfo(
                tmux_session="work",
                tmux_window="A",
                tmux_pane="%1",
                agent_pid=100,
                cwd="/tmp/sharedcwd",
            ),
            discover.TmuxPaneInfo(
                tmux_session="work",
                tmux_window="B",
                tmux_pane="%2",
                agent_pid=200,
                cwd="/tmp/sharedcwd",
            ),
        ],
    )

    updated = discover.enrich_state_files(sd)
    assert updated == 2

    a = json.loads((sd / "sid-A.json").read_text())
    b = json.loads((sd / "sid-B.json").read_text())
    assert a["tmux_window"] == "A"
    assert a["tmux_pane"] == "%1"
    assert b["tmux_window"] == "B"
    assert b["tmux_pane"] == "%2"


def test_enrich_skips_ambiguous_cwd_when_pid_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two state files share a cwd, neither records agent_pid, and two
    discovered panes share that cwd. The matcher must NOT guess — it
    should skip both and wait for the hook handler to record pids."""
    sd = tmp_path / "sessions"
    sd.mkdir()
    _make_state(sd, "sid-A")  # agent_pid intentionally null
    _make_state(sd, "sid-B")

    monkeypatch.setattr(
        discover,
        "discover_panes",
        lambda: [
            discover.TmuxPaneInfo(
                tmux_session="work",
                tmux_window="A",
                tmux_pane="%1",
                agent_pid=100,
                cwd="/tmp/sharedcwd",
            ),
            discover.TmuxPaneInfo(
                tmux_session="work",
                tmux_window="B",
                tmux_pane="%2",
                agent_pid=200,
                cwd="/tmp/sharedcwd",
            ),
        ],
    )

    updated = discover.enrich_state_files(sd)
    assert updated == 0  # ambiguous → skip both

    a = json.loads((sd / "sid-A.json").read_text())
    assert a["tmux_window"] is None
    assert a["tmux_pane"] is None


def test_enrich_records_claude_pid_when_matched_by_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cwd-matched state files should also get agent_pid backfilled so the
    next enrich call can use the unambiguous pid path."""
    sd = tmp_path / "sessions"
    sd.mkdir()
    _make_state(sd, "sid-X", cwd="/tmp/uniqueX")

    monkeypatch.setattr(
        discover,
        "discover_panes",
        lambda: [
            discover.TmuxPaneInfo(
                tmux_session="work",
                tmux_window="X",
                tmux_pane="%9",
                agent_pid=42,
                cwd="/tmp/uniqueX",
            ),
        ],
    )

    discover.enrich_state_files(sd)
    after = json.loads((sd / "sid-X.json").read_text())
    assert after["agent_pid"] == 42


# ---------------------------------------------------------------------------
# session_id-from-argv extraction
# ---------------------------------------------------------------------------


def test_session_id_extracted_from_resume_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sd = tmp_path / "sessions"
    sd.mkdir()
    sid = "abc12345-1234-1234-1234-1234567890ab"
    _make_state(sd, sid)  # default cwd /tmp/sharedcwd, agent_pid null

    monkeypatch.setattr(
        discover,
        "discover_panes",
        lambda: [
            discover.TmuxPaneInfo(
                tmux_session="work",
                tmux_window="A",
                tmux_pane="%9",
                agent_pid=999,
                cwd="/tmp/sharedcwd",
                session_id=sid,
            ),
        ],
    )
    discover.enrich_state_files(sd)
    after = json.loads((sd / f"{sid}.json").read_text())
    assert after["tmux_pane"] == "%9"
    assert after["agent_pid"] == 999  # backfilled from definitive match


def test_session_id_priority_disambiguates_same_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sd = tmp_path / "sessions"
    sd.mkdir()
    sid_a = "11111111-1111-1111-1111-111111111111"
    sid_b = "22222222-2222-2222-2222-222222222222"
    _make_state(sd, sid_a)
    _make_state(sd, sid_b)

    monkeypatch.setattr(
        discover,
        "discover_panes",
        lambda: [
            discover.TmuxPaneInfo(
                tmux_session="work",
                tmux_window="A",
                tmux_pane="%1",
                agent_pid=100,
                cwd="/tmp/sharedcwd",
                session_id=sid_a,
            ),
            discover.TmuxPaneInfo(
                tmux_session="work",
                tmux_window="B",
                tmux_pane="%2",
                agent_pid=200,
                cwd="/tmp/sharedcwd",
                session_id=sid_b,
            ),
        ],
    )
    updated = discover.enrich_state_files(sd)
    assert updated == 2
    a = json.loads((sd / f"{sid_a}.json").read_text())
    b = json.loads((sd / f"{sid_b}.json").read_text())
    assert a["tmux_pane"] == "%1"
    assert b["tmux_pane"] == "%2"


def test_definitive_match_overwrites_wrong_existing_tmux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a previous cwd-only match wrote wrong tmux info, a definitive
    sid match should overwrite it."""
    sd = tmp_path / "sessions"
    sd.mkdir()
    sid = "33333333-3333-3333-3333-333333333333"
    _make_state(
        sd,
        sid,
        tmux_session="stale-session",
        tmux_window="stale-window",
        tmux_pane="%99",
    )

    monkeypatch.setattr(
        discover,
        "discover_panes",
        lambda: [
            discover.TmuxPaneInfo(
                tmux_session="fresh",
                tmux_window="fresh-w",
                tmux_pane="%5",
                agent_pid=1,
                cwd="/tmp/sharedcwd",
                session_id=sid,
            ),
        ],
    )
    discover.enrich_state_files(sd)
    after = json.loads((sd / f"{sid}.json").read_text())
    assert after["tmux_session"] == "fresh"
    assert after["tmux_window"] == "fresh-w"
    assert after["tmux_pane"] == "%5"


def test_corrupt_tab_concat_treated_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-fix state files have `tmux_session` containing literal '\t' from
    the bad display-message format. Even with only cwd-match available, we
    should treat that as corrupt and overwrite."""
    sd = tmp_path / "sessions"
    sd.mkdir()
    sid = "44444444-4444-4444-4444-444444444444"
    _make_state(
        sd,
        sid,
        cwd="/tmp/uniqueX",
        tmux_session=r"work	claude	%9",  # the exact corruption
        tmux_window="",
        tmux_pane="",
    )

    monkeypatch.setattr(
        discover,
        "discover_panes",
        lambda: [
            discover.TmuxPaneInfo(
                tmux_session="work",
                tmux_window="claude",
                tmux_pane="%9",
                agent_pid=42,
                cwd="/tmp/uniqueX",  # session_id intentionally None
            ),
        ],
    )
    discover.enrich_state_files(sd)
    after = json.loads((sd / f"{sid}.json").read_text())
    assert after["tmux_session"] == "work"
    assert after["tmux_window"] == "claude"
    assert after["tmux_pane"] == "%9"


def test_read_session_id_from_cmdline_round_trip(tmp_path: Path) -> None:
    """Synthesize a fake /proc/<pid>/cmdline and confirm extraction."""
    proc = tmp_path / "proc"
    proc.mkdir()
    pid_dir = proc / "1234"
    pid_dir.mkdir()
    sid = "deadbeef-dead-beef-dead-beefdeadbeef"
    cmdline = b"\0".join([b"claude", b"--dangerously-skip-permissions", b"--resume", sid.encode()])
    (pid_dir / "cmdline").write_bytes(cmdline)

    # Patch the module to read from our fake /proc.
    real_open = open

    def fake_open(path: str | Path, *a: Any, **kw: Any) -> Any:
        s = str(path)
        if s.startswith("/proc/1234/"):
            return real_open(proc / "1234" / Path(s).name, *a, **kw)
        return real_open(path, *a, **kw)

    import builtins

    builtins.open = fake_open  # type: ignore[assignment]
    try:
        got = discover._read_session_id_from_cmdline(1234, ("--resume", "-r"))
    finally:
        builtins.open = real_open  # type: ignore[assignment]
    assert got == sid


def test_read_session_id_rejects_garbage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-UUID-shaped argument after --resume must be rejected."""
    proc = tmp_path / "proc"
    proc.mkdir()
    pid_dir = proc / "9999"
    pid_dir.mkdir()
    cmdline = b"\0".join([b"claude", b"--resume", b"; rm -rf /"])
    (pid_dir / "cmdline").write_bytes(cmdline)

    real_open = open

    def fake_open(path: str | Path, *a: Any, **kw: Any) -> Any:
        s = str(path)
        if s.startswith("/proc/9999/"):
            return real_open(proc / "9999" / Path(s).name, *a, **kw)
        return real_open(path, *a, **kw)

    import builtins

    builtins.open = fake_open  # type: ignore[assignment]
    try:
        got = discover._read_session_id_from_cmdline(9999, ("--resume", "-r"))
    finally:
        builtins.open = real_open  # type: ignore[assignment]
    assert got is None


def test_discover_uses_pane_claude_sid_option(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the per-pane @ephor_sid user option is set (by the SessionStart
    hook), discover_panes uses it instead of parsing the claude --resume
    argv. This is what disambiguates same-cwd panes."""
    monkeypatch.setattr(discover, "has_tmux", lambda: True)
    # 5-tab-separated: pid, session, window, pane_id, @ephor_sid
    panes = "1234\twork\tclaude\t%1\tabc-pane-sid\n"
    pgrep = "5678\n"
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(panes, pgrep))
    monkeypatch.setattr(discover, "_read_proc_cwd", lambda pid: "/tmp/projX")
    monkeypatch.setattr(discover, "_read_proc_ppid", lambda pid: 1234 if pid == 5678 else None)

    # If discover read cmdline, it'd return this — but pane option must win.
    monkeypatch.setattr(
        discover, "_read_session_id_from_cmdline", lambda pid, flags: "wrong-cmdline-sid"
    )

    found = discover.discover_panes()
    assert len(found) == 1
    assert found[0].session_id == "abc-pane-sid"


def test_discover_falls_back_to_cmdline_when_pane_option_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When @ephor_sid isn't set on the pane (older claude that hasn't
    fired SessionStart yet, or pre-fix state), fall back to argv parsing."""
    monkeypatch.setattr(discover, "has_tmux", lambda: True)
    # Trailing tab-then-empty mimics tmux 5-field format with empty option.
    panes = "1234\twork\tclaude\t%1\t\n"
    pgrep = "5678\n"
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(panes, pgrep))
    monkeypatch.setattr(discover, "_read_proc_cwd", lambda pid: "/tmp/projX")
    monkeypatch.setattr(discover, "_read_proc_ppid", lambda pid: 1234 if pid == 5678 else None)
    monkeypatch.setattr(
        discover, "_read_session_id_from_cmdline", lambda pid, flags: "fallback-sid"
    )

    found = discover.discover_panes()
    assert len(found) == 1
    assert found[0].session_id == "fallback-sid"


def test_list_tmux_panes_handles_legacy_4_field_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older tmux versions trim trailing empty fields (no `@ephor_sid`).
    Must still parse without crashing."""
    monkeypatch.setattr(discover, "has_tmux", lambda: True)
    panes = "1234\twork\tclaude\t%1\n"
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(panes, ""))
    parsed = discover._list_tmux_panes()
    assert parsed == [(1234, "work", "claude", "%1", "")]

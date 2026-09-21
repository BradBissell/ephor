"""CLI: start / resume / log / work."""

from __future__ import annotations

from pathlib import Path

import pytest

from ephor import eventlog, launcher, work_items
from ephor.cli import _parse_duration, main


def test_start_dry_run_touches_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["start", "DR-8222", "--title", "Add retry", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would create" in out
    assert work_items.load("DR-8222") is None


def test_start_rejects_a_non_ticket(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["start", "nonsense", "--dry-run"]) == 1
    assert "not a ticket key" in capsys.readouterr().err


def test_start_reports_the_branch_and_worktree(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        launcher,
        "start",
        lambda *a, **k: launcher.StartResult(
            True, "created", worktree=str(tmp_path / "w"), branch="DR-1-x", window="@1"
        ),
    )
    assert main(["start", "DR-1"]) == 0
    out = capsys.readouterr().out
    assert "DR-1-x" in out
    assert str(tmp_path / "w") in out


def test_resume_needs_an_unambiguous_session(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["resume", "nosuch"]) == 1
    assert "no session matching" in capsys.readouterr().err


def test_work_reports_nothing_when_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["work"]) == 0
    assert "no work items" in capsys.readouterr().err


def test_work_lists_recorded_items(capsys: pytest.CaptureFixture[str]) -> None:
    work_items.record("DR-1", session_id="s1", branch="DR-1-x", pr_number=7, pr_state="OPEN")
    work_items.record("DR-2", jira_status="Done")
    assert main(["work"]) == 0
    out = capsys.readouterr().out
    assert "DR-1" in out
    assert "#7 open" in out
    assert "Done" in out


def test_work_prune(capsys: pytest.CaptureFixture[str]) -> None:
    work_items.record("DR-1")
    assert main(["work", "--prune"]) == 0
    assert "pruned 0" in capsys.readouterr().out


def test_log_reports_nothing_when_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["log"]) == 0
    assert "no matching events" in capsys.readouterr().err


def test_log_renders_events_and_filters_by_ticket(capsys: pytest.CaptureFixture[str]) -> None:
    eventlog.append(eventlog.EventKind.SESSION_STARTED, session_id="s1", ticket="DR-1", detail="a")
    eventlog.append(eventlog.EventKind.PR_LINKED, session_id="s2", ticket="DR-2", detail="b")

    assert main(["log", "DR-1"]) == 0
    out = capsys.readouterr().out
    assert "session_started" in out
    assert "DR-2" not in out


def test_log_rejects_an_unreadable_duration(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["log", "--since", "yesterday"]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_parse_duration() -> None:
    assert _parse_duration("30s") == 30
    assert _parse_duration("45m") == 2700
    assert _parse_duration("2h") == 7200
    assert _parse_duration("7d") == 604800
    assert _parse_duration("1w") == 604800
    assert _parse_duration("nope") is None
    assert _parse_duration(None) is None

"""`ephor flows board`: the status lanes from the command line."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ephor import board, jira_api, ticket_pins, work_items
from ephor.cli import main
from ephor.config import SCHEMA_VERSION, config_dir, state_dir
from ephor.flows import snapshot as snap
from ephor.flows.snapshot import Snapshot, TicketState
from ephor.jira_api import Issue

_BOARD = """
[board]
projects = ["DR"]
columns = ["To Do", "In Progress", { name = "Review", statuses = ["Peer Review"] }, "Done"]
"""


def _write_board(text: str = _BOARD) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / board.BOARD_FILE).write_text(text)


def _write_snapshot(tickets: dict[str, TicketState]) -> None:
    assert snap.save(Snapshot(taken_at=datetime.now(UTC).isoformat(), tickets=tickets))


def _jira_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPHOR_JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("EPHOR_JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("EPHOR_JIRA_TOKEN", "t")


def test_flows_without_a_subcommand_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["flows"])
    assert "board" in capsys.readouterr().out


def test_board_without_a_board_toml_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["flows", "board"]) == 1
    assert "no board at" in capsys.readouterr().err


def test_board_without_a_snapshot_says_how_to_get_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_board()
    assert main(["flows", "board"]) == 1
    assert "flows board --refresh" in capsys.readouterr().err


def test_board_prints_lanes_in_column_order(capsys: pytest.CaptureFixture[str]) -> None:
    _write_board()
    _write_snapshot(
        {
            "DR-10": TicketState(status="To Do", title="Tenth"),
            "DR-9": TicketState(status="To Do", title="Ninth\x1b[2J"),
            "DR-2": TicketState(status="Peer Review", title="Second"),
            "DR-5": TicketState(status="Blocked", title="Fifth"),
            "DR-6": TicketState(status="To Do", title="Gone", unseen_ticks=1),
        }
    )
    assert main(["flows", "board"]) == 0
    out = capsys.readouterr().out
    lanes = [line for line in out.splitlines() if line and not line.startswith((" ", "board:"))]
    assert lanes[:5] == ["To Do (2)", "In Progress (0)", "Review (1)", "Done (0)", "Other (1)"]
    assert out.index("DR-9") < out.index("DR-10")
    assert "\x1b" not in out
    assert "DR-6" not in out
    assert "1 ticket(s) missing from the last search" in out


def test_board_counts_live_sessions_per_ticket(capsys: pytest.CaptureFixture[str]) -> None:
    _write_board()
    _write_snapshot({"DR-1": TicketState(status="In Progress", title="First")})
    sessions = state_dir()
    sessions.mkdir(parents=True, exist_ok=True)
    for sid in ("s1", "s2"):
        (sessions / f"{sid}.json").write_text(
            json.dumps(
                {
                    "session_id": sid,
                    "cwd": str(Path("/tmp") / sid),
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "status": "IDLE",
                    "ticket": "DR-1" if sid == "s1" else "",
                    "schema_version": SCHEMA_VERSION,
                }
            )
        )
    work_items.record("DR-1", session_id="s2")
    work_items.record("DR-1", session_id="gone")  # not a live session
    assert main(["flows", "board"]) == 0
    assert "2 session(s)" in capsys.readouterr().out


def _write_live_session(sid: str, **fields: object) -> None:
    sessions = state_dir()
    sessions.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "session_id": sid,
        "cwd": str(Path("/nonexistent-ephor-test") / sid),
        "started_at": "2026-01-01T00:00:00+00:00",
        "status": "IDLE",
        "schema_version": SCHEMA_VERSION,
        **fields,
    }
    (sessions / f"{sid}.json").write_text(json.dumps(payload))


def test_board_counts_a_pinned_session_like_the_dashboard_does(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ticket comes from the dashboard's resolver, not the raw state field."""
    _write_board()
    _write_snapshot({"DR-1": TicketState(status="In Progress", title="First")})
    _write_live_session("s1")  # no `ticket` field at all
    ticket_pins.pin("s1", "DR-1")
    assert main(["flows", "board"]) == 0
    assert "1 session(s)" in capsys.readouterr().out


def test_board_survives_a_null_ticket_in_a_state_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_board()
    _write_snapshot({"DR-1": TicketState(status="In Progress", title="First")})
    _write_live_session("s1", ticket=None)
    work_items.record("DR-1", session_id="s1")
    assert main(["flows", "board"]) == 0
    assert "1 session(s)" in capsys.readouterr().out


def test_board_reports_config_errors_and_falls_back(capsys: pytest.CaptureFixture[str]) -> None:
    _write_board('[board]\nprojects = ["DR"]\ncolumns = []\n')
    _write_snapshot({"DR-1": TicketState(status="In Progress")})
    assert main(["flows", "board"]) == 1
    captured = capsys.readouterr()
    assert "columns must be a non-empty list" in captured.err
    assert "In Progress (1)" in captured.out


def test_refresh_without_jira_explains_and_shows_the_old_snapshot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_board()
    _write_snapshot({"DR-1": TicketState(status="In Progress")})
    assert main(["flows", "board", "--refresh"]) == 1
    captured = capsys.readouterr()
    assert "Jira is not configured" in captured.err
    assert "DR-1" in captured.out


def test_refresh_searches_and_persists(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_board()
    _jira_on(monkeypatch)
    monkeypatch.setattr(
        jira_api,
        "search",
        lambda *a, **k: [Issue(key="DR-4", status="Peer Review", status_id="5", title="Four")],
    )
    assert main(["flows", "board", "--refresh"]) == 0
    assert "DR-4" in capsys.readouterr().out
    stored = snap.load()
    assert stored is not None and "DR-4" in stored.tickets


def test_a_failed_refresh_keeps_the_snapshot(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_board()
    _write_snapshot({"DR-1": TicketState(status="In Progress")})
    _jira_on(monkeypatch)
    monkeypatch.setattr(jira_api, "search", lambda *a, **k: None)
    assert main(["flows", "board", "--refresh"]) == 1
    captured = capsys.readouterr()
    assert "search failed" in captured.err
    assert "DR-1" in captured.out

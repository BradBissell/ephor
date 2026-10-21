"""``ephor flows tick | ls | approve | deny | pause | resume | check``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ephor import board, jira_api, launcher
from ephor.cli import main
from ephor.config import config_dir
from ephor.flows import runners, runs
from ephor.flows import snapshot as snap
from ephor.flows.runs import Run, RunState
from ephor.flows.snapshot import Snapshot, TicketState
from ephor.jira_api import Issue

_BOARD = """
[board]
projects = ["DR"]
columns = ["To Do", "In Progress", "Peer Review", "Done"]

[[flow]]
name = "notify"
on_enter = "Peer Review"
command = "notify.sh {ticket}"
mode = "auto"
assignee = "any"
"""


def _write_board(text: str = _BOARD) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / board.BOARD_FILE).write_text(text)


@pytest.fixture
def jira(monkeypatch: pytest.MonkeyPatch) -> dict[str, Issue]:
    monkeypatch.setenv("EPHOR_JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("EPHOR_JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("EPHOR_JIRA_TOKEN", "t")
    issues: dict[str, Issue] = {}
    monkeypatch.setattr(jira_api, "search", lambda jql, fields=(): list(issues.values()))
    monkeypatch.setattr(jira_api, "fetch_fresh", lambda key: issues.get(key))
    monkeypatch.setattr(jira_api, "myself", lambda: "acct-me")
    monkeypatch.setattr(runners, "live_panes", lambda: None)
    monkeypatch.setattr(runners, "flow_panes", dict)
    return issues


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    opened: list[dict[str, Any]] = []

    def open_window(
        name: str,
        cwd: Path,
        command: str,
        env: dict[str, str],
        *,
        detached: bool = False,
        target_session: str = "",
    ) -> tuple[str, str, str]:
        opened.append({"name": name, "detached": detached, "target_session": target_session})
        return "@1", "%1", "opened"

    monkeypatch.setattr(launcher, "open_window", open_window)
    return opened


def _move(jira: dict[str, Issue]) -> None:
    jira["DR-1"] = Issue(key="DR-1", status="In Progress", status_id="3", title="One")
    assert main(["flows", "tick"]) == 0
    jira["DR-1"] = Issue(key="DR-1", status="Peer Review", status_id="10", title="One")


def test_tick_runs_rules_and_launches(
    jira: dict[str, Issue], windows: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    _write_board()
    _move(jira)
    assert "adopted   notify" in capsys.readouterr().out
    assert main(["flows", "tick"]) == 0
    out = capsys.readouterr().out
    assert "moved     DR-1" in out
    assert "created    DR-1.notify.10-1" in out
    assert "dispatched DR-1.notify.10-1" in out
    assert [w["name"] for w in windows] == ["flow:DR-1:notify"]
    assert windows[0]["detached"] is True


def test_tick_dry_run_launches_nothing(
    jira: dict[str, Issue], windows: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    _write_board()
    _move(jira)
    capsys.readouterr()
    assert main(["flows", "tick", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dispatch   DR-1.notify.10-1" in out
    assert "nothing was written" in out
    assert windows == []
    assert runs.list_runs() == []


def test_tick_reports_a_failed_search(
    jira: dict[str, Issue], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_board()
    monkeypatch.setattr(jira_api, "search", lambda *a, **k: None)
    assert main(["flows", "tick"]) == 1
    assert "search failed" in capsys.readouterr().err


def test_tick_without_a_board(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["flows", "tick"]) == 1
    assert "no board at" in capsys.readouterr().err


def _awaiting(
    run_id: str = "DR-1.review.10-1", state: RunState = RunState.AWAITING_CONFIRM
) -> None:
    ticket, flow, generation = run_id.split(".")
    stamp = runs.iso(datetime.now(UTC))
    assert runs.create(
        Run(
            run_id=run_id,
            flow=flow,
            ticket=ticket,
            trigger="on_enter",
            generation=generation,
            state=state,
            created_at=stamp,
            updated_at=stamp,
            to_status="Peer Review",
            reason="waiting for you" if state is RunState.AWAITING_CONFIRM else "",
        )
    )


def test_ls_lists_runs_and_hints_at_approve(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["flows", "ls"]) == 0
    assert "no runs" in capsys.readouterr().out
    _awaiting()
    _awaiting("DR-2.review.10-1", RunState.LAUNCHED)
    assert main(["flows", "ls"]) == 0
    out = capsys.readouterr().out
    assert "awaiting_confirm" in out and "DR-1.review.10-1" in out
    assert "DR-2.review.10-1" in out
    assert "1 awaiting confirmation" in out


def test_ls_hides_old_finished_runs_without_all(capsys: pytest.CaptureFixture[str]) -> None:
    _awaiting("DR-3.review.10-1", RunState.DONE)
    path = runs.run_path("DR-3.review.10-1")
    path.write_text(
        path.read_text().replace(
            runs.load("DR-3.review.10-1").updated_at, "2026-01-01T00:00:00+00:00"
        )
    )  # type: ignore[union-attr]
    main(["flows", "ls"])
    assert "DR-3" not in capsys.readouterr().out
    main(["flows", "ls", "--all"])
    assert "DR-3.review.10-1" in capsys.readouterr().out


def test_approve_and_deny_by_prefix(capsys: pytest.CaptureFixture[str]) -> None:
    _awaiting()
    _awaiting("DR-2.review.10-1")
    assert main(["flows", "approve", "dr-1"]) == 0
    assert "approved DR-1.review.10-1" in capsys.readouterr().out
    assert runs.load("DR-1.review.10-1").state is RunState.CLAIMED  # type: ignore[union-attr]
    assert main(["flows", "approve", "DR-1"]) == 1
    assert "claimed, not awaiting_confirm" in capsys.readouterr().err
    assert main(["flows", "deny", "DR-2.review"]) == 0
    assert runs.load("DR-2.review.10-1").state is RunState.DENIED  # type: ignore[union-attr]
    assert main(["flows", "deny", "DR-9"]) == 1
    assert "no run matches" in capsys.readouterr().err


def test_pause_and_resume(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["flows", "pause"]) == 0
    assert runs.is_paused()
    main(["flows", "ls"])
    assert "PAUSED" in capsys.readouterr().out
    assert main(["flows", "resume"]) == 0
    assert not runs.is_paused()


def test_check_validates_and_probes_jira(
    jira: dict[str, Issue], capsys: pytest.CaptureFixture[str]
) -> None:
    _write_board()
    assert main(["flows", "check"]) == 0
    out = capsys.readouterr().out
    assert "ok: 4 column(s), 1 flow(s)" in out
    assert "flow notify: on_enter='Peer Review'" in out
    assert "accountId acct-me" in out


def test_check_reports_errors_and_a_silent_jira(
    jira: dict[str, Issue], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_board(_BOARD.replace('mode = "auto"', 'mode = "auto"\nstart = "/x {ticket}"'))
    monkeypatch.setattr(jira_api, "myself", lambda: None)
    assert main(["flows", "check"]) == 1
    out = capsys.readouterr().out
    assert "error:" in out
    assert "/myself did not answer" in out


def test_check_without_jira(capsys: pytest.CaptureFixture[str]) -> None:
    _write_board()
    assert main(["flows", "check"]) == 1
    assert "jira: not configured" in capsys.readouterr().out


def test_board_shows_flow_badges(capsys: pytest.CaptureFixture[str]) -> None:
    _write_board()
    assert snap.save(
        Snapshot(
            taken_at=datetime.now(UTC).isoformat(),
            tickets={"DR-1": TicketState(status="Peer Review", status_id="10", title="One")},
        )
    )
    _awaiting()
    assert main(["flows", "board"]) == 0
    assert "[⏸ review?]" in capsys.readouterr().out


def test_board_refresh_runs_the_rules_rather_than_swallowing_the_move(
    jira: dict[str, Issue], windows: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    _write_board()
    _move(jira)
    assert main(["flows", "board", "--refresh"]) == 0
    assert [r.run_id for r in runs.list_runs()] == ["DR-1.notify.10-1"]
    assert "[▶ notify]" in capsys.readouterr().out

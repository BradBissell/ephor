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
    monkeypatch.setattr(jira_api, "search", lambda jql, fields=(), **_: list(issues.values()))
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


# --- watch ----------------------------------------------------------------------------


def test_watch_ticks_until_told_to_stop(
    jira: dict[str, Issue], windows: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    from ephor.flows import cli as flows_cli

    _write_board()
    jira["DR-1"] = Issue(key="DR-1", status="In Progress", status_id="3", title="One")
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        # Between the first and second tick the ticket moves.
        jira["DR-1"] = Issue(key="DR-1", status="Peer Review", status_id="10", title="One")

    assert flows_cli.cmd_watch(interval=30, max_ticks=3, sleep=sleep) == 0
    assert sleeps == [30, 30]
    out = capsys.readouterr().out
    assert "watching the board every 30s" in out
    assert "adopted   notify" in out
    assert "moved     DR-1" in out
    assert "dispatched DR-1.notify.10-1" in out
    assert len(windows) == 1
    # The third tick changed nothing and printed nothing new.
    assert out.count("ticket(s) on the board") == 2


def test_watch_stops_cleanly_on_ctrl_c(
    jira: dict[str, Issue], capsys: pytest.CaptureFixture[str]
) -> None:
    from ephor.flows import cli as flows_cli

    _write_board()

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    assert flows_cli.cmd_watch(interval=60, sleep=interrupt) == 0
    assert capsys.readouterr().out.rstrip().endswith("stopped")


def test_watch_rejects_a_tiny_interval_and_a_missing_board(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["flows", "watch", "--interval", "1"]) == 2
    assert "at least 10 seconds" in capsys.readouterr().err
    assert main(["flows", "watch"]) == 1
    assert "no board at" in capsys.readouterr().err


@pytest.mark.parametrize("interval", [float("nan"), float("inf")])
def test_watch_rejects_a_non_finite_interval_without_ticking(
    interval: float,
    jira: dict[str, Issue],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ephor.flows import cli as flows_cli

    _write_board()  # a board that would tick, so only the interval stops it
    ticks: list[str] = []
    monkeypatch.setattr(flows_cli, "_watch_once", lambda last: ticks.append(last) or last)
    assert flows_cli.cmd_watch(interval=interval, max_ticks=1, sleep=lambda s: None) == 2
    assert "finite number of at least 10 seconds" in capsys.readouterr().err
    assert ticks == []


def test_watch_waits_out_a_board_that_breaks_and_a_held_lock(
    jira: dict[str, Issue], capsys: pytest.CaptureFixture[str]
) -> None:
    from ephor.filelock import locked
    from ephor.flows import cli as flows_cli

    _write_board()
    calls = 0

    def sleep(seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            _write_board("[board]\nprojects = 3\n")
        elif calls == 2:
            _write_board()

    with locked(snap.tick_lock_path(), timeout=0) as held:
        assert held
        assert flows_cli.cmd_watch(interval=10, max_ticks=1, sleep=sleep) == 0
    assert "on the board" not in capsys.readouterr().out
    assert flows_cli.cmd_watch(interval=10, max_ticks=3, sleep=sleep) == 0
    out = capsys.readouterr().out
    assert "board.toml [board] has 2 error(s)" in out
    assert out.count("ticket(s) on the board") == 2


def test_tick_prints_pr_events(
    jira: dict[str, Issue],
    windows: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from ephor import github
    from ephor.github import PullRequest

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_board(
        f"""
[board]
projects = ["DR"]
[repos]
DR = "{repo}"
[[flow]]
name = "ping"
on_pr = "opened"
command = "ping.sh {{ticket}}"
mode = "auto"
assignee = "any"
"""
    )
    found: list[PullRequest] = []
    monkeypatch.setattr(github, "find_prs_for_ticket", lambda path, key: list(found))
    jira["DR-1"] = Issue(key="DR-1", status="In Progress", status_id="3", title="One")
    assert main(["flows", "tick"]) == 0
    found.append(PullRequest(number=7, url="https://x/pull/7", state="OPEN"))
    stored = snap.load()
    assert stored is not None
    snap.save(Snapshot(**{**stored.__dict__, "pr_checked_at": ""}))
    assert main(["flows", "tick"]) == 0
    out = capsys.readouterr().out
    assert "pr        DR-1       PR #7 opened" in out
    assert "dispatched DR-1.ping.pr7" in out


def test_ls_says_stale_once(capsys: pytest.CaptureFixture[str]) -> None:
    _awaiting()
    path = runs.run_path("DR-1.review.10-1")
    run = runs.load("DR-1.review.10-1")
    assert run is not None
    from dataclasses import replace

    path.write_text(
        replace(run, stale=True, reason="stale: created after a gap in ticks").to_json()
    )
    main(["flows", "ls"])
    line = next(x for x in capsys.readouterr().out.splitlines() if "DR-1.review" in x)
    assert line.count("stale") == 1


def test_resume_says_how_many_held_runs_will_go(capsys: pytest.CaptureFixture[str]) -> None:
    _awaiting("DR-1.review.10-1", RunState.BLOCKED)
    _awaiting("DR-2.review.10-1", RunState.CLAIMED)
    _awaiting("DR-3.review.10-1")
    main(["flows", "pause"])
    capsys.readouterr()
    assert main(["flows", "resume"]) == 0
    assert "2 held run(s) dispatch on the next tick" in capsys.readouterr().out


def test_check_reports_paused_even_without_jira(capsys: pytest.CaptureFixture[str]) -> None:
    _write_board()
    runs.pause()
    assert main(["flows", "check"]) == 1
    out = capsys.readouterr().out
    assert "flows: PAUSED" in out
    assert out.index("flows: PAUSED") < out.index("jira: not configured")


def test_check_names_the_missing_jira_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_board()
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    assert main(["flows", "check"]) == 1
    out = capsys.readouterr().out
    assert "missing email (EPHOR_JIRA_EMAIL, JIRA_EMAIL or JIRA_USERNAME)" in out
    assert "url" not in out.split("missing", 1)[1] and "token" not in out.split("missing", 1)[1]


def test_jira_username_is_an_email_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net/")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    monkeypatch.setenv("JIRA_USERNAME", "me@example.com")
    creds = jira_api.credentials()
    assert creds is not None and creds.email == "me@example.com"
    assert jira_api.missing_credentials() == ""
    monkeypatch.setenv("JIRA_EMAIL", "other@example.com")
    assert jira_api.credentials().email == "other@example.com"  # type: ignore[union-attr]


def test_check_runs_the_board_search(
    jira: dict[str, Issue], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_board()
    asked: list[tuple[str, dict[str, Any]]] = []

    def search(jql: str, fields: tuple[str, ...] = (), **kw: Any) -> list[Issue] | None:
        asked.append((jql, kw))
        return None

    monkeypatch.setattr(jira_api, "search", search)
    assert main(["flows", "check"]) == 1
    assert "board search failed" in capsys.readouterr().out
    ((jql, kw),) = asked
    assert "project in (DR)" in jql
    assert kw == {"page_size": 1, "max_pages": 1}

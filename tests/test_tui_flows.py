"""Flows in the dashboard: badges, the header count, the `f` screen and its y / n."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from textual.content import Content

from ephor import board, jira_api
from ephor.config import SCHEMA_VERSION, config_dir
from ephor.flows import runs
from ephor.flows import snapshot as snap
from ephor.flows import tick as flows_tick
from ephor.flows.runs import Run, RunState
from ephor.flows.snapshot import Snapshot, TicketState
from ephor.state.manager import StateManager
from ephor.state.models import AgentState, StatusSummary
from ephor.tui import lanes as lanes_module
from ephor.tui.app import EphorApp
from ephor.tui.flows_screen import FlowsScreen, ordered_runs, run_markup
from ephor.tui.lanes import LaneData, Lanes, ticket_row_markup
from ephor.tui.rows import virtual_row_id
from ephor.tui.widgets.header_bar import format_header
from ephor.tui.widgets.session_row import RowContext, render_detail

_BOARD = """
[board]
projects = ["DR"]
columns = ["To Do", "In Progress", "Peer Review", "Done"]
"""


def _plain(markup: str) -> str:
    return Content.from_markup(markup).plain


def _run(
    run_id: str = "DR-3.review.10-1",
    state: RunState = RunState.AWAITING_CONFIRM,
    *,
    age: timedelta = timedelta(minutes=1),
    **extra: object,
) -> Run:
    ticket, flow, generation = run_id.split(".")
    stamp = runs.iso(datetime.now(UTC) - age)
    fields: dict[str, object] = {
        "run_id": run_id,
        "flow": flow,
        "ticket": ticket,
        "trigger": "on_enter",
        "generation": generation,
        "state": state,
        "created_at": stamp,
        "updated_at": stamp,
        "to_status": "Peer Review",
        **extra,
    }
    return Run(**fields)  # type: ignore[arg-type]


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    """One session on DR-1; DR-3 is in Peer Review with no session and a run awaiting."""
    directory = tmp_path / "sessions"
    directory.mkdir()
    (directory / "s1.json").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "cwd": str(directory / "s1"),
                "started_at": "2026-01-01T00:00:00+00:00",
                "status": "IDLE",
                "project_name": "alpha",
                "ticket": "DR-1",
                "last_event_time": "2026-01-01T00:00:01+00:00",
                "schema_version": SCHEMA_VERSION,
            }
        )
    )
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / board.BOARD_FILE).write_text(_BOARD)
    assert snap.save(
        Snapshot(
            taken_at=datetime.now(UTC).isoformat(),
            tickets={
                "DR-1": TicketState(status="In Progress", status_id="3", title="One"),
                "DR-3": TicketState(status="Peer Review", status_id="10", title="Three"),
            },
        )
    )
    assert runs.create(_run())
    assert runs.create(_run("DR-1.notify.3-1", RunState.LAUNCHED, age=timedelta(minutes=2)))
    return directory


# --- pure rendering ----------------------------------------------------------------------


def test_the_header_counts_runs_awaiting_confirmation() -> None:
    summary = StatusSummary()
    assert "FLOW?" not in _plain(format_header(summary))
    assert "FLOW? 2" in _plain(format_header(summary, flows_awaiting=2))


def test_a_ticket_row_shows_its_flow_badge() -> None:
    record = TicketState(status="Peer Review", title="Three")
    plain = _plain(ticket_row_markup("DR-3", record, "⏸ review?"))
    assert "DR-3" in plain and "⏸ review?" in plain and "Three" in plain
    assert "⏸" not in _plain(ticket_row_markup("DR-3", record))


def test_a_session_row_shows_its_flow_badge_beside_the_title() -> None:
    agent = AgentState.from_dict(
        {
            "session_id": "s1",
            "cwd": "/x",
            "started_at": "2026-01-01T00:00:00+00:00",
            "status": "IDLE",
            "schema_version": SCHEMA_VERSION,
        }
    )
    shown = _plain(
        render_detail(agent, RowContext(flow_badge="▶ review"), width=60, title="My work")
    )
    assert shown.startswith("▶ review My work")
    # Without a title the badge says where to look.
    assert "f for flows" in _plain(
        render_detail(agent, RowContext(flow_badge="▶ review"), width=60)
    )
    # A shared worktree still outranks it.
    clash = RowContext(flow_badge="▶ review", collisions=("s2",))
    assert "▶" not in _plain(render_detail(agent, clash, width=60, title="My work"))


def test_lanes_badges_and_count_follow_the_runs() -> None:
    lanes = Lanes(LaneData(runs=(_run(), _run("DR-1.notify.3-1", RunState.LAUNCHED))))
    assert lanes.badge_for("DR-3") == "⏸ review?"
    assert lanes.badge_for("dr-1") == "▶ notify"
    assert lanes.badge_for("") == ""
    assert lanes.awaiting_count == 1
    lanes.update_run(_run(state=RunState.CLAIMED))
    assert lanes.awaiting_count == 0
    assert lanes.badge_for("DR-3") == "… review"


def test_the_screen_lists_awaiting_first_then_active_then_recent() -> None:
    ordered = ordered_runs(
        [
            _run("DR-1.a.1-0", RunState.LAUNCHED),
            _run("DR-2.a.1-0", RunState.DONE),
            _run("DR-3.a.1-0", RunState.AWAITING_CONFIRM, age=timedelta(minutes=5)),
            _run("DR-4.a.1-0", RunState.AWAITING_CONFIRM, age=timedelta(minutes=9)),
            _run("DR-5.a.1-0", RunState.DONE, age=timedelta(days=3)),
        ]
    )
    assert [r.ticket for r in ordered] == ["DR-4", "DR-3", "DR-1", "DR-2"]


def test_run_markup_escapes_reasons() -> None:
    line = run_markup(_run(reason="ticket is now [bold]QA[/]"))
    assert "ticket is now [bold]QA[/]" in _plain(line)


# --- the dashboard ------------------------------------------------------------------------


async def _status(app: EphorApp) -> None:
    while app._group_by != "status":
        app.action_cycle_grouping()
    await app._refresh_table()


@pytest.mark.asyncio
async def test_rows_and_header_carry_the_flow_state(fleet: Path) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _status(app)
        assert app._header_bar is not None
        assert "FLOW? 1" in str(app._header_bar.render())
        ticket_row = app._tickets_by_id[virtual_row_id("DR-3")]
        assert "⏸ review?" in str(ticket_row.render())
        assert "▶ notify" in str(app._rows_by_sid["s1"].render())


@pytest.mark.asyncio
async def test_f_then_y_approves_and_asks_for_a_tick(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        ticks: list[bool] = []
        monkeypatch.setattr(app, "_refresh_board", lambda: ticks.append(True))
        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, FlowsScreen)
        await pilot.press("y")
        await pilot.pause()
        stored = runs.load("DR-3.review.10-1")
        assert stored is not None and stored.state is RunState.CLAIMED
        assert ticks == [True]
        assert app._lanes.awaiting_count == 0
        # `y` again: the run is claimed now, and the screen says so.
        await pilot.press("y")
        await pilot.pause()
        assert "only a run awaiting confirmation" in str(
            app.screen.query_one("#flows-status").render()
        )
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, FlowsScreen)


@pytest.mark.asyncio
async def test_f_then_n_denies_without_a_tick(fleet: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        ticks: list[bool] = []
        monkeypatch.setattr(app, "_refresh_board", lambda: ticks.append(True))
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        stored = runs.load("DR-3.review.10-1")
        assert stored is not None and stored.state is RunState.DENIED
        assert ticks == []


@pytest.mark.asyncio
async def test_n_in_the_flows_screen_is_not_next_attention(fleet: Path) -> None:
    """The screen's keys win over the dashboard's while it is open."""
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        moved: list[bool] = []
        app.action_next_attention = lambda: moved.append(True)  # type: ignore[method-assign]
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert moved == []


@pytest.mark.asyncio
async def test_an_approve_during_a_tick_queues_another(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        started: list[bool] = []
        monkeypatch.setattr(app, "_board_worker", lambda: started.append(True))
        app._refresh_board()
        assert started == [True]
        app._request_tick()  # one is in flight: queued, not started
        assert started == [True]
        app._on_board_data(None)
        assert started == [True, True]
        app._on_board_data(None)
        assert started == [True, True]


def test_gather_runs_the_tick_when_jira_is_configured(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def fake_tick(config: object, **_: object) -> flows_tick.TickResult:
        calls.append(config)
        return flows_tick.TickResult(flows_tick.TickStatus.LOCKED, snapshot=snap.load())

    monkeypatch.setattr(flows_tick, "run", fake_tick)
    data = lanes_module.gather(refresh=True)
    assert calls == []  # Jira is off
    assert {r.run_id for r in data.runs} == {"DR-3.review.10-1", "DR-1.notify.3-1"}
    monkeypatch.setenv("EPHOR_JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("EPHOR_JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("EPHOR_JIRA_TOKEN", "t")
    assert jira_api.is_configured()
    data = lanes_module.gather(refresh=True)
    assert len(calls) == 1
    assert data.result is not None and not data.result.ok
    assert data.snapshot is not None and "DR-3" in data.snapshot.tickets


# --- keys inside the flows screen never reach a session -----------------------------------


@pytest.mark.asyncio
async def test_enter_space_and_clicks_in_the_flows_screen_never_act_on_a_session(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ephor.tui import app as app_module

    jumped: list[object] = []
    monkeypatch.setattr(app_module, "jump_to", lambda agent: jumped.append(agent))
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _status(app)
        session_list = app.query_one("#session-list", app_module.ListView)
        session_list.index = app._sid_by_row.index("s1")
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, FlowsScreen)
        await pilot.press("down", "space", "enter")
        await pilot.pause()
        await pilot.click("#flows-list ListItem")
        await pilot.pause()
        assert isinstance(app.screen, FlowsScreen)
        assert jumped == []
        assert app._selected == set()
        # Calling the actions directly while the modal is up is inert too.
        await app.action_jump()
        app.action_toggle_select()
        assert jumped == [] and app._selected == set()
        await pilot.press("escape")
        await pilot.pause()
        # Back on the dashboard, space marks the session under the cursor again.
        await pilot.press("space")
        await pilot.pause()
        assert app._selected == {"s1"}


@pytest.mark.asyncio
async def test_the_flows_title_counts_what_the_rows_show_now(fleet: Path) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        app._refresh_board = lambda: None  # type: ignore[method-assign]
        await pilot.press("f")
        await pilot.pause()
        title = app.screen.query_one("#flows-title")
        assert "1 awaiting" in str(title.render())
        await pilot.press("y")
        await pilot.pause()
        assert "0 awaiting" in str(title.render())


# --- the board says when it is not working ------------------------------------------------


def _health_config(text: str) -> board.BoardConfig:
    config = board.parse(__import__("tomllib").loads(text))
    return config


def test_health_is_quiet_without_a_board_and_when_all_is_well() -> None:
    assert Lanes().health() == ""
    ok = _health_config(_BOARD)
    now = datetime.now(UTC)
    fresh = Snapshot(taken_at=now.isoformat())
    result = snap.RefreshResult(snap.RefreshStatus.OK, snapshot=fresh)
    assert Lanes(LaneData(config=ok, snapshot=fresh, result=result)).health(now) == ""
    locked = snap.RefreshResult(snap.RefreshStatus.LOCKED, snapshot=fresh)
    assert Lanes(LaneData(config=ok, snapshot=fresh, result=locked)).health(now) == ""


def test_health_reports_flow_errors_a_failing_search_and_a_stale_snapshot() -> None:
    broken = _health_config(
        _BOARD + '\n[[flow]]\nname = "review"\non_enter = "Peer Review"\n'
        'start = "/review-pr {ticket}"\nmode = "auto"\n'
    )
    assert broken.flow_errors and broken.lanes_ok
    assert "1 flow error(s)" in Lanes(LaneData(config=broken)).health()

    now = datetime.now(UTC)
    old = Snapshot(taken_at=(now - timedelta(hours=2)).isoformat())
    failed = snap.RefreshResult(snap.RefreshStatus.SEARCH_FAILED, snapshot=None)
    health = Lanes(LaneData(config=_health_config(_BOARD), snapshot=old, result=failed)).health(now)
    assert "Jira search failed" in health and "snapshot 2h old" in health

    off = Lanes(LaneData(config=_health_config(_BOARD), jira_missing="email (X or Y)"))
    assert off.health() == "Jira not configured — missing email (X or Y)"

    bad_board = _health_config('[board]\ncolumns = []\nprojects = ["DR"]\n')
    assert "[board] has" in Lanes(LaneData(config=bad_board)).health()


def test_the_header_shows_a_flows_problem() -> None:
    summary = StatusSummary()
    assert "FLOWS ✗" not in _plain(format_header(summary))
    shown = _plain(format_header(summary, flows_problem="board: Jira search failed [x]"))
    assert "FLOWS ✗ board: Jira search failed [x]" in shown


@pytest.mark.asyncio
async def test_a_change_in_board_health_is_toasted_once(fleet: Path) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        config = _health_config(_BOARD)
        failed = snap.RefreshResult(snap.RefreshStatus.SEARCH_FAILED)
        toasts: list[str] = []
        app._set_toast = toasts.append  # type: ignore[method-assign]
        app._flows_health = ""
        app._on_board_data(LaneData(config=config, result=failed))
        app._on_board_data(LaneData(config=config, result=failed))
        assert toasts == ["flows: board: Jira search failed — snapshot left as it was"]
        ok = snap.RefreshResult(snap.RefreshStatus.OK, snapshot=Snapshot(taken_at=""))
        app._on_board_data(LaneData(config=config, result=ok))
        assert toasts[-1] == "flows: board refresh is working again"
        assert app._header_bar is not None


@pytest.mark.asyncio
async def test_status_grouping_without_a_board_says_where_to_look(tmp_path: Path) -> None:
    directory = tmp_path / "sessions"
    directory.mkdir()
    app = EphorApp(manager=StateManager(directory))
    async with app.run_test() as pilot:
        await pilot.pause()
        toasts: list[str] = []
        app._set_toast = toasts.append  # type: ignore[method-assign]
        while app._group_by != "status":
            app.action_cycle_grouping()
        assert toasts[-1] == "grouping: status — no board.toml, default lanes (see docs/flows.md)"

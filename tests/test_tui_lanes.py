"""Status lanes in the dashboard, and the rule that a row which is not a session is never one."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from textual.content import Content

from ephor import board, launcher, permissions, work_items
from ephor.config import SCHEMA_VERSION, config_dir
from ephor.flows import snapshot as snap
from ephor.flows.snapshot import Snapshot, TicketState
from ephor.jira_api import Issue
from ephor.state.manager import StateManager
from ephor.tui import app as app_module
from ephor.tui.app import EphorApp
from ephor.tui.lanes import LaneData, Lanes, markup_text, ticket_row_markup
from ephor.tui.rows import (
    group_key_of,
    group_row_id,
    is_group_row,
    is_session_row,
    is_virtual_row,
    ticket_of_virtual_row,
    virtual_row_id,
)

_BOARD = """
[board]
projects = ["DR"]
columns = ["To Do", "In Progress", { name = "Review", statuses = ["Peer Review", "QA"] }, "Done"]
"""


def _write_session(directory: Path, sid: str, **fields: object) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": sid,
        "cwd": str(directory / sid),
        "started_at": "2026-01-01T00:00:00+00:00",
        "status": "IDLE",
        "project_name": sid,
        "last_event": "Stop",
        "last_event_time": "2026-01-01T00:00:01+00:00",
        "last_event_seq": 1,
        "tool_count": 0,
        "error_count": 0,
        "schema_version": SCHEMA_VERSION,
        **fields,
    }
    (directory / f"{sid}.json").write_text(json.dumps(payload))


def _ticket(status: str, title: str = "", category: str = "", **extra: Any) -> TicketState:
    return TicketState(
        status=status, status_id=status, status_category=category, title=title, **extra
    )


def _write_board(text: str = _BOARD) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / board.BOARD_FILE).write_text(text)


def _write_snapshot(tickets: dict[str, TicketState]) -> None:
    taken = datetime.now(UTC).isoformat(timespec="seconds")
    assert snap.save(Snapshot(taken_at=taken, tickets=tickets))


@pytest.fixture
def lanes_fleet(tmp_path: Path) -> Path:
    """Sessions on DR-1 (In Progress) and DR-2 (Peer Review), one unticketed;
    DR-3 (To Do), DR-4 (Done) and DR-5 (Blocked) have no session."""
    directory = tmp_path / "sessions"
    _write_session(directory, "s1", ticket="DR-1", project_name="alpha")
    _write_session(directory, "s2", ticket="DR-2", project_name="beta")
    _write_session(directory, "s3", project_name="gamma")
    _write_board()
    _write_snapshot(
        {
            "DR-1": _ticket("In Progress", "First"),
            "DR-2": _ticket("Peer Review", "Second"),
            "DR-3": _ticket("To Do", "Third [bold]x[/]"),
            "DR-4": _ticket("Done", "Fourth", "done"),
            "DR-5": _ticket("Blocked", "Fifth"),
            "DR-6": _ticket("To Do", "Gone", unseen_ticks=2),
        }
    )
    return directory


async def _status_app(app: EphorApp) -> None:
    while app._group_by != "status":
        app.action_cycle_grouping()
    await app._refresh_table()


# --- row ids ----------------------------------------------------------------


def test_row_id_kinds_never_overlap() -> None:
    header, ticket = group_row_id("Review"), virtual_row_id("DR-3")
    assert is_group_row(header) and not is_virtual_row(header) and not is_session_row(header)
    assert is_virtual_row(ticket) and not is_group_row(ticket) and not is_session_row(ticket)
    assert is_session_row("s1") and not is_group_row("s1") and not is_virtual_row("s1")
    assert not is_session_row(None) and not is_session_row("")
    assert group_key_of(header) == "Review"
    assert ticket_of_virtual_row(ticket) == "DR-3"


# --- Lanes (pure) -------------------------------------------------------------


def _lanes(tickets: dict[str, TicketState], **work: str) -> Lanes:
    config = board.parse(
        {
            "board": {
                "projects": ["DR"],
                "columns": [
                    "To Do",
                    "In Progress",
                    {"name": "Review", "statuses": ["Peer Review", "QA"]},
                    "Done",
                ],
            }
        }
    )
    return Lanes(
        LaneData(config=config, snapshot=Snapshot(taken_at="", tickets=tickets), work_status=work)
    )


def test_lane_order_is_columns_then_other_then_unticketed() -> None:
    lanes = _lanes({})
    assert lanes.order() == ("To Do", "In Progress", "Review", "Done", "Other", "Unticketed")


def test_status_comes_from_the_snapshot_then_live_then_work_record() -> None:
    lanes = _lanes(
        {"DR-1": _ticket("Peer Review"), "DR-2": _ticket("QA", unseen_ticks=3)},
        **{"DR-2": "In Progress", "DR-3": "To Do"},
    )
    assert lanes.lane_of("DR-1", live_status="To Do") == "Review"  # snapshot wins
    assert lanes.lane_of("DR-2") == "In Progress"  # unseen: the work record is fresher
    assert lanes.lane_of("DR-2", live_status="Done") == "Done"  # and the live lookup fresher still
    assert lanes.lane_of("DR-3") == "To Do"
    assert lanes.lane_of("DR-9") == "Other"  # a ticket nobody knows the status of
    assert lanes.lane_of(None) == "Unticketed"
    assert lanes.lane_of("") == "Unticketed"


def test_an_invalid_board_falls_back_to_the_default_lanes() -> None:
    config = board.parse({"board": {"projects": ["DR"], "columns": []}})
    lanes = Lanes(LaneData(config=config))
    assert lanes.board_errors
    assert lanes.order() == ("To Do", "In Progress", "Done", "Other", "Unticketed")


def test_finished_lanes_are_folded_once() -> None:
    lanes = _lanes({"DR-1": _ticket("Shipped", category="done")})
    collapsed: set[str] = set()
    lanes.apply_default_folds(collapsed)
    assert collapsed == {"Done"}  # Other holds a done ticket but is never auto-folded
    collapsed.discard("Done")  # unfolded by hand…
    lanes.apply_default_folds(collapsed)
    assert collapsed == set()  # …stays unfolded
    lanes.reset_folds()
    lanes.apply_default_folds(collapsed)
    assert collapsed == {"Done"}


def test_a_lane_whose_tickets_are_all_done_counts_as_finished() -> None:
    lanes = _lanes({"DR-1": _ticket("QA", category="done")})
    assert "Review" in lanes.done_lanes()


def test_sessionless_tickets_are_sorted_and_filtered() -> None:
    lanes = _lanes(
        {
            "DR-10": _ticket("To Do", "ten"),
            "DR-9": _ticket("To Do", "nine"),
            "DR-1": _ticket("To Do", "one"),
            "DR-2": _ticket("Done", "gone", unseen_ticks=1),
        }
    )
    rows = lanes.tickets_without_session({"DR-1"})
    assert [key for key, _ in rows["To Do"]] == ["DR-9", "DR-10"]
    assert "Done" not in rows  # unseen tickets are not drawn
    assert [key for key, _ in lanes.tickets_without_session(set(), "NINE")["To Do"]] == ["DR-9"]


@pytest.mark.parametrize(
    "title",
    ["[$accent]boom", "[bold red]x", "x [/]", "trailing \\", "a\\[b]", "[bold red]boom[/]"],
)
def test_ticket_row_markup_shows_ticket_text_verbatim(title: str) -> None:
    """Parsed the way the widget parses it, the title survives exactly."""
    record = _ticket("To Do", title, assignee="Ann")
    plain = Content.from_markup(ticket_row_markup("DR-3", record)).plain
    assert title in plain
    # A trailing backslash must not swallow the closing tag after it, so
    # the assignee that follows the title still renders as itself.
    assert plain.rstrip().endswith("Ann")
    assert "[/]" not in plain.replace(title, "")


def test_ticket_row_markup_strips_control_characters() -> None:
    markup = ticket_row_markup("DR-3", _ticket("To Do", "boom\x1b[2J"))
    assert "\x1b" not in markup


def test_markup_text_is_inert_for_every_markup_shape() -> None:
    for text in ("[$accent]boom", "trailing \\", "x [/]", "[bold red]hi", "a\\[b]", "[@click=x]y"):
        assert Content.from_markup(f"[dim]{markup_text(text)}[/] after").plain.startswith(text)
        assert (
            Content.from_markup(f"[dim]{markup_text(text)}[/] after")
            .plain.rstrip()
            .endswith("after")
        )


def test_lane_header_markup_escapes_the_lane_name() -> None:
    header = _lanes({}).header_markup("Blocked [/] \\", [], 0, False)
    assert "Blocked [/] \\" in Content.from_markup(header).plain


# --- the dashboard -------------------------------------------------------------


@pytest.mark.asyncio
async def test_g_cycles_into_status_lanes(lanes_fleet: Path) -> None:
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        seen = []
        for _ in range(len(EphorApp.GROUPINGS)):
            app.action_cycle_grouping()
            seen.append(app._group_by)
        assert seen == ["ticket", "repo", "status", "none"]


@pytest.mark.asyncio
async def test_status_lanes_order_rows_and_fold_done(lanes_fleet: Path) -> None:
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _status_app(app)
        assert app._sid_by_row == [
            group_row_id("To Do"),
            virtual_row_id("DR-3"),
            group_row_id("In Progress"),
            "s1",
            group_row_id("Review"),
            "s2",
            group_row_id("Done"),  # folded: DR-4 is hidden under it
            group_row_id("Other"),
            virtual_row_id("DR-5"),
            group_row_id("Unticketed"),
            "s3",
        ]
        # Sessions with a ticket are never drawn twice as ticket rows, and a
        # ticket the last search missed is not drawn at all.
        assert virtual_row_id("DR-1") not in app._sid_by_row
        assert virtual_row_id("DR-6") not in app._sid_by_row


@pytest.mark.asyncio
async def test_unfolding_done_shows_its_tickets_and_stays_unfolded(lanes_fleet: Path) -> None:
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _status_app(app)
        list_view = app.query_one("ListView")
        list_view.index = app._sid_by_row.index(group_row_id("Done"))
        app.action_toggle_collapse()
        await app._refresh_table()
        await app._refresh_table()
        assert virtual_row_id("DR-4") in app._sid_by_row


@pytest.mark.asyncio
async def test_a_session_is_placed_by_its_work_record_when_the_board_lacks_it(
    lanes_fleet: Path,
) -> None:
    _write_session(lanes_fleet, "s4", ticket="DR-7", project_name="delta")
    work_items.record("DR-7", jira_status="Peer Review")
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _status_app(app)
        order = app._sid_by_row
        assert order.index(group_row_id("Review")) < order.index("s4")
        assert order.index("s4") < order.index(group_row_id("Done"))


@pytest.mark.asyncio
async def test_a_filtered_out_session_does_not_turn_its_ticket_into_a_ticket_row(
    lanes_fleet: Path,
) -> None:
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _status_app(app)
        app._filter = "beta"
        await app._refresh_table()
        assert "s2" in app._sid_by_row
        assert "s1" not in app._sid_by_row
        assert virtual_row_id("DR-1") not in app._sid_by_row
        # Ticket rows are filtered on their own text.
        assert virtual_row_id("DR-3") not in app._sid_by_row
        app._filter = "third"
        await app._refresh_table()
        assert virtual_row_id("DR-3") in app._sid_by_row


@pytest.mark.asyncio
async def test_lane_headers_count_sessionless_tickets(lanes_fleet: Path) -> None:
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _status_app(app)
        markup = app._group_header_markup("To Do", [])
        assert "1 ticket(s) without a session" in markup
        row = app._tickets_by_id[virtual_row_id("DR-3")]
        assert "DR-3" in str(row.render())


@pytest.mark.asyncio
async def test_lanes_follow_a_new_snapshot_without_a_restart(lanes_fleet: Path) -> None:
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _status_app(app)
        _write_snapshot({"DR-1": _ticket("QA", "First"), "DR-2": _ticket("Peer Review")})
        app._on_board_data(app_module.gather_lanes(refresh=False))
        await app._refresh_table()
        order = app._sid_by_row
        assert order.index(group_row_id("Review")) < order.index("s1")
        assert virtual_row_id("DR-3") not in order


# --- row safety: a ticket row is never a session ---------------------------------


async def _cursor_on(app: EphorApp, row_id: str) -> None:
    await _status_app(app)
    app.query_one("ListView").index = app._sid_by_row.index(row_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("row_id", [virtual_row_id("DR-3"), group_row_id("Review")])
async def test_actions_on_a_non_session_row_never_fall_back_to_the_marks(
    lanes_fleet: Path, monkeypatch: pytest.MonkeyPatch, row_id: str
) -> None:
    """The bug this guards: `a` on a ticket row granting the marked sessions."""
    fired: list[str] = []
    monkeypatch.setattr(app_module, "kill_session", lambda *a, **k: fired.append("kill"))
    monkeypatch.setattr(app_module, "jump_to", lambda *a, **k: fired.append("jump"))
    monkeypatch.setattr(launcher, "start", lambda *a, **k: fired.append("start"))
    monkeypatch.setattr(launcher, "resume", lambda *a, **k: fired.append("resume"))
    monkeypatch.setattr(permissions, "revoke", lambda *a, **k: fired.append("revoke"))

    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "_summarize", lambda *a, **k: fired.append("summarize"))
        monkeypatch.setattr(app, "open_url", lambda *a, **k: fired.append("open"))
        toasts: list[str] = []
        app._set_toast = toasts.append  # type: ignore[method-assign]
        await _cursor_on(app, row_id)
        app._selected = {"s1", "s2"}

        assert app._cursor_session_sid() is None
        assert app._target_sids() == []

        app.action_allow()
        app.action_deny()
        app.action_allow_standing()
        app.action_revoke()
        await app.action_kill()
        await app.action_kill()  # the second press is the one that would fire
        app.action_summarize()
        app.action_open_pr()
        app.action_pin_ticket()
        app.action_new_session()
        app.action_resume_session()
        # start/resume run on thread workers: let any that were (wrongly)
        # scheduled reach the patched launcher before asserting nothing fired.
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert permissions.pending() == {}
        assert fired == []
        assert app._ticket_input is not None and not app._ticket_input.display
        assert app._selected == {"s1", "s2"}  # marks are kept, just not used from here
        assert any("session row" in t for t in toasts)


@pytest.mark.asyncio
async def test_enter_on_a_ticket_row_describes_it_instead_of_jumping(
    lanes_fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jumped: list[object] = []
    monkeypatch.setattr(app_module, "jump_to", jumped.append)
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        toasts: list[str] = []
        app._set_toast = toasts.append  # type: ignore[method-assign]
        await _cursor_on(app, virtual_row_id("DR-3"))
        await app.action_jump()
        assert jumped == []
        assert toasts[-1] == "DR-3 · To Do — no session on this ticket"


@pytest.mark.asyncio
async def test_enter_on_a_ticket_row_with_a_markup_like_status_keeps_running(
    lanes_fleet: Path,
) -> None:
    """A Jira status is admin-typed text; the toast must not parse it as markup."""
    _write_snapshot({"DR-3": _ticket("Blocked [/]", "Third")})
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _cursor_on(app, virtual_row_id("DR-3"))
        await app.action_jump()
        await pilot.pause()
        assert app.is_running
        toast = app.query_one(app_module.StatusToast)
        assert "Blocked [/]" in Content.from_markup(str(toast.content)).plain
        assert "Blocked [/]" in str(toast.render())


@pytest.mark.asyncio
async def test_space_on_a_ticket_row_marks_nothing(lanes_fleet: Path) -> None:
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _cursor_on(app, virtual_row_id("DR-3"))
        app.action_toggle_select()
        assert app._selected == set()


@pytest.mark.asyncio
async def test_space_on_a_lane_header_marks_only_its_sessions(lanes_fleet: Path) -> None:
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _cursor_on(app, group_row_id("To Do"))
        app.action_toggle_select()
        assert app._selected == set()  # To Do holds only a ticket row
        await _cursor_on(app, group_row_id("In Progress"))
        app.action_toggle_select()
        assert app._selected == {"s1"}


@pytest.mark.asyncio
async def test_z_on_a_ticket_row_folds_its_lane(lanes_fleet: Path) -> None:
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _cursor_on(app, virtual_row_id("DR-5"))
        app.action_toggle_collapse()
        assert "Other" in app._collapsed


@pytest.mark.asyncio
async def test_marks_still_apply_from_a_session_row(lanes_fleet: Path) -> None:
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _cursor_on(app, "s3")
        app._selected = {"s1", "s2"}
        assert app._target_sids() == ["s1", "s2"]
        app.action_allow()
        assert set(permissions.pending()) == {"s1", "s2"}


@pytest.mark.asyncio
async def test_next_attention_skips_ticket_rows(lanes_fleet: Path) -> None:
    _write_session(lanes_fleet, "s1", ticket="DR-1", status="WAITING_PERMISSION")
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _cursor_on(app, virtual_row_id("DR-3"))
        app.action_next_attention()
        assert app._sid_by_row[app.query_one("ListView").index] == "s1"


@pytest.mark.asyncio
async def test_an_only_ticket_rows_board_is_not_rebuilt_every_tick(tmp_path: Path) -> None:
    """With no sessions the rows are all headers and ticket rows; repaint in place."""
    _write_board()
    _write_snapshot({"DR-3": _ticket("To Do", "Third")})
    app = EphorApp(manager=StateManager(tmp_path / "sessions"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await _status_app(app)
        item = app._items_by_sid[virtual_row_id("DR-3")]
        await app._refresh_table()
        assert app._items_by_sid[virtual_row_id("DR-3")] is item


# --- the refresh worker -------------------------------------------------------


@pytest.mark.asyncio
async def test_the_dashboard_refreshes_the_snapshot_when_jira_is_configured(
    lanes_fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ephor import jira_api

    monkeypatch.setenv("EPHOR_JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("EPHOR_JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("EPHOR_JIRA_TOKEN", "t")
    searches: list[str] = []

    def search(jql: str, fields: tuple[str, ...] = ()) -> list[Issue]:
        searches.append(jql)
        return [Issue(key="DR-8", status="QA", status_id="7", title="Eight")]

    monkeypatch.setattr(jira_api, "search", search)
    # The per-ticket lookups are not what this test is about; keep them offline.
    monkeypatch.setattr(jira_api, "_fetch", lambda *a, **k: None)

    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert searches == ["project in (DR) AND (" + board.DEFAULT_JQL + ")"]
        assert app._lanes.snapshot is not None
        assert "DR-8" in app._lanes.snapshot.on_board()
        stored = snap.load()
        assert stored is not None and "DR-8" in stored.tickets


@pytest.mark.asyncio
async def test_without_jira_the_dashboard_only_reads_the_snapshot(
    lanes_fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ephor import jira_api

    # Recorded, not raised: the board worker swallows exceptions, so a
    # raising stub would let a search without credentials pass unnoticed.
    calls: list[object] = []
    monkeypatch.setattr(jira_api, "search", lambda *a, **k: calls.append(a) or [])
    before = snap.snapshot_path().read_text()
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert calls == []
        assert app._lanes.last_result is None
        assert app._lanes.snapshot is not None
        assert "DR-3" in app._lanes.snapshot.tickets
    assert snap.snapshot_path().read_text() == before


@pytest.mark.asyncio
async def test_board_refreshes_never_overlap(
    lanes_fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[bool] = []
    app = EphorApp(manager=StateManager(lanes_fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        monkeypatch.setattr(app, "_board_worker", lambda: started.append(True))
        app._refresh_board()
        app._refresh_board()  # the first is still "running": skipped
        assert started == [True]
        app._on_board_data(None)  # a failed worker still clears the flag
        app._refresh_board()
        assert started == [True, True]

"""Dashboard behaviour at fleet scale: grouping, marking, the permission inbox."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ephor import permissions, work_items
from ephor.config import SCHEMA_VERSION
from ephor.constants import CiState, WorkAttention
from ephor.github import PullRequest
from ephor.state.manager import StateManager
from ephor.tui.app import EphorApp, group_key_of, is_group_row


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


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    """Three sessions: two on one ticket, one on another."""
    directory = tmp_path / "sessions"
    _write_session(directory, "s1", ticket="DR-1", project_name="alpha")
    _write_session(directory, "s2", ticket="DR-1", project_name="beta")
    _write_session(directory, "s3", ticket="DR-2", project_name="gamma")
    return directory


# --- grouping --------------------------------------------------------------


@pytest.mark.asyncio
async def test_grouping_by_ticket_inserts_headers_and_gathers_rows(fleet: Path) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not any(is_group_row(r) for r in app._sid_by_row)

        app.action_cycle_grouping()
        await app._refresh_table()
        assert app._group_by == "ticket"

        headers = [r for r in app._sid_by_row if is_group_row(r)]
        assert sorted(group_key_of(h) for h in headers) == ["DR-1", "DR-2"]
        # A ticket's sessions sit together, under their own header.
        order = app._sid_by_row
        assert order.index("s1") > order.index("\x00grp:DR-1")
        assert order.index("s3") > order.index("\x00grp:DR-2")


@pytest.mark.asyncio
async def test_collapsing_a_group_hides_its_sessions(fleet: Path) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_cycle_grouping()
        await app._refresh_table()

        app._collapsed.add("DR-1")
        await app._refresh_table()
        assert "s1" not in app._sid_by_row
        assert "s2" not in app._sid_by_row
        assert "s3" in app._sid_by_row  # a different group is untouched
        assert "\x00grp:DR-1" in app._sid_by_row  # the header stays, to unfold


@pytest.mark.asyncio
async def test_cycling_grouping_drops_stale_folds(fleet: Path) -> None:
    """Group names differ per mode, so a fold from the old mode hides nothing."""
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_cycle_grouping()
        app._collapsed.add("DR-1")
        app.action_cycle_grouping()
        assert app._group_by == "repo"
        assert app._collapsed == set()


@pytest.mark.asyncio
async def test_a_header_row_is_never_mistaken_for_a_session(fleet: Path) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_cycle_grouping()
        await app._refresh_table()
        list_view = app.query_one("ListView")
        list_view.index = app._sid_by_row.index("\x00grp:DR-1")

        assert app._cursor_session_sid(list_view) is None
        assert app._target_sids() == []


# --- selection -------------------------------------------------------------


@pytest.mark.asyncio
async def test_marking_rows_makes_actions_batch(fleet: Path) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._selected = {"s1", "s3"}
        # On-screen order, not set order.
        assert app._target_sids() == ["s1", "s3"]


@pytest.mark.asyncio
async def test_without_marks_actions_fall_back_to_the_cursor(fleet: Path) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("ListView").index = app._sid_by_row.index("s2")
        assert app._target_sids() == ["s2"]


@pytest.mark.asyncio
async def test_space_on_a_header_marks_the_whole_group(fleet: Path) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_cycle_grouping()
        await app._refresh_table()
        app.query_one("ListView").index = app._sid_by_row.index("\x00grp:DR-1")

        app.action_toggle_select()
        assert app._selected == {"s1", "s2"}
        # Pressing it again on a fully-marked group unmarks it.
        app.action_toggle_select()
        assert app._selected == set()


@pytest.mark.asyncio
async def test_clear_selection(fleet: Path) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._selected = {"s1", "s2"}
        app.action_clear_selection()
        assert app._selected == set()


@pytest.mark.asyncio
async def test_a_vanished_session_drops_out_of_the_selection(fleet: Path) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._selected = {"s1", "s3"}
        (fleet / "s3.json").unlink()
        await app._refresh_table()
        assert app._selected == {"s1"}


# --- permission inbox ------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_writes_a_decision_for_every_marked_session(fleet: Path) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._selected = {"s1", "s2"}
        app.action_allow()
        queued = permissions.pending()
        assert set(queued) == {"s1", "s2"}
        assert all(entry.decision is permissions.Decision.ALLOW for entry in queued.values())


@pytest.mark.asyncio
async def test_standing_allow_and_revoke(fleet: Path) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._selected = {"s1"}
        app.action_allow_standing()
        assert permissions.standing_for("s1") is permissions.Decision.ALLOW
        app.action_revoke()
        assert permissions.standing_for("s1") is None


@pytest.mark.asyncio
async def test_the_toast_does_not_claim_a_session_was_unblocked(fleet: Path) -> None:
    """Without a wait window the answer lands on the *next* request, not this one."""
    app = EphorApp(manager=StateManager(fleet))
    captured: list[str] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app._set_toast = captured.append  # type: ignore[method-assign]
        app._selected = {"s1"}
        app.action_deny()
        assert "queued for the next request" in captured[-1]


@pytest.mark.asyncio
async def test_with_a_wait_window_the_toast_says_now(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPHOR_PERMISSION_WAIT_SEC", "5")
    app = EphorApp(manager=StateManager(fleet))
    captured: list[str] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app._set_toast = captured.append  # type: ignore[method-assign]
        app._selected = {"s1"}
        app.action_allow()
        assert captured[-1].endswith("now")


# --- work attention --------------------------------------------------------


@pytest.mark.asyncio
async def test_next_attention_finds_an_idle_session_with_red_ci(fleet: Path) -> None:
    """The row this exists for: agent finished, work is not done."""
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        failing = PullRequest(number=1, url="u", state="OPEN", ci=CiState.FAILING)
        cwds = {a.session_id: a.cwd for a in StateManager(fleet).scan()}
        app._prs.cached = lambda cwd: failing if cwd == cwds["s2"] else None  # type: ignore[assignment]

        captured: list[str] = []
        app._set_toast = captured.append  # type: ignore[method-assign]
        app.action_next_attention()
        assert app._sid_by_row[app.query_one("ListView").index] == "s2"
        assert "CI" in captured[-1]


@pytest.mark.asyncio
async def test_work_attention_reports_ticket_drift(fleet: Path) -> None:
    from ephor.jira_api import Issue

    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        merged = PullRequest(number=1, url="u", state="MERGED")
        app._prs.cached = lambda cwd: merged  # type: ignore[assignment]
        app._issues.cached = lambda key: Issue(  # type: ignore[assignment]
            key="DR-1", status="In Progress", status_category="indeterminate"
        )
        agent = next(a for a in StateManager(fleet).scan() if a.session_id == "s1")
        assert app._work_attention(agent) is WorkAttention.TICKET_DRIFT


# --- observation -----------------------------------------------------------


@pytest.mark.asyncio
async def test_status_transitions_are_logged_once_not_per_repaint(fleet: Path) -> None:
    from ephor import eventlog

    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._refresh_table()
        await app._refresh_table()
        starts = [e for e in eventlog.read() if e.kind is eventlog.EventKind.SESSION_STARTED]
        assert len(starts) == 3  # one per session, not one per refresh

        _write_session(fleet, "s1", ticket="DR-1", status="WORKING", project_name="alpha")
        await app._refresh_table()
        changes = [e for e in eventlog.read() if e.kind is eventlog.EventKind.STATUS_CHANGED]
        assert len(changes) == 1
        assert changes[0].detail == "IDLE → WORKING"


@pytest.mark.asyncio
async def test_a_confirmed_ticket_is_promoted_into_the_work_record(fleet: Path) -> None:
    app = EphorApp(manager=StateManager(fleet))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._refresh_table()
        item = work_items.load("DR-1")
        assert item is not None
        assert item.confirmed is True
        assert set(item.session_ids) >= {"s1", "s2"}

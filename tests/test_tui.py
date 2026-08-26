"""Tests for the Textual TUI dashboard. Uses Textual's Pilot harness.

We don't try to assert pixel layout; just that the table populates from
StateManager and that key bindings invoke the right action.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ephor.state.manager import StateManager
from ephor.state.models import AgentState
from ephor.tui import app as tui_app
from ephor.tui.app import EphorApp


def _write_state(directory: Path, sid: str, **overrides: Any) -> None:
    base = {
        "session_id": sid,
        "cwd": "/tmp/x",
        "started_at": "2026-04-29T10:00:00Z",
        "last_event_time": "2026-04-29T10:00:00Z",
    }
    base.update(overrides)
    state = AgentState(**base)
    (directory / f"{sid}.json").write_text(state.to_json())


async def _list_view(app: EphorApp, pilot: Any) -> Any:
    """Return the app's mounted ListView, waiting for compose to finish.

    A single `pilot.pause()` yields one message-pump cycle. That is enough on
    a fast machine, but on a loaded CI runner the query can land before the
    widget has mounted and raise NoMatches. Pump until it shows up instead of
    assuming one cycle was enough.
    """
    from textual.widgets import ListView

    for _ in range(50):
        nodes = app.query(ListView)
        if len(nodes):
            return nodes.first()
        await pilot.pause()
    raise AssertionError("ListView never mounted")


@pytest.fixture
def populated_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("EPHOR_STATE_DIR", str(sd))
    _write_state(sd, "alpha-id", project_name="alpha", tool_count=5)
    _write_state(sd, "beta-id", project_name="beta", tool_count=2)
    _write_state(sd, "gamma-id", project_name="gamma", tool_count=0)
    return sd


@pytest.mark.asyncio
async def test_app_populates_table_on_mount(populated_dir: Path) -> None:
    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        # Three state files → three rows.
        assert len(app._sid_by_row) == 3
        assert set(app._sid_by_row) == {"alpha-id", "beta-id", "gamma-id"}


@pytest.mark.asyncio
async def test_app_quits_on_q(populated_dir: Path) -> None:
    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert not app.is_running


@pytest.mark.asyncio
async def test_app_refresh_on_r(populated_dir: Path) -> None:
    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        before_rows = len(app._sid_by_row)
        # Add another session file mid-run.
        _write_state(populated_dir, "delta-id", project_name="delta")
        await pilot.press("r")
        await pilot.pause()
        assert len(app._sid_by_row) == before_rows + 1


@pytest.mark.asyncio
async def test_jump_action_invokes_navigator(
    populated_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing Enter should call jump_to() with the agent at the cursor."""
    from ephor.tmux import navigator

    captured: list[AgentState] = []

    def fake_jump(agent: AgentState) -> Any:
        captured.append(agent)

        class _Outcome:
            ok = True
            result = navigator.JumpResult.OK
            detail = ""

        return _Outcome()

    monkeypatch.setattr(tui_app, "jump_to", fake_jump)

    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        # Directly invoke the action — testing the binding wiring is
        # Textual's job; we just want to confirm the action calls jump_to.
        await app.action_jump()
        await pilot.pause()

    assert len(captured) == 1, "action_jump should call jump_to exactly once"
    assert captured[0].session_id in {"alpha-id", "beta-id", "gamma-id"}


@pytest.mark.asyncio
async def test_enter_keybinding_invokes_jump(
    populated_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing Enter (NOT calling the action directly) must trigger jump_to.

    This is the regression test for the bug where DataTable swallowed Enter
    before our app-level binding fired.
    """
    from ephor.tmux import navigator

    captured: list[AgentState] = []

    def fake_jump(agent: AgentState) -> Any:
        captured.append(agent)

        class _Outcome:
            ok = True
            result = navigator.JumpResult.OK
            detail = ""

        return _Outcome()

    monkeypatch.setattr(tui_app, "jump_to", fake_jump)

    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    assert len(captured) >= 1, "pressing Enter must call jump_to"


# ---- pure-function helpers (no Textual harness needed) -------------------


def test_human_age_renders_seconds() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    ts = (now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = tui_app._human_age(ts)
    assert out.endswith("s") or out.endswith("m")  # rounding-tolerant


def test_human_age_handles_empty() -> None:
    assert tui_app._human_age("") == "-"


def test_human_age_handles_garbage() -> None:
    assert tui_app._human_age("not a date") == "?"


def test_jump_error_has_useful_message() -> None:
    from ephor.tmux.navigator import JumpResult

    assert "tmux" in tui_app._jump_error(JumpResult.NO_TMUX_INFO, "")
    assert "tmux" in tui_app._jump_error(JumpResult.TMUX_MISSING, "")


def test_is_stale_tmux_ref_classifies_recoverable_failures() -> None:
    """Re-discovery should retry on NO_TMUX_INFO and on FAILED-with-can't-find;
    not on TMUX_MISSING / SESSION_NOT_FOUND / unrelated FAILED reasons."""
    from ephor.tmux.navigator import JumpOutcome, JumpResult

    assert tui_app._is_stale_tmux_ref(JumpOutcome(JumpResult.NO_TMUX_INFO, ""))
    assert tui_app._is_stale_tmux_ref(
        JumpOutcome(JumpResult.FAILED, "select-window failed: can't find pane: %16")
    )
    assert tui_app._is_stale_tmux_ref(
        JumpOutcome(JumpResult.FAILED, "select-window failed: can't find window: claude")
    )
    # Not recoverable via re-discovery:
    assert not tui_app._is_stale_tmux_ref(JumpOutcome(JumpResult.OK, ""))
    assert not tui_app._is_stale_tmux_ref(JumpOutcome(JumpResult.TMUX_MISSING, "no tmux"))
    assert not tui_app._is_stale_tmux_ref(
        JumpOutcome(JumpResult.SESSION_NOT_FOUND, "tmux session 'work' no longer exists")
    )
    assert not tui_app._is_stale_tmux_ref(
        JumpOutcome(JumpResult.FAILED, "tmux subprocess timed out")
    )


@pytest.mark.asyncio
async def test_jump_retries_with_rediscovery_on_stale_pane(
    populated_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: when a state file's recorded pane is dead but agent_pid
    is still alive in a new pane, action_jump should call enrich_state_files,
    re-read the state, and retry the jump — instead of just toasting the
    'can't find pane' error."""
    from ephor.tmux.navigator import JumpOutcome, JumpResult

    jump_calls: list[AgentState] = []
    enrich_calls: list[Path] = []

    def fake_jump(agent: AgentState) -> JumpOutcome:
        jump_calls.append(agent)
        # First call returns the stale-pane failure; second call (after
        # enrich rewrote the file) succeeds.
        if len(jump_calls) == 1:
            return JumpOutcome(
                JumpResult.FAILED,
                "select-window failed: can't find pane: %16",
            )
        return JumpOutcome(JumpResult.OK, "")

    def fake_enrich(directory: Path) -> int:
        enrich_calls.append(directory)
        return 1  # pretend we updated one state file

    monkeypatch.setattr(tui_app, "jump_to", fake_jump)
    monkeypatch.setattr(tui_app, "enrich_state_files", fake_enrich)

    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        await app.action_jump()
        await pilot.pause()

    assert len(jump_calls) == 2, "should retry jump_to once after enrich rewrites state"
    assert len(enrich_calls) == 1, "should call enrich_state_files exactly once"


@pytest.mark.asyncio
async def test_jump_does_not_rediscover_on_unrecoverable_failures(
    populated_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FAILED outcome that isn't a 'can't find ...' tmux error must not
    trigger a re-discovery — that would just produce a noisy retry loop."""
    from ephor.tmux.navigator import JumpOutcome, JumpResult

    jump_calls: list[AgentState] = []
    enrich_calls: list[Path] = []

    def fake_jump(agent: AgentState) -> JumpOutcome:
        jump_calls.append(agent)
        return JumpOutcome(JumpResult.FAILED, "tmux subprocess timed out")

    def fake_enrich(directory: Path) -> int:
        enrich_calls.append(directory)
        return 0

    monkeypatch.setattr(tui_app, "jump_to", fake_jump)
    monkeypatch.setattr(tui_app, "enrich_state_files", fake_enrich)

    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        await app.action_jump()
        await pilot.pause()

    assert len(jump_calls) == 1, "no retry expected for non-tmux-ref failures"
    assert enrich_calls == [], "enrich must not run for unrecoverable failures"


@pytest.mark.asyncio
async def test_summarize_action_writes_to_store_and_repaints(
    populated_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pressing `s` triggers a (mocked) summarizer and persists the result."""
    summary_dir = tmp_path / "summaries"
    monkeypatch.setattr("ephor.summary_store.summary_dir", lambda: summary_dir)
    monkeypatch.setattr(
        "ephor.tui.app.summarize_transcript",
        lambda _path, cwd=None: "stubbed summary",
    )

    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        # Reset the store's directory: the populated_dir fixture set EPHOR_STATE_DIR,
        # but SummaryStore was constructed before our monkeypatch took effect.
        from ephor.summary_store import SummaryStore

        app._summaries = SummaryStore()
        app.action_summarize()
        await app.workers.wait_for_complete()
        await pilot.pause()

    sid = app._sid_by_row[0]
    saved = (summary_dir / f"{sid}.json").read_text()
    assert "stubbed summary" in saved


@pytest.mark.asyncio
async def test_summarize_action_with_no_selection_toasts(
    populated_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("ephor.summary_store.summary_dir", lambda: tmp_path / "s")
    app = EphorApp(manager=StateManager(populated_dir))
    captured: list[str] = []
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        # Capture toast messages via the public-ish helper rather than poking
        # at Static internals (which differ across Textual versions).
        monkeypatch.setattr(app, "_set_toast", lambda text: captured.append(text))
        app._sid_by_row = []
        app.action_summarize()
        await pilot.pause()
    assert any("no row" in msg for msg in captured)


@pytest.mark.asyncio
async def test_session_order_is_stable_across_event_time_changes(
    populated_dir: Path,
) -> None:
    """The dashboard sorts by (started_at, session_id) so existing rows
    never shift when a hook fires on one of them.

    Regression: previously sort was by last_event_time desc, which made
    every PreToolUse event reshuffle the list — visually disorienting,
    and the cause of the now-fixed Path-2 reorder flash.
    """
    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        original_items = dict(app._items_by_sid)
        original_sids = list(app._sid_by_row)
        assert len(original_items) == 3

        # Touch beta-id's last_event_time. Order MUST NOT change.
        _write_state(
            populated_dir,
            "beta-id",
            project_name="beta",
            tool_count=2,
            last_event_time="2030-01-01T00:00:00Z",
        )
        await app._refresh_table()
        await pilot.pause()

        assert app._sid_by_row == original_sids, (
            "session order should be stable when last_event_time changes"
        )
        # Same widget objects — proves the no-op (Path 1) path ran.
        for sid, item in original_items.items():
            assert app._items_by_sid[sid] is item


@pytest.mark.asyncio
async def test_new_session_appears_at_bottom_without_reordering(
    populated_dir: Path,
) -> None:
    """Sort by started_at ascending means a newly-started session lands at
    the bottom of the list and doesn't push existing rows around."""
    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        original_sids = list(app._sid_by_row)

        # Add a new session with a LATER started_at — must end up at bottom.
        _write_state(
            populated_dir,
            "delta-id",
            project_name="delta",
            started_at="2099-01-01T00:00:00Z",
        )
        await app._refresh_table()
        await pilot.pause()

        assert app._sid_by_row[: len(original_sids)] == original_sids
        assert app._sid_by_row[-1] == "delta-id"


@pytest.mark.asyncio
async def test_dead_sessions_are_hidden_from_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ephor.constants import AgentStatus

    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("EPHOR_STATE_DIR", str(sd))
    _write_state(sd, "live", project_name="live")
    _write_state(sd, "ghost", project_name="ghost", status=AgentStatus.DEAD)

    app = EphorApp(manager=StateManager(sd))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        assert app._sid_by_row == ["live"]
        assert "ghost" not in app._items_by_sid


@pytest.mark.asyncio
async def test_next_attention_jumps_to_perm_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ephor.constants import AgentStatus

    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("EPHOR_STATE_DIR", str(sd))
    # 4 sessions; only the third needs attention.
    _write_state(sd, "a", project_name="a", status=AgentStatus.IDLE)
    _write_state(sd, "b", project_name="b", status=AgentStatus.WORKING)
    _write_state(sd, "c", project_name="c", status=AgentStatus.WAITING_PERMISSION)
    _write_state(sd, "d", project_name="d", status=AgentStatus.IDLE)

    app = EphorApp(manager=StateManager(sd))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        list_view = await _list_view(app, pilot)
        list_view.index = 0
        await pilot.press("n")
        await pilot.pause()
        # Row index of 'c' in _sid_by_row.
        target_idx = app._sid_by_row.index("c")
        assert list_view.index == target_idx


@pytest.mark.asyncio
async def test_next_attention_wraps_when_past_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ephor.constants import AgentStatus

    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("EPHOR_STATE_DIR", str(sd))
    _write_state(sd, "a", project_name="a", status=AgentStatus.WAITING_PERMISSION)
    _write_state(sd, "b", project_name="b", status=AgentStatus.IDLE)

    app = EphorApp(manager=StateManager(sd))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        list_view = await _list_view(app, pilot)
        # Park cursor past the only attention row to force wrap.
        list_view.index = len(app._sid_by_row) - 1
        await pilot.press("n")
        await pilot.pause()
        target_idx = app._sid_by_row.index("a")
        assert list_view.index == target_idx


def test_agent_matches_filter_substrings() -> None:
    from ephor.tui.app import _agent_matches_filter

    a = AgentState(
        session_id="abc-123",
        cwd="/home/me/projects/marketplace",
        started_at="2026-04-29T10:00:00Z",
        project_name="marketplace",
        last_summary="rewriting the checkout flow",
    )
    assert _agent_matches_filter(a, "market")
    assert _agent_matches_filter(a, "CHECKOUT")  # case-insensitive
    assert _agent_matches_filter(a, "abc")  # session_id matches
    assert not _agent_matches_filter(a, "nope")


@pytest.mark.asyncio
async def test_filter_action_hides_non_matching_rows(populated_dir: Path) -> None:
    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        assert len(app._sid_by_row) == 3

        app._filter = "alpha"
        await app._refresh_table()
        await pilot.pause()
        assert app._sid_by_row == ["alpha-id"]

        # Clearing the filter restores all rows.
        await app.action_clear_filter()
        await pilot.pause()
        assert set(app._sid_by_row) == {"alpha-id", "beta-id", "gamma-id"}


def test_summary_line_shows_cap_when_configured() -> None:
    """When account.weekly_cap_tokens is set, summary line shows used/cap and %."""
    from ephor.state.models import StatusSummary
    from ephor.tui.activity import ActivitySampler
    from ephor.tui.app import _render_summary_line
    from ephor.tui.tokens import TokenTracker

    summary = StatusSummary(working=1)
    sampler = ActivitySampler()
    tokens = TokenTracker()
    # No agents → total_tokens=0, but cap rendering should still apply.
    line = _render_summary_line(summary, [], sampler, tokens, weekly_cap=1_000_000)
    assert "/ 1.0M" in line
    assert "0%" in line


def test_summary_line_omits_cap_when_unset() -> None:
    from ephor.state.models import StatusSummary
    from ephor.tui.app import _render_summary_line

    line = _render_summary_line(StatusSummary(), [], None, None, weekly_cap=None)
    # No "/<cap>" suffix when cap unset; the "[/]" in markup is a closing tag
    # not a slash separator.
    assert " / " not in line
    assert "tokens:" in line


# ---- regression: Enter must stay rock-solid -------------------------------


@pytest.mark.asyncio
async def test_cold_path_refresh_keeps_dom_and_state_in_sync(
    populated_dir: Path,
) -> None:
    """After a cold-path rebuild (sessions added/removed) the ListView's
    children, list_view.index, and self._sid_by_row must all agree.

    This is the regression test for the flakiness root-cause: ListView.clear()
    and .append() are async (return AwaitRemove/AwaitMount) but the previous
    code fired them synchronously, so `_sid_by_row` was overwritten before
    the DOM had finished reshaping. An Enter keypress landing in that window
    saw `list_view.index = None` (clear() resets it) but `_sid_by_row`
    populated, producing a "no row selected" toast even though the visible
    list looked normal.
    """
    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        list_view = await _list_view(app, pilot)

        # Initial mount: 3 sessions. DOM and state must agree.
        assert len(app._sid_by_row) == 3
        assert len(list_view.children) == 3
        assert list_view.index is not None
        assert app._sid_by_row[list_view.index] in {"alpha-id", "beta-id", "gamma-id"}

        # Add a session — forces cold path because the set changed.
        _write_state(populated_dir, "delta-id", project_name="delta")
        await app._refresh_table()
        await pilot.pause()
        assert len(app._sid_by_row) == 4
        assert len(list_view.children) == 4
        assert list_view.index is not None
        assert 0 <= list_view.index < len(app._sid_by_row)

        # Remove two sessions — cold path again, with cursor preserved if
        # possible.
        (populated_dir / "alpha-id.json").unlink()
        (populated_dir / "beta-id.json").unlink()
        await app._refresh_table()
        await pilot.pause()
        assert len(app._sid_by_row) == 2
        assert len(list_view.children) == 2
        assert list_view.index is not None
        assert 0 <= list_view.index < len(app._sid_by_row)
        # The sid the cursor points at must exist in our state mapping.
        assert app._sid_by_row[list_view.index] in app._items_by_sid

        # Drain to empty. Index goes to None, lengths stay aligned.
        for sid in list(app._sid_by_row):
            (populated_dir / f"{sid}.json").unlink()
        await app._refresh_table()
        await pilot.pause()
        assert app._sid_by_row == []
        assert len(list_view.children) == 0
        assert list_view.index is None


@pytest.mark.asyncio
async def test_enter_with_filter_focused_jumps_and_dismisses_input(
    populated_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing Enter while the filter input is focused must:
      1. Hide the filter input (so j/k resume navigating the list).
      2. Move focus back to the ListView.
      3. Still invoke jump_to for the highlighted row.

    The previous behavior left the input visible and focused — j/k stopped
    working until the user found Esc, which felt like Enter was broken.
    """
    from textual.widgets import ListView

    from ephor.tmux import navigator

    captured: list[AgentState] = []

    def fake_jump(agent: AgentState) -> Any:
        captured.append(agent)

        class _Outcome:
            ok = True
            result = navigator.JumpResult.OK
            detail = ""

        return _Outcome()

    monkeypatch.setattr(tui_app, "jump_to", fake_jump)

    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        # Open the filter overlay and type something that matches.
        app.action_filter()
        await pilot.pause()
        assert app._filter_input is not None
        assert app._filter_input.display is True
        assert app.focused is app._filter_input

        await pilot.press("a")  # match "alpha"
        await pilot.pause()
        assert app._filter == "a"

        # Press Enter while filter input is focused.
        await pilot.press("enter")
        await pilot.pause()

        assert len(captured) >= 1, "Enter should still call jump_to"
        # Input is hidden and focus is back on the list.
        assert app._filter_input.display is False
        list_view = app.query_one(ListView)
        assert app.focused is list_view


@pytest.mark.asyncio
async def test_concurrent_refresh_calls_do_not_corrupt_state(
    populated_dir: Path,
) -> None:
    """Two refreshes scheduled back-to-back must not interleave.

    The set_interval timer fires every 500ms and a cold-path rebuild contains
    two awaits (clear, mount). Without re-entrancy guarding, a second tick
    could begin while the first is still building, both ending up writing to
    `_sid_by_row` in a non-deterministic order.
    """
    import asyncio

    from textual.widgets import ListView

    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()

        # Add a session so the next refresh hits the cold path.
        _write_state(populated_dir, "delta-id", project_name="delta")

        # Fire two refreshes "concurrently" — the second one must be a no-op
        # while the first is in flight.
        await asyncio.gather(app._refresh_table(), app._refresh_table())
        await pilot.pause()

        list_view = app.query_one(ListView)
        assert len(app._sid_by_row) == len(list_view.children) == 4
        assert list_view.index is not None
        assert 0 <= list_view.index < len(app._sid_by_row)


@pytest.mark.asyncio
async def test_auto_follow_moves_cursor_to_externally_focused_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the user switches to another tmux client (e.g. clicks a Ghostty
    window hosting session beta), ephor's cursor should move to beta on the
    next refresh — without the user touching the dashboard."""
    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("EPHOR_STATE_DIR", str(sd))
    _write_state(sd, "alpha-id", project_name="alpha", tmux_pane="%10")
    _write_state(sd, "beta-id", project_name="beta", tmux_pane="%20")
    _write_state(sd, "gamma-id", project_name="gamma", tmux_pane="%30")

    # Mutable holder so each test step can change what the stub returns
    # without re-monkeypatching. Mount-time refreshes see None so they
    # don't pre-arm _last_external_pane to a value the test cares about.
    state = {"pane": None}

    def fake_detect(_self_pane: str | None) -> str | None:
        return state["pane"]

    monkeypatch.setattr(tui_app, "detect_focused_external_pane", fake_detect)

    app = EphorApp(manager=StateManager(sd))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        list_view = await _list_view(app, pilot)

        # The user "switches" to a Ghostty showing session beta.
        state["pane"] = "%20"
        await app._refresh_table()
        await pilot.pause()
        assert app._sid_by_row[list_view.index] == "beta-id"

        # Same pane on next refresh → user navigation must be respected.
        # Move the cursor manually and confirm refresh leaves it alone.
        list_view.index = (list_view.index + 1) % len(app._sid_by_row)
        manual_pos = list_view.index
        await app._refresh_table()
        await pilot.pause()
        assert list_view.index == manual_pos, (
            "auto-follow must not override user navigation when external pane is unchanged"
        )

        # User switches Ghostty windows again — now to gamma. Cursor moves.
        state["pane"] = "%30"
        await app._refresh_table()
        await pilot.pause()
        assert app._sid_by_row[list_view.index] == "gamma-id"


@pytest.mark.asyncio
async def test_auto_follow_ignores_panes_with_no_matching_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the user focuses a tmux pane that isn't hosting any ephor session
    (e.g. a plain shell), we should NOT clobber the cursor — and we should
    remember the pane so we don't re-evaluate it next tick."""
    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("EPHOR_STATE_DIR", str(sd))
    _write_state(sd, "alpha-id", project_name="alpha", tmux_pane="%10")
    _write_state(sd, "beta-id", project_name="beta", tmux_pane="%20")

    monkeypatch.setattr(tui_app, "detect_focused_external_pane", lambda _self_pane: "%999")

    app = EphorApp(manager=StateManager(sd))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        list_view = await _list_view(app, pilot)
        # User picks the second row manually.
        list_view.index = 1
        await app._refresh_table()
        await pilot.pause()
        assert list_view.index == 1, "follow target had no matching session, must not move cursor"
        # And the pane is now cached, so next refresh is a no-op too.
        assert app._last_external_pane == "%999"


@pytest.mark.asyncio
async def test_auto_follow_silent_when_tmux_query_fails(
    populated_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tmux subprocess failure inside the follow detector must not crash
    the refresh loop nor move the cursor."""

    def boom(_self_pane: str | None) -> str | None:
        raise RuntimeError("tmux subprocess crashed")

    monkeypatch.setattr(tui_app, "detect_focused_external_pane", boom)

    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        # If the exception leaked, we'd never have populated _sid_by_row.
        assert len(app._sid_by_row) == 3


@pytest.mark.asyncio
async def test_enter_after_cold_path_rebuild_jumps_to_correct_session(
    populated_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a cold-path rebuild + Enter keypress must jump to the
    sid that the cursor visually points at, not a stale or off-by-one one.
    """
    from ephor.tmux import navigator

    captured: list[AgentState] = []

    def fake_jump(agent: AgentState) -> Any:
        captured.append(agent)

        class _Outcome:
            ok = True
            result = navigator.JumpResult.OK
            detail = ""

        return _Outcome()

    monkeypatch.setattr(tui_app, "jump_to", fake_jump)

    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()

        # Force a cold-path rebuild by adding a session, then immediately
        # Enter. With the async fix the cursor sid must match _sid_by_row[idx].
        _write_state(populated_dir, "delta-id", project_name="delta")
        await app._refresh_table()
        await pilot.pause()

        idx = app.query_one("ListView").index  # type: ignore[attr-defined]
        assert idx is not None
        expected_sid = app._sid_by_row[idx]

        await pilot.press("enter")
        await pilot.pause()

        assert len(captured) >= 1
        assert captured[-1].session_id == expected_sid


@pytest.mark.asyncio
async def test_summarize_falls_back_to_last_reply_for_non_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-Claude session (no Claude transcript) summarizes its captured
    last_reply via summarize_text, not the empty transcript summarizer."""
    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("EPHOR_STATE_DIR", str(sd))
    _write_state(
        sd,
        "grok-sid",
        provider="grok",
        last_reply="A deadlock is mutual waiting.",
        last_summary="explain what a deadlock is",
    )
    monkeypatch.setattr("ephor.summary_store.summary_dir", lambda: tmp_path / "summaries")
    # No Claude transcript → transcript summarizer yields "".
    monkeypatch.setattr("ephor.tui.app.summarize_transcript", lambda _p, cwd=None: "")
    seen: dict[str, object] = {}

    def fake_text(text: str, cwd: object = None, *, prompt: str = "") -> str:
        seen["text"] = text
        seen["prompt"] = prompt
        return "condensed reply"

    monkeypatch.setattr("ephor.tui.app.summarize_text", fake_text)

    app = EphorApp(manager=StateManager(sd))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        from ephor.summary_store import SummaryStore

        app._summaries = SummaryStore()
        app.action_summarize()
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert seen["text"] == "A deadlock is mutual waiting."
    # The latest user prompt (last_summary) is passed through as task context.
    assert seen["prompt"] == "explain what a deadlock is"
    saved = (tmp_path / "summaries" / "grok-sid.json").read_text()
    assert "condensed reply" in saved


@pytest.mark.asyncio
async def test_summarize_agy_reads_brain_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agy session summarizes via summarize_agy (its brain transcript,
    keyed by session id) rather than the Claude-convention transcript path —
    agy hands the hook no transcript path and captures no last_reply."""
    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("EPHOR_STATE_DIR", str(sd))
    # No last_reply, no last_summary — exactly the empty-capture case agy hits.
    _write_state(sd, "agy-sid", provider="agy", last_reply="", last_summary="")
    monkeypatch.setattr("ephor.summary_store.summary_dir", lambda: tmp_path / "summaries")
    # If it wrongly took the Claude path this would win; it must not be called.
    monkeypatch.setattr(
        "ephor.tui.app.summarize_transcript",
        lambda _p, cwd=None: "WRONG-claude-path",
    )
    seen: dict[str, object] = {}

    def fake_agy(sid: str, cwd: object = None) -> str:
        seen["sid"] = sid
        return "Review GCP deployment config"

    monkeypatch.setattr("ephor.tui.app.summarize_agy", fake_agy)

    app = EphorApp(manager=StateManager(sd))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        from ephor.summary_store import SummaryStore

        app._summaries = SummaryStore()
        app.action_summarize()
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert seen["sid"] == "agy-sid"
    saved = (tmp_path / "summaries" / "agy-sid.json").read_text()
    assert "Review GCP deployment config" in saved
    assert "WRONG-claude-path" not in saved


@pytest.mark.asyncio
async def test_refresh_tick_survives_missing_list_view(populated_dir: Path) -> None:
    """A refresh tick landing while the ListView is absent must be a no-op.

    Textual lets an exception raised inside a timer callback escape and kill
    the app, so the unguarded `query_one(ListView)` in `_refresh_table` turned
    a normal teardown-window race into a hard failure:

        textual/timer.py:189 in _tick -> app.py in _refresh_table
        textual.css.query.NoMatches: No nodes match 'ListView'

    It only reproduced on the slowest CI runners, where the window is wide
    enough to lose. Removing the widget makes that window deterministic.
    """
    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        list_view = await _list_view(app, pilot)
        await list_view.remove()
        await pilot.pause()
        assert not len(app.query(type(list_view)))

        # The tick must return quietly rather than raise.
        await app._refresh_table()

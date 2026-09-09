"""Tests for the per-widget pieces of the TUI layout.

Covers what the existing test_tui.py doesn't: rendering of the HeaderBar
counter strip and the sparkline glyph mapping. Layout-level smoke tests
(app composes without crashing, every AgentStatus renders) live here too,
since they exercise the new widget tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ephor.constants import AgentStatus
from ephor.github import PullRequest
from ephor.state.manager import StateManager
from ephor.state.models import AgentState, StatusSummary
from ephor.tui.app import EphorApp
from ephor.tui.widgets.header_bar import format_header
from ephor.tui.widgets.session_row import (
    _SPARK_GLYPHS,
    _SPARK_WIDTH,
    _TICKET_COL_WIDTH,
    render_sparkline,
    render_ticket_cell,
)

# ---- render_sparkline -----------------------------------------------------


def test_sparkline_empty_returns_placeholder_of_fixed_width() -> None:
    out = render_sparkline([])
    assert len(out) == _SPARK_WIDTH
    # Placeholder must NOT use spark glyphs — that would be a lie about activity.
    assert not any(c in out for c in _SPARK_GLYPHS)


def test_sparkline_full_range_maps_to_extremes() -> None:
    out = render_sparkline([0.0, 1.0])
    assert out.endswith(_SPARK_GLYPHS[0] + _SPARK_GLYPHS[-1])


def test_sparkline_clamps_out_of_range_values() -> None:
    out = render_sparkline([-2.0, 5.0])
    assert out.endswith(_SPARK_GLYPHS[0] + _SPARK_GLYPHS[-1])


def test_sparkline_truncates_to_width() -> None:
    samples = [i / 32 for i in range(64)]
    out = render_sparkline(samples)
    assert len(out) == _SPARK_WIDTH


def test_sparkline_left_pads_short_input_to_width() -> None:
    out = render_sparkline([0.5, 0.5])
    assert len(out) == _SPARK_WIDTH


# ---- layout smoke ---------------------------------------------------------


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


@pytest.fixture
def all_statuses_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One state file per AgentStatus value."""
    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("EPHOR_STATE_DIR", str(sd))
    for i, status in enumerate(AgentStatus):
        _write_state(sd, f"sid-{i}", status=status, project_name=f"proj-{status.value}")
    return sd


@pytest.mark.asyncio
async def test_app_renders_every_status_without_crashing(all_statuses_dir: Path) -> None:
    """Mount the app against one row per AgentStatus; no exception means the
    SessionRow rich markup is well-formed for every color/symbol combination.

    DEAD sessions are hidden from the dashboard, so the rendered count is
    one less than the total number of statuses.
    """
    app = EphorApp(manager=StateManager(all_statuses_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        assert len(app._sid_by_row) == len(list(AgentStatus)) - 1
        dead_sid = next(f"sid-{i}" for i, s in enumerate(AgentStatus) if s is AgentStatus.DEAD)
        assert dead_sid not in app._sid_by_row


def test_header_bar_format_includes_every_label() -> None:
    summary = StatusSummary(
        working=1, idle=1, waiting_permission=1, waiting_answer=1, error=1, dead=1
    )
    rendered = format_header(summary)
    for label in ("PERM", "WAIT", "ERR", "WORK", "IDLE", "DEAD", "TOTAL"):
        assert label in rendered
    # TOTAL count reflects the sum.
    assert "TOTAL 6" in rendered


def test_header_bar_format_dims_zero_buckets() -> None:
    """Zero-count buckets render in [dim] so the eye skips them."""
    summary = StatusSummary(working=2)
    rendered = format_header(summary)
    assert "[dim]PERM 0[/]" in rendered
    assert "[bold #3fb950]WORK[/] [bold]2[/]" in rendered


def test_status_summary_from_agents_counts_buckets() -> None:
    agents = [
        AgentState(
            session_id=f"sid-{i}",
            cwd="/tmp/x",
            started_at="2026-04-29T10:00:00Z",
            status=status,
        )
        for i, status in enumerate(AgentStatus)
    ]
    summary = StatusSummary.from_agents(agents)
    assert summary.total == len(list(AgentStatus))
    assert summary.attention == 3  # PERM + WAIT + ERROR


def test_is_heartbeat_stale_flags_working_with_old_event() -> None:
    from datetime import UTC, datetime, timedelta

    from ephor.constants import AgentStatus
    from ephor.state.models import AgentState
    from ephor.tui.widgets.session_row import is_heartbeat_stale

    old = (datetime.now(UTC) - timedelta(seconds=600)).isoformat().replace("+00:00", "Z")
    fresh = (datetime.now(UTC) - timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
    base = dict(
        session_id="x",
        cwd="/tmp",
        started_at="2026-04-29T10:00:00Z",
        project_name="x",
    )
    stale_agent = AgentState(**base, status=AgentStatus.WORKING, last_event_time=old)
    fresh_agent = AgentState(**base, status=AgentStatus.WORKING, last_event_time=fresh)
    idle_agent = AgentState(**base, status=AgentStatus.IDLE, last_event_time=old)

    assert is_heartbeat_stale(stale_agent, threshold_sec=60)
    assert not is_heartbeat_stale(fresh_agent, threshold_sec=60)
    # IDLE never goes stale — no hook activity is normal.
    assert not is_heartbeat_stale(idle_agent, threshold_sec=60)


def test_is_heartbeat_stale_tolerates_garbage_timestamp() -> None:
    from ephor.constants import AgentStatus
    from ephor.state.models import AgentState
    from ephor.tui.widgets.session_row import is_heartbeat_stale

    a = AgentState(
        session_id="x",
        cwd="/tmp",
        started_at="2026-04-29T10:00:00Z",
        status=AgentStatus.WORKING,
        last_event_time="not-a-date",
    )
    assert not is_heartbeat_stale(a)


# ---- SessionRow column composition ---------------------------------------


def _agent_for_row(cwd: str = "/tmp/proj", *, sid: str = "abcdef1234") -> AgentState:
    return AgentState(
        session_id=sid,
        cwd=cwd,
        started_at="2026-01-01T00:00:00Z",
        status=AgentStatus.WORKING,
        project_name="proj",
        last_event="PreToolUse",
        last_event_time="2026-01-01T00:00:01Z",
        last_event_seq=1,
        tool_count=7,
        error_count=2,
    )


def test_session_row_drops_tool_and_error_columns(tmp_path: Path) -> None:
    """The T<n> and E<n> cells were removed in favor of the Jira column."""
    from ephor.tui.widgets.session_row import SessionRow

    row = SessionRow()
    agent = _agent_for_row(cwd=str(tmp_path))
    row.update_agent(agent, summary="doing things")
    rendered = str(row.render())
    # The literal "T7" or "E2" used to appear here. Their absence is the
    # contract — Jira column replaced them.
    assert "T7" not in rendered
    assert "E2" not in rendered


def test_session_row_renders_jira_ticket_when_present(tmp_path: Path) -> None:
    """The Jira cell replaces the session_id suffix when a key is inferable."""
    from ephor.tui.widgets.session_row import SessionRow

    worktree = tmp_path / "DR-4242"
    worktree.mkdir()
    row = SessionRow()
    agent = _agent_for_row(cwd=str(worktree))
    row.update_agent(agent, summary="ok")
    rendered = str(row.render())
    assert "DR-4242" in rendered
    # The old session_id suffix must no longer appear.
    assert "abcdef12" not in rendered


def test_session_row_uses_em_dash_when_no_ticket(tmp_path: Path) -> None:
    """A bare cwd with no Jira-shaped component renders a placeholder."""
    from ephor.tui.widgets.session_row import SessionRow

    row = SessionRow()
    agent = _agent_for_row(cwd=str(tmp_path))
    row.update_agent(agent, summary="x")
    rendered = str(row.render())
    # Em-dash placeholder is rendered when no ticket inference succeeds.
    assert "—" in rendered


def test_session_row_prefers_cwd_over_summary_prefix(tmp_path: Path) -> None:
    """The cwd-derived key wins over one parsed out of the LLM summary.

    The summarizer already glues the cwd key onto its own output, so a key
    that differs there is the model's reading of the transcript — the
    weakest of the signals, not the freshest.
    """
    from ephor.tui.widgets.session_row import SessionRow

    worktree = tmp_path / "DR-1111"
    worktree.mkdir()
    row = SessionRow()
    agent = _agent_for_row(cwd=str(worktree))
    row.update_agent(agent, summary="DR-2222: model's guess")
    rendered = str(row.render())
    assert "DR-1111" in rendered


def test_session_row_falls_back_to_summary_when_cwd_has_no_key(tmp_path: Path) -> None:
    """A shared checkout with no key in the path still gets a ticket."""
    from ephor.tui.widgets.session_row import SessionRow

    row = SessionRow()
    agent = _agent_for_row(cwd=str(tmp_path))
    row.update_agent(agent, summary="DR-2222: doing the work")
    rendered = str(row.render())
    assert "DR-2222" in rendered


def test_session_row_shows_provider_column(tmp_path: Path) -> None:
    """The provider (claude/grok/opencode/…) appears as its own cell."""
    from ephor.tui.widgets.session_row import SessionRow

    row = SessionRow()
    agent = _agent_for_row(cwd=str(tmp_path))
    agent.provider = "opencode"
    row.update_agent(agent, summary="x")
    rendered = str(row.render())
    assert "opencode" in rendered


def test_session_row_uses_color_not_icon_for_status(tmp_path: Path) -> None:
    """Status is conveyed by the project-name color, not a leading icon/label
    cell. render() resolves markup to plain text, so we assert the removal
    contract there; that the status color markup is well-formed is covered by
    test_app_renders_every_status_without_crashing."""
    from ephor.tui.widgets.session_row import SessionRow

    row = SessionRow()
    agent = _agent_for_row(cwd=str(tmp_path))  # WORKING
    agent.provider = "grok"
    row.update_agent(agent, summary="x")
    rendered = str(row.render())
    for gone in ("WORK", "IDLE", ">>>", "---", "[!]", "[?]"):
        assert gone not in rendered, gone
    assert "proj" in rendered  # project name still shown (tinted by status color)


def test_session_row_provider_placeholder_when_unknown(tmp_path: Path) -> None:
    from ephor.tui.widgets.session_row import SessionRow

    row = SessionRow()
    agent = _agent_for_row(cwd=str(tmp_path))  # provider defaults to ""
    row.update_agent(agent, summary="x")
    rendered = str(row.render())
    assert "—" in rendered


# ---- Jira cell / PR link --------------------------------------------------


def test_ticket_cell_without_pr_is_plain_but_clickable() -> None:
    cell = render_ticket_cell("DR-8222", "sid-1")
    assert "@click=app.open_pr('sid-1')" in cell
    assert "underline" not in cell
    assert "DR-8222" in cell


def test_ticket_cell_with_open_pr_is_underlined() -> None:
    pr = PullRequest(number=7, url="https://github.com/a/b/pull/7", state="OPEN")
    cell = render_ticket_cell("DR-8222", "sid-1", pr)
    assert "underline" in cell
    assert "@click=app.open_pr('sid-1')" in cell


def test_ticket_cell_colors_merged_and_closed_differently() -> None:
    merged = render_ticket_cell("DR-1", "sid", PullRequest(number=1, url="u", state="MERGED"))
    closed = render_ticket_cell("DR-1", "sid", PullRequest(number=1, url="u", state="CLOSED"))
    open_pr = render_ticket_cell("DR-1", "sid", PullRequest(number=1, url="u", state="OPEN"))
    assert merged != closed != open_pr


def test_ticket_cell_placeholder_is_not_clickable() -> None:
    cell = render_ticket_cell("—", "sid-1")
    assert "@click" not in cell


def test_ticket_cell_refuses_to_interpolate_an_unsafe_session_id() -> None:
    """A crafted state file must not be able to inject markup or an action."""
    cell = render_ticket_cell("DR-8222", "sid') app.quit(")
    assert "@click" not in cell
    assert "app.quit" not in cell


def test_ticket_cell_pads_to_a_stable_width() -> None:
    """Column alignment must not shift as PR status changes."""
    import re as _re

    def visible(markup: str) -> str:
        return _re.sub(r"\[[^\]]*\]", "", markup)

    pr = PullRequest(number=7, url="u", state="OPEN")
    widths = {
        len(visible(render_ticket_cell("DR-8222", "sid-1"))),
        len(visible(render_ticket_cell("DR-8222", "sid-1", pr))),
        len(visible(render_ticket_cell("DR-1", "sid-1", pr))),
        len(visible(render_ticket_cell("—", "sid-1"))),
    }
    assert widths == {_TICKET_COL_WIDTH}


def test_ticket_cell_markup_parses_into_a_click_span() -> None:
    """Textual must actually turn the cell into a click target.

    render_ticket_cell emits markup; if Textual's parser ever stops
    accepting the `@click` form the key would silently render as inert
    text, so assert on the parsed spans rather than the string.
    """
    from textual.content import Content

    content = Content.from_markup(render_ticket_cell("DR-8222", "sid-1"))
    click_spans = [
        span
        for span in content.spans
        if isinstance(span.style, str) and span.style.startswith("@click=")
    ]
    assert len(click_spans) == 1
    span = click_spans[0]
    assert span.style == "@click=app.open_pr('sid-1')"
    # The hit area is the key itself, not the alignment padding.
    assert content.plain[span.start : span.end] == "DR-8222"

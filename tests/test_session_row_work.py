"""Row rendering for work state: the PR cell, the subline, the mark."""

from __future__ import annotations

from ephor.constants import CiState, ReviewState
from ephor.github import PullRequest
from ephor.state.models import AgentState
from ephor.tui.widgets.session_row import (
    RowContext,
    render_subline,
    render_work_cell,
)


def _agent(**fields: object) -> AgentState:
    base = {"session_id": "s1", "cwd": "/tmp", "started_at": "2026-01-01T00:00:00+00:00"}
    return AgentState(**{**base, "provider": "claude", **fields})  # type: ignore[arg-type]


def test_no_pr_renders_a_stable_width_placeholder() -> None:
    cell = render_work_cell(None, width=12)
    assert "—" in cell
    assert "dim" in cell


def test_work_cell_shows_number_and_verdict_glyphs() -> None:
    pr = PullRequest(
        number=143,
        url="u",
        state="OPEN",
        ci=CiState.PASSING,
        review=ReviewState.CHANGES_REQUESTED,
    )
    cell = render_work_cell(pr)
    assert "#143" in cell
    assert "v" in cell  # CI passing
    assert "!" in cell  # changes requested


def test_failing_ci_is_red() -> None:
    pr = PullRequest(number=1, url="u", state="OPEN", ci=CiState.FAILING)
    assert "#f85149" in render_work_cell(pr)


def test_a_conflict_overrides_the_review_glyph() -> None:
    pr = PullRequest(
        number=1, url="u", state="OPEN", review=ReviewState.APPROVED, mergeable="CONFLICTING"
    )
    cell = render_work_cell(pr)
    assert ">" in cell
    assert "+" not in cell  # the approval no longer matters


def test_drift_adds_a_marker() -> None:
    pr = PullRequest(number=1, url="u", state="MERGED")
    assert "~" in render_work_cell(pr, drift="PR merged, ticket still In Progress")


def test_subline_shows_the_prompt_by_default() -> None:
    line = render_subline(_agent(last_summary="implement the retry"), RowContext())
    assert "implement the retry" in line
    assert "↳" in line


def test_a_collision_displaces_the_prompt() -> None:
    """News beats context you already have — you typed the prompt."""
    line = render_subline(
        _agent(last_summary="implement the retry"), RowContext(collisions=("s2", "s3"))
    )
    assert "shares its worktree with 2 other sessions" in line
    assert "implement the retry" not in line


def test_warning_precedence_is_collision_then_drift_then_ci() -> None:
    agent = _agent(last_summary="p")
    both = RowContext(collisions=("s2",), drift="d", failing_checks=("ci",))
    assert "shares its worktree" in render_subline(agent, both)
    assert "d" in render_subline(agent, RowContext(drift="d", failing_checks=("ci",)))
    assert "CI failing" in render_subline(agent, RowContext(failing_checks=("ci",)))


def test_failing_check_names_are_listed_and_capped() -> None:
    """Three names is enough to act on; the rest would just wrap off-screen."""
    line = render_subline(_agent(), RowContext(failing_checks=("unit", "lint", "e2e", "typecheck")))
    assert "unit, lint, e2e" in line
    assert "typecheck" not in line


def test_jira_status_tags_the_prompt() -> None:
    line = render_subline(_agent(last_summary="p"), RowContext(jira_status="In Review"))
    assert "«In Review»" in line


def test_jira_title_fills_in_when_there_is_no_prompt() -> None:
    line = render_subline(_agent(), RowContext(jira_title="Add retry to upload client"))
    assert "Add retry to upload client" in line


def test_a_queued_decision_is_announced() -> None:
    line = render_subline(_agent(), RowContext(decision_queued="allow"))
    assert "allow queued" in line


def test_selection_marker_appears_on_the_primary_row() -> None:
    from ephor.tui.widgets.session_row import SessionRow

    row = SessionRow()
    row.update_agent(_agent(project_name="ephor"), context=RowContext(selected=True))
    assert "•" in str(row.render())

    row.update_agent(_agent(project_name="ephor"), context=RowContext(selected=False))
    assert "•" not in str(row.render())


def test_rendering_without_a_context_still_works() -> None:
    """Every existing caller passes no context; that path must stay intact."""
    from ephor.tui.widgets.session_row import SessionRow

    row = SessionRow()
    row.update_agent(_agent(project_name="ephor", last_summary="doing a thing"))
    assert "doing a thing" in str(row.render())

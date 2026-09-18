"""2-row session card.

Row 1: provider / project (colored by status) / summary / tokens / sparkline / Jira
Row 2: dim italic conversation summary (last_summary, truncated)

Status is conveyed by the *color* of the project name (green = working,
grey = idle, red = waiting on permission, etc. — see STATUS_DISPLAY) rather
than a leading icon/label cell.

Sparkline data comes from `activity_samples`; if empty, render a dim placeholder
so the column doesn't shift width when samples land later.

The Jira ticket cell replaces the older session_id tail. It is clickable:
when the session's PR is known the key dispatches ``app.open_pr(<sid>)``,
which opens the pull request in the browser (the ``o`` hotkey does the same
for the selected row). Underlined + bright = PR resolved; plain green =
ticket known but no PR found yet; amber = the key was only *mentioned* in a
tmux label, a prompt or a model summary rather than established by git, so
it is a guess and is drawn as one; dim dash = no ticket. T/E (tool count +
error count) cells were removed once the LLM summary made them redundant —
errors still surface through the status icon and the STALE badge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from textual.widgets import Static

from ephor.constants import (
    STALE_HEARTBEAT_SEC,
    STATUS_DISPLAY,
    AgentStatus,
    CiState,
    ReviewState,
)
from ephor.github import PullRequest
from ephor.jira import match_for_agent
from ephor.state.models import AgentState

_SPARK_GLYPHS = "▁▂▃▄▅▆▇█"
_SPARK_WIDTH = 16
_SUMMARY_COL_WIDTH = 40  # LLM summary column on the primary row
_SUBLINE_WIDTH = 70  # latest user prompt on the dim subline
_TICKET_COL_WIDTH = 12  # Jira key cell on the tail of row 1
_WORK_COL_WIDTH = 12  # PR number + CI/review glyphs, right of the Jira key


def is_heartbeat_stale(agent: AgentState, threshold_sec: int = STALE_HEARTBEAT_SEC) -> bool:
    """True iff the session is WORKING but its last hook event is too old.

    Used to flag sessions where agent_pid is still alive but the process
    is hung on a model timeout or network stall — the PID liveness check
    can't tell those from healthy ones, but the hook-event clock can.
    """
    if agent.status != AgentStatus.WORKING:
        return False
    if not agent.last_event_time:
        return False
    try:
        ts = datetime.fromisoformat(agent.last_event_time.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(UTC) - ts).total_seconds() > threshold_sec


def render_sparkline(samples: list[float]) -> str:
    """Map a list of [0, 1] samples to ▁▂▃▄▅▆▇█.

    Out-of-range values are clamped. An empty list returns a placeholder
    of the same visual width so layout doesn't reflow.
    """
    if not samples:
        return "·" * _SPARK_WIDTH
    n = len(_SPARK_GLYPHS) - 1
    out: list[str] = []
    for s in samples[-_SPARK_WIDTH:]:
        clamped = 0.0 if s < 0 else (1.0 if s > 1 else s)
        out.append(_SPARK_GLYPHS[round(clamped * n)])
    # Left-pad with the lowest glyph so width is stable when fewer samples exist.
    if len(out) < _SPARK_WIDTH:
        out = [_SPARK_GLYPHS[0]] * (_SPARK_WIDTH - len(out)) + out
    return "".join(out)


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


# Session ids come off disk, and the id is interpolated into a Textual
# markup action. Only render a click handler for ids that can't break out
# of the quoted argument.
_SAFE_SID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _ticket_label(
    agent: AgentState, summary: str | None, pr: PullRequest | None = None
) -> tuple[str, bool]:
    """Jira key for the row plus whether it is a fact, not a guess.

    See :func:`ephor.jira.match_for_agent` for the probe order. The bool is
    the match's confidence; the placeholder is reported as confident so the
    dash renders with its own dim style rather than the guess style.
    """
    match = match_for_agent(agent, summary, pr)
    if match is None:
        return "—", True
    return match.key, match.confident


def render_ticket_cell(
    ticket: str,
    session_id: str = "",
    pr: PullRequest | None = None,
    width: int = _TICKET_COL_WIDTH,
    confident: bool = True,
) -> str:
    """Markup for the Jira cell — a click target when a PR is known.

    A resolved PR renders the key underlined and colored by PR state
    (green open, purple merged, red closed) and wraps it in a Textual
    ``@click`` action so a mouse click opens the pull request. Without a
    PR the key still clicks through — the action resolves lazily and
    toasts if there's nothing to open — but stays unadorned so the
    dashboard shows at a glance which sessions have review in flight.

    ``confident=False`` (the key was read out of a label, a prompt or a
    model summary rather than out of git) draws it amber and italic. The
    column used to present a lucky regex hit on prose exactly like a branch
    name; this is the difference between the two being visible instead of
    silently asserted.
    """
    if ticket == "—":
        return f"[dim]{ticket:<{width}}[/]"
    if pr is not None:
        state = pr.state.upper()
        if state == "MERGED":
            color = "#bc8cff"
        elif state == "CLOSED":
            color = "#f85149"
        else:
            color = "#7ee787"
        style = f"{color} underline"
    elif confident:
        style = "#7ee787"
    else:
        style = "#d29922 italic"
    padded = f"{ticket:<{width}}"
    if not _SAFE_SID.fullmatch(session_id):
        return f"[{style}]{padded}[/]"
    # Pad outside the click span so the hit area is the key itself, not
    # the trailing alignment whitespace.
    return f"[@click=app.open_pr('{session_id}')][{style}]{ticket}[/][/]{padded[len(ticket) :]}"


@dataclass(frozen=True)
class RowContext:
    """Everything about a row that does not come from its ``AgentState``.

    These arrive from four different resolvers on four different clocks (the
    PR cache, the Jira cache, the collision scan, the selection set), and
    passing them as one object keeps :meth:`SessionRow.update_agent` from
    growing a tenth positional parameter every time the dashboard learns to
    notice something new.
    """

    selected: bool = False
    collisions: tuple[str, ...] = ()
    jira_status: str = ""
    jira_title: str = ""
    drift: str = ""
    decision_queued: str = ""
    failing_checks: tuple[str, ...] = field(default=())


_EMPTY_CONTEXT = RowContext()

# Glyphs for the work cell. Deliberately ASCII-adjacent: this sits at the
# right edge of a row that already spends its Unicode budget on the
# sparkline, and a box-drawing failure there would misalign every column.
_CI_GLYPH: dict[CiState, tuple[str, str]] = {
    CiState.PASSING: ("v", "#7ee787"),
    CiState.FAILING: ("x", "#f85149"),
    CiState.PENDING: ("~", "#d29922"),
    CiState.NONE: (" ", "dim"),
}
_REVIEW_GLYPH: dict[ReviewState, tuple[str, str]] = {
    ReviewState.APPROVED: ("+", "#7ee787"),
    ReviewState.CHANGES_REQUESTED: ("!", "#f85149"),
    ReviewState.REVIEW_REQUIRED: ("?", "#d29922"),
    ReviewState.NONE: (" ", "dim"),
}


def render_work_cell(
    pr: PullRequest | None,
    drift: str = "",
    width: int = _WORK_COL_WIDTH,
) -> str:
    """Markup for the work-state cell: PR number, CI verdict, review verdict.

    This is the cell that answers "is this finished?" — the question the
    status column cannot answer, because the coding agent going idle says
    nothing about whether its work is mergeable. A blank cell means no PR,
    which for an in-flight session is information too.
    """
    if pr is None:
        return f"[dim]{'—':<{width}}[/]"
    number = f"#{pr.number}"
    ci_char, ci_color = _CI_GLYPH.get(pr.ci, (" ", "dim"))
    review_char, review_color = _REVIEW_GLYPH.get(pr.review, (" ", "dim"))
    # A conflicting branch overrides the review glyph: no verdict matters
    # while the thing cannot merge.
    if pr.has_conflict:
        review_char, review_color = ("><"[0], "#f85149")
    drift_char = "[#bc8cff]~[/]" if drift else " "
    body = f"{number} [{ci_color}]{ci_char}[/][{review_color}]{review_char}[/]{drift_char}"
    # Pad against the *visible* width; the markup tags are zero-width.
    visible = len(number) + 3
    return (
        f"[dim]{body}{' ' * max(0, width - visible)}[/]"
        if not pr.is_open
        else (f"{body}{' ' * max(0, width - visible)}")
    )


def render_subline(
    agent: AgentState,
    context: RowContext,
    width: int = _SUBLINE_WIDTH,
) -> str:
    """The dim second line: a warning if there is one, else what was asked.

    Warnings displace the prompt rather than sharing the line with it. The
    prompt is context you already have — you typed it — whereas "two sessions
    are editing this worktree" is news, and news that scrolls off the right
    edge is news nobody reads.
    """
    if context.collisions:
        others = len(context.collisions)
        plural = "s" if others != 1 else ""
        return (
            f"  [bold #f85149]! shares its worktree with {others} other session{plural}[/]"
            f"[dim] — edits will interleave[/]"
        )
    if context.drift:
        return f"  [#bc8cff]~ {_truncate(context.drift, width)}[/]"
    if context.failing_checks:
        names = ", ".join(context.failing_checks[:3])
        return f"  [#f85149]x CI failing:[/][dim] {_truncate(names, width)}[/]"
    if context.decision_queued:
        return f"  [#7ee787]> {context.decision_queued} queued — sent on the next prompt[/]"

    prompt = getattr(agent, "last_summary", "") or ""
    if context.jira_status and prompt:
        tag = f"[dim italic]«{context.jira_status}»[/] "
        return f"  {tag}[dim italic]{_truncate(prompt, width - len(context.jira_status) - 4)}[/]"
    if context.jira_title and not prompt:
        return f"  [dim italic]↳ {_truncate(context.jira_title, width)}[/]"
    if prompt:
        return f"  [dim italic]↳ {_truncate(prompt, width)}[/]"
    return "  [dim]·[/]"


class SessionRow(Static):
    """Renders one AgentState as a 2-row card. Stateless; rebuilds on update."""

    def update_agent(
        self,
        agent: AgentState,
        samples: list[float] | None = None,
        summary: str | None = None,
        tokens: int | None = None,
        speaking: bool = False,
        pr: PullRequest | None = None,
        context: RowContext | None = None,
    ) -> None:
        # Status is shown purely by color now (no icon/label cell); the color
        # tints the project name below.
        color = STATUS_DISPLAY[agent.status][2]
        spark = render_sparkline(samples or [])
        # WORKING + no recent hook = process is alive but stalled (model
        # timeout, network hang). Render a STALE marker that takes the place
        # of the (now-removed) error column when present.
        stale = is_heartbeat_stale(agent)
        stale_badge = "[bold #f85149]STALE[/]" if stale else "     "
        # Per-session token count from this session's transcript (lazy import
        # avoids a circular dep with tui.tokens). Right-justified to keep
        # downstream columns stable.
        from ephor.tui.tokens import format_tokens

        tok_cell = f"[dim]{format_tokens(tokens):>6}[/]" if tokens else "[dim]     —[/]"
        # Summary column: LLM-generated description of current activity, or
        # "—" placeholder so column width stays stable.
        if summary:
            summary_cell = (
                f"[#dbe4e3]{_truncate(summary, _SUMMARY_COL_WIDTH):<{_SUMMARY_COL_WIDTH}}[/]"
            )
        else:
            summary_cell = f"[dim]{'—':<{_SUMMARY_COL_WIDTH}}[/]"

        # Jira cell: takes the slot the session_id occupied. When no
        # ticket can be inferred we render a dim em-dash so column width
        # stays stable across sessions. Clicking it opens the PR.
        ticket, ticket_confident = _ticket_label(agent, summary, pr)
        ticket_cell = render_ticket_cell(ticket, agent.session_id, pr, confident=ticket_confident)

        ctx = context or _EMPTY_CONTEXT
        work_cell = render_work_cell(pr, ctx.drift)

        # Speaking marker: a 1-char left accent (▌) in cyan when this row
        # is the TTS source. A leading space when silent — ALWAYS 2 chars
        # of prefix so column alignment never shifts as the speaker
        # changes. Subtler than an emoji and matches the native TUI feel.
        speak_prefix = "[bold #00ffff]▌[/] " if speaking else "  "
        # Batch-selection marker, one char ahead of the speaking accent, so
        # a marked row reads at a glance without the two signals colliding.
        select_prefix = "[bold #58a6ff]•[/]" if ctx.selected else " "

        # Provider column: which coding agent this session belongs to. Muted so
        # the status color (on the project name) stays the primary signal.
        provider_cell = f"[#8b949e]{(agent.provider or '—'):<8}[/]"

        primary = (
            f"{select_prefix}{speak_prefix}"
            f"{provider_cell} "
            f"[bold {color}]{agent.project_name or '—':<20}[/] "
            f"{summary_cell} "
            f"{stale_badge}  "
            f"{tok_cell}  "
            f"[#00ffff]{spark}[/]  "
            f"{ticket_cell}"
            f"{work_cell}"
        )

        self.update(f"{primary}\n{render_subline(agent, ctx)}")

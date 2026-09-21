"""One session, one line — laid out to whatever width the pane actually has.

The row used to be a two-line card ~131 columns wide. Tiled two terminals to
a 27" screen and it wrapped, which costs twice: the wrap itself, and the
second line each session already spent on its prompt. Twenty sessions became
forty lines of mush.

So the row is one line, and its columns are **budgeted against the measured
width** rather than fixed. :func:`plan_row` spends the available columns in
priority order — the cells that answer *which session is this* and *is it
finished* are bought first, and the decorative ones are bought last and
dropped first. At 80 columns you still get project, detail, ticket, PR, CI
and review; the sparkline and the provider cell are simply not drawn.

Two consequences worth naming:

*The subline's news had to go somewhere.* A worktree collision or a red CI
run was the whole reason the second line existed, and losing it would be a
regression dressed up as a cleanup. :func:`render_detail` keeps that
precedence — a warning **displaces** the prompt on the one line available,
because news you cannot see is not news. The prompt is context you already
have; you typed it.

*Width is discovered, not declared.* The widget re-plans on resize, so
dragging a pane narrower re-lays the row rather than clipping it. Before the
first layout there is no measured width, so :data:`DEFAULT_WIDTH` stands in.

Status is conveyed by the *color* of the project name (green = working, grey
= idle, red = waiting on permission — see STATUS_DISPLAY) rather than a
leading icon cell, which is how the row affords a status signal for free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from textual import events
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
_SPARK_WIDTH = 16  # activity.py sizes its ring buffer to match this

# Width assumed before the widget has been laid out (and in unit tests, which
# render without mounting). Chosen so the default render exercises every
# column rather than a degraded subset.
DEFAULT_WIDTH = 120

_PREFIX_W = 3  # batch-selection dot + speaking accent
_TICKET_W = 10
_WORK_W = 10
_TOKENS_W = 6
_PROVIDER_W = 8
_MIN_PROJECT, _MAX_PROJECT = 12, 18
_MIN_DETAIL = 14
_DETAIL_GROWTH = 18  # how much detail gets before the decorative cells bid
_SPARK_MIN, _SPARK_MAX = 8, _SPARK_WIDTH

# The floor layout — every essential cell at its minimum, with the two
# separators that config actually needs (ticket and work sit flush). Narrower
# than this and there is nothing useful left to draw, so the planner clamps
# here rather than producing negative cells.
MIN_WIDTH = _PREFIX_W + _MIN_PROJECT + _MIN_DETAIL + _TICKET_W + _WORK_W + 2


@dataclass(frozen=True)
class RowLayout:
    """Column widths for one row at one width. ``0`` means "not drawn"."""

    total: int
    project: int
    detail: int
    ticket: int = _TICKET_W
    work: int = _WORK_W
    provider: int = 0
    tokens: int = 0
    spark: int = 0

    @property
    def visible_width(self) -> int:
        """Columns this layout actually paints, separators included."""
        cells = [self.project, self.detail, self.ticket]
        for optional in (self.provider, self.tokens, self.spark):
            if optional:
                cells.append(optional)
        # Every cell but the first is preceded by one space; work sits flush
        # against ticket, mirroring how the two read as one "state" column.
        return _PREFIX_W + sum(cells) + len(cells) - 1 + self.work


def plan_row(total: int) -> RowLayout:
    """Spend ``total`` columns across the row, most valuable cell first.

    The order below *is* the design: identity and outcome before decoration.
    Project name and detail text are what let you tell two rows apart;
    ticket and work answer whether the thing is done. Tokens, the provider
    and the sparkline are context, and go only if there is room.
    """
    width = max(MIN_WIDTH, total)
    project, detail = _MIN_PROJECT, _MIN_DETAIL
    provider = tokens = spark = 0
    # Each optional cell pays for its own separator as it is bought, so the
    # floor carries only the two the essential config needs.
    surplus = width - MIN_WIDTH

    def afford(cost: int) -> bool:
        nonlocal surplus
        if surplus >= cost:
            surplus -= cost
            return True
        return False

    if afford(_TOKENS_W + 1):
        tokens = _TOKENS_W
    grow = min(_MAX_PROJECT - _MIN_PROJECT, max(0, surplus))
    project += grow
    surplus -= grow
    if afford(_PROVIDER_W + 1):
        provider = _PROVIDER_W
    grow = min(_DETAIL_GROWTH, max(0, surplus))
    detail += grow
    surplus -= grow
    if afford(_SPARK_MIN + 1):
        spark = _SPARK_MIN
        spark += min(_SPARK_MAX - _SPARK_MIN, max(0, surplus))
        surplus -= spark - _SPARK_MIN
    # Whatever is left belongs to the detail text: it is the only cell whose
    # usefulness keeps rising with width, and letting it absorb the remainder
    # means the row fills the pane instead of trailing whitespace.
    detail += max(0, surplus)
    return RowLayout(
        total=width,
        project=project,
        detail=detail,
        provider=provider,
        tokens=tokens,
        spark=spark,
    )


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


def render_sparkline(samples: list[float], width: int = _SPARK_WIDTH) -> str:
    """Map a list of [0, 1] samples to ▁▂▃▄▅▆▇█.

    Out-of-range values are clamped. An empty list returns a placeholder
    of the same visual width so layout doesn't reflow.
    """
    if width <= 0:
        return ""
    if not samples:
        return "·" * width
    n = len(_SPARK_GLYPHS) - 1
    out: list[str] = []
    for s in samples[-width:]:
        clamped = 0.0 if s < 0 else (1.0 if s > 1 else s)
        out.append(_SPARK_GLYPHS[round(clamped * n)])
    # Left-pad with the lowest glyph so width is stable when fewer samples exist.
    if len(out) < width:
        out = [_SPARK_GLYPHS[0]] * (width - len(out)) + out
    return "".join(out)


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _cell(text: str, width: int, style: str = "") -> str:
    """One padded cell of exactly ``width`` *visible* columns."""
    body = f"{_truncate(text, width):<{width}}"
    return f"[{style}]{body}[/]" if style else body


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
    width: int = _TICKET_W,
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
    shown = _truncate(ticket, width)
    padded = f"{shown:<{width}}"
    if not _SAFE_SID.fullmatch(session_id):
        return f"[{style}]{padded}[/]"
    # Pad outside the click span so the hit area is the key itself, not
    # the trailing alignment whitespace.
    return f"[@click=app.open_pr('{session_id}')][{style}]{shown}[/][/]{padded[len(shown) :]}"


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
    width: int = _WORK_W,
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
    # Pad against the *visible* width; the markup tags are zero-width. The
    # body paints five things — number, space, CI glyph, review glyph, drift
    # glyph — so the count is len(number) + 4. It read +3 while the row was
    # two lines wide enough to wrap anyway, which hid the overflow.
    visible = len(number) + 4
    return (
        f"[dim]{body}{' ' * max(0, width - visible)}[/]"
        if not pr.is_open
        else (f"{body}{' ' * max(0, width - visible)}")
    )


def render_detail(
    agent: AgentState,
    context: RowContext,
    width: int = 40,
    stale: bool = False,
) -> str:
    """The flexible middle cell: a warning if there is one, else what was asked.

    Precedence is the point. Warnings **displace** the prompt rather than
    sharing the cell with it, because the prompt is context you already have
    — you typed it — whereas "two sessions are editing this worktree" is
    news, and news that scrolls off the right edge is news nobody reads.

    Ordered by how much it costs to not notice: a shared worktree corrupts
    work silently, a stalled heartbeat means the session is dead but looks
    alive, and red CI means the thing you think is finished is not.
    """
    if context.collisions:
        others = len(context.collisions)
        plural = "s" if others != 1 else ""
        return _cell(f"! shares worktree with {others} other{plural}", width, "bold #f85149")
    if stale:
        return _cell("! STALE — no hook events, session may be hung", width, "bold #f85149")
    if context.failing_checks:
        names = ", ".join(context.failing_checks[:3])
        return _cell(f"x CI failing: {names}", width, "#f85149")
    if context.drift:
        return _cell(f"~ {context.drift}", width, "#bc8cff")
    if context.decision_queued:
        return _cell(f"> {context.decision_queued} queued — sent next prompt", width, "#7ee787")

    prompt = getattr(agent, "last_summary", "") or ""
    if context.jira_status and prompt:
        return _cell(f"«{context.jira_status}» {prompt}", width, "dim italic")
    if prompt:
        return _cell(prompt, width, "#dbe4e3")
    if context.jira_title:
        return _cell(f"↳ {context.jira_title}", width, "dim italic")
    return _cell("—", width, "dim")


@dataclass(frozen=True)
class _RowInputs:
    """The last thing this row was told, kept so a resize can re-render it."""

    agent: AgentState
    samples: tuple[float, ...] = ()
    summary: str | None = None
    tokens: int | None = None
    speaking: bool = False
    pr: PullRequest | None = None
    context: RowContext = _EMPTY_CONTEXT


class SessionRow(Static):
    """Renders one AgentState as a single line. Stateless; rebuilds on update."""

    def __init__(self) -> None:
        super().__init__()
        self._inputs: _RowInputs | None = None

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
        self._inputs = _RowInputs(
            agent=agent,
            samples=tuple(samples or ()),
            summary=summary,
            tokens=tokens,
            speaking=speaking,
            pr=pr,
            context=context or _EMPTY_CONTEXT,
        )
        self._repaint()

    def on_resize(self, event: events.Resize) -> None:
        """Re-plan the columns for the new width rather than clipping them."""
        if self._inputs is not None:
            self._repaint()

    def _measured_width(self) -> int:
        """The pane's width, or :data:`DEFAULT_WIDTH` before first layout."""
        try:
            width = int(self.size.width)
        except (AttributeError, TypeError, ValueError):
            return DEFAULT_WIDTH
        return width if width >= MIN_WIDTH else DEFAULT_WIDTH

    def _repaint(self) -> None:
        row = self._inputs
        if row is None:
            return
        agent = row.agent
        ctx = row.context
        layout = plan_row(self._measured_width())

        # Status is shown purely by color (no icon cell); it tints the project.
        color = STATUS_DISPLAY[agent.status][2]
        stale = is_heartbeat_stale(agent)

        # Speaking marker: a 1-char left accent (▌) in cyan when this row is
        # the TTS source, a space when silent — ALWAYS 2 chars of prefix so
        # alignment never shifts as the speaker changes.
        speak_prefix = "[bold #00ffff]▌[/] " if row.speaking else "  "
        # Batch-selection marker, one char ahead of the speaking accent, so a
        # marked row reads at a glance without the two signals colliding.
        select_prefix = "[bold #58a6ff]•[/]" if ctx.selected else " "

        parts = [f"{select_prefix}{speak_prefix}"]
        if layout.provider:
            parts.append(_cell(agent.provider or "—", layout.provider, "#8b949e") + " ")
        parts.append(_cell(agent.project_name or "—", layout.project, f"bold {color}") + " ")
        parts.append(render_detail(agent, ctx, layout.detail, stale) + " ")
        if layout.tokens:
            from ephor.tui.tokens import format_tokens

            shown = format_tokens(row.tokens) if row.tokens else "—"
            parts.append(f"[dim]{shown:>{layout.tokens}}[/] ")
        if layout.spark:
            parts.append(f"[#00ffff]{render_sparkline(list(row.samples), layout.spark)}[/] ")

        ticket, confident = _ticket_label(agent, row.summary, row.pr)
        parts.append(
            render_ticket_cell(
                ticket, agent.session_id, row.pr, width=layout.ticket, confident=confident
            )
        )
        parts.append(render_work_cell(row.pr, ctx.drift, width=layout.work))
        self.update("".join(parts))

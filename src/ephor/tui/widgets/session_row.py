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
ticket known but no PR found yet; dim dash = no ticket. T/E (tool count +
error count) cells were removed once the LLM summary made them redundant —
errors still surface through the status icon and the STALE badge.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from textual.widgets import Static

from ephor.constants import (
    STALE_HEARTBEAT_SEC,
    STATUS_DISPLAY,
    AgentStatus,
)
from ephor.github import PullRequest
from ephor.jira import ticket_for_agent
from ephor.state.models import AgentState

_SPARK_GLYPHS = "▁▂▃▄▅▆▇█"
_SPARK_WIDTH = 16
_SUMMARY_COL_WIDTH = 40  # LLM summary column on the primary row
_SUBLINE_WIDTH = 70  # latest user prompt on the dim subline
_TICKET_COL_WIDTH = 12  # Jira key cell on the tail of row 1


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


def _ticket_label(agent: AgentState, summary: str | None) -> str:
    """Best-effort Jira key for the row. Falls back to "—" placeholder.

    See :func:`ephor.jira.ticket_for_agent` for the probe order.
    """
    return ticket_for_agent(agent, summary) or "—"


def render_ticket_cell(
    ticket: str,
    session_id: str = "",
    pr: PullRequest | None = None,
    width: int = _TICKET_COL_WIDTH,
) -> str:
    """Markup for the Jira cell — a click target when a PR is known.

    A resolved PR renders the key underlined and colored by PR state
    (green open, purple merged, red closed) and wraps it in a Textual
    ``@click`` action so a mouse click opens the pull request. Without a
    PR the key still clicks through — the action resolves lazily and
    toasts if there's nothing to open — but stays unadorned so the
    dashboard shows at a glance which sessions have review in flight.
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
    else:
        style = "#7ee787"
    padded = f"{ticket:<{width}}"
    if not _SAFE_SID.fullmatch(session_id):
        return f"[{style}]{padded}[/]"
    # Pad outside the click span so the hit area is the key itself, not
    # the trailing alignment whitespace.
    return f"[@click=app.open_pr('{session_id}')][{style}]{ticket}[/][/]{padded[len(ticket) :]}"


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
        ticket = _ticket_label(agent, summary)
        ticket_cell = render_ticket_cell(ticket, agent.session_id, pr)

        # Speaking marker: a 1-char left accent (▌) in cyan when this row
        # is the TTS source. A leading space when silent — ALWAYS 2 chars
        # of prefix so column alignment never shifts as the speaker
        # changes. Subtler than an emoji and matches the native TUI feel.
        speak_prefix = "[bold #00ffff]▌[/] " if speaking else "  "

        # Provider column: which coding agent this session belongs to. Muted so
        # the status color (on the project name) stays the primary signal.
        provider_cell = f"[#8b949e]{(agent.provider or '—'):<8}[/]"

        primary = (
            f"{speak_prefix}"
            f"{provider_cell} "
            f"[bold {color}]{agent.project_name or '—':<20}[/] "
            f"{summary_cell} "
            f"{stale_badge}  "
            f"{tok_cell}  "
            f"[#00ffff]{spark}[/]  "
            f"{ticket_cell}"
        )

        last_prompt = getattr(agent, "last_summary", "") or ""
        if last_prompt:
            subline = f"  [dim italic]↳ {_truncate(last_prompt, _SUBLINE_WIDTH)}[/]"
        else:
            subline = "  [dim]·[/]"

        self.update(f"{primary}\n{subline}")

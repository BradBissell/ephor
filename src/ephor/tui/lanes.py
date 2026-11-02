"""Status lanes: the session list grouped by where each ticket sits in Jira.

The other groupings answer "what is running". This one answers the question
you actually open Jira for — *where is everything?* — without leaving the
dashboard: one lane per column of your board (``board.toml``), in column
order, then ``Other`` for statuses no column claims, then ``Unticketed`` for
sessions bound to no ticket at all.

Two kinds of row live in a lane:

* **sessions**, placed by their ticket's status. The status comes from the
  freshest thing that knows it: the board snapshot when the last search saw
  the ticket, else the dashboard's own per-ticket Jira lookup, else the work
  record, else whatever the snapshot last knew.
* **ticket rows** for tickets on the board that have no session. These are
  *virtual*: they are not sessions and every session action refuses them
  (see :mod:`ephor.tui.rows`). They exist so a lane shows the whole column,
  not just the part of it an agent happens to be working on.

A status no column names is placed by its Jira category: a done-category
status folds under the last column, a to-do one under the first, and only
the rest land in ``Other`` (see :meth:`ephor.board.BoardSettings.column_for`).

Lanes that hold finished work (a ``Done`` column, one whose tickets are all
in Jira's done category, or an ``Other`` holding only done tickets and no
session) start folded, so a week of closed tickets does not push the live
work off the screen. A lane folded or unfolded by hand stays that way; only
a lane seen for the first time gets the default.

Both kinds of row carry a **flow badge** when their ticket has a run in
flight; see :func:`ephor.flows.runs.badge` and ``runs.BADGE_LEGEND``:

* ``⏸ review?`` — awaiting your confirmation (``f``, then ``y`` / ``N N``);
* ``… review`` — approved, dispatching on the next tick;
* ``⧗ review`` — queued for a free agent slot (``max_active_agents``);
* ``⊘ review`` — blocked by a guard, retried while its event holds;
* ``▶ review`` — launched, its window is running;
* ``⚠ review stuck`` — the tick gave up (``ephor flows retry``).

A ticket row also says how long the ticket has sat in its status (``~``
when the start is Jira's estimate rather than a status change this board
saw), its PR and CI from the snapshot, and ``⚑`` when it is flagged.

Nothing here does I/O on the render path. :func:`gather` reads board.toml,
runs the flows tick (which refreshes the snapshot), and reads the runs and
the work records on a worker thread; the app hands the result to
:meth:`Lanes.apply` on the event loop.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from rich.cells import cell_len
from textual import events
from textual.widgets import Static

from ephor import board, jira_api, work_items
from ephor.board import OTHER_LANE, UNTICKETED_LANE, BoardConfig, BoardSettings, Column
from ephor.constants import ATTENTION_STATUSES, AgentStatus
from ephor.flows import printable, runs, ticket_sort_key
from ephor.flows import snapshot as board_snapshot
from ephor.flows import tick as flows_tick
from ephor.flows.runs import Run, RunState
from ephor.flows.snapshot import PrState, RefreshResult, RefreshStatus, Snapshot, TicketState
from ephor.state.models import AgentState
from ephor.tui.rows import group_row_id, virtual_row_id
from ephor.tui.text import markup_text
from ephor.tui.widgets.session_row import (
    DEFAULT_WIDTH,
    MIN_WIDTH,
    plan_row,
    render_snapshot_pr_cell,
)

# The tick's cadence, from the TUI. Tickets move when a human drags a card,
# and one search covers the whole board, so this is one request a minute.
SNAPSHOT_REFRESH_INTERVAL = 60.0
# A snapshot older than this many tick intervals is reported as stale.
_STALE_INTERVALS = 2.0
# How old a snapshot may get before the board line warns, when there is no
# board.toml to read ``[flows] max_event_age`` from.
DEFAULT_STALE_SEC = 30 * 60.0

# Colours for an age past its column's limits (and for a stale board line).
_WARN = "#EBCB8B"
_ALERT = "#f85149"

__all__ = ["LaneCounts", "LaneData", "Lanes", "TicketRow", "TicketRowData", "markup_text"]


@dataclass(frozen=True)
class LaneData:
    """Everything the lanes read from disk or Jira, gathered off the event loop."""

    config: BoardConfig | None = None
    snapshot: Snapshot | None = None
    # ticket -> Jira status from the work records (the per-ticket lookup's
    # answer, persisted). A fallback for sessions on tickets the board
    # search does not cover.
    work_status: Mapping[str, str] = field(default_factory=dict)
    result: RefreshResult | None = None
    # Every flow run on disk, newest first — for badges, the header count
    # and the `f` screen.
    runs: tuple[Run, ...] = ()
    # Which Jira credentials are missing ("" when configured), read on the
    # worker so the render path never touches the environment.
    jira_missing: str = ""
    # The snapshot file's mtime (epoch seconds), for its age when the file
    # carries no readable ``taken_at`` — read on the worker, like the rest.
    snapshot_mtime: float | None = None


@dataclass(frozen=True)
class LaneCounts:
    """What one lane header says, counted once per refresh."""

    rows: int = 0  # sessions plus ticket rows
    tickets: int = 0  # distinct tickets in the lane — what a column's wip counts
    working: int = 0
    needs: int = 0  # sessions waiting on a human, plus runs awaiting your confirmation
    queued: int = 0  # approved agent runs waiting for a free slot (⧗)
    blocked: int = 0  # runs a guard is holding (⊘)
    stuck: int = 0  # runs the tick gave up on (⚠)


@dataclass(frozen=True)
class TicketRowData:
    """Everything a ticket row draws, so a resize can redraw it without the app."""

    key: str
    record: TicketState | None = None
    badge: str = ""
    prs: tuple[PrState, ...] = ()
    show_assignee: bool = True
    column: Column | None = None


def gather(*, refresh: bool, force: bool = False) -> LaneData:
    """Read the board's inputs, running a flows tick first when asked. Blocking.

    The tick runs only when there is a board.toml whose ``[board]`` is valid
    and Jira is configured; otherwise this just reads what is on disk, which
    is how lanes still render from a snapshot another process wrote. With no
    flow rules the tick is just the board refresh. ``force`` skips the
    tick's coalescing (a snapshot under a minute old), for an explicit `r`.
    """
    config = board.load()
    result: RefreshResult | None = None
    if refresh and config is not None and config.lanes_ok and jira_api.is_configured():
        result = flows_tick.run(config, force=force).refresh
    snap = result.snapshot if result is not None and result.ok else board_snapshot.load()
    statuses = {key: w.jira_status for key, w in work_items.load_all().items() if w.jira_status}
    mtime: float | None = None
    with contextlib.suppress(OSError):
        mtime = board_snapshot.snapshot_path().stat().st_mtime
    return LaneData(
        config=config,
        snapshot=snap,
        work_status=statuses,
        result=result,
        runs=tuple(runs.list_runs()),
        jira_missing=jira_api.missing_credentials() if config is not None else "",
        snapshot_mtime=mtime,
    )


def jira_browse_url(key: str) -> str:
    """The ticket's page on the configured Jira site, or "" when no site is set.

    Only the site is needed to open a ticket in a browser, so this works
    without the email and token the API calls need. Read from the same
    variables as :func:`ephor.jira_api.credentials`.
    """
    for name in jira_api.CREDENTIAL_ENV["url"]:
        base = os.environ.get(name, "").strip().rstrip("/")
        if base.lower().startswith(("https://", "http://")):
            return f"{base}/browse/{key}"
    return ""


class Lanes:
    """The app's lane state: config, snapshot, and which folds were defaulted."""

    def __init__(self, data: LaneData | None = None) -> None:
        self._data = data or LaneData()
        # Lanes that have already been given their default fold. A lane is
        # defaulted once, so unfolding Done by hand is not undone a minute
        # later by the next refresh.
        self._defaulted: set[str] = set()
        self._badges: dict[str, str] = runs.badges_by_ticket(self._data.runs)
        self._uniform_assignee = self._compute_uniform_assignee()

    # --- inputs -----------------------------------------------------------

    def apply(self, data: LaneData) -> None:
        self._data = data
        self._badges = runs.badges_by_ticket(data.runs)
        self._uniform_assignee = self._compute_uniform_assignee()

    def update_run(self, run: Run) -> None:
        """Fold one run the user just changed (approve / deny) in before the next tick."""
        kept = [r for r in self._data.runs if r.run_id != run.run_id]
        self.apply(replace(self._data, runs=(run, *kept)))

    # --- flows ------------------------------------------------------------

    @property
    def runs(self) -> tuple[Run, ...]:
        return self._data.runs

    def badge_for(self, ticket: str | None) -> str:
        """The flow badge for ``ticket``'s row, or ""."""
        return self._badges.get(ticket.upper(), "") if ticket else ""

    @property
    def awaiting_count(self) -> int:
        """Runs waiting for a human — the header bar's flow counter."""
        return sum(1 for r in self._data.runs if r.state is RunState.AWAITING_CONFIRM)

    def flow_counts(self, now: datetime | None = None) -> tuple[int, int, int, int, bool]:
        """(awaiting, queued, blocked, stuck, any badge) — what the header's FLOWS cell shows."""
        moment = now or datetime.now(UTC)
        shown = [r for r in self._data.runs if runs.shows_badge(r, moment)]
        return (
            sum(1 for r in shown if r.state is RunState.AWAITING_CONFIRM),
            sum(1 for r in shown if runs.is_queued(r)),
            sum(1 for r in shown if r.state is RunState.BLOCKED and not runs.is_queued(r)),
            sum(1 for r in shown if r.state is RunState.STUCK),
            bool(shown),
        )

    @property
    def config(self) -> BoardConfig | None:
        return self._data.config

    @property
    def snapshot(self) -> Snapshot | None:
        return self._data.snapshot

    @property
    def last_result(self) -> RefreshResult | None:
        return self._data.result

    def health(self, now: datetime | None = None) -> str:
        """What is wrong with the board and its flows, in a few words; "" when nothing is.

        A broken board.toml or a failing tick otherwise looks exactly like a
        quiet board: the lanes keep drawing the last snapshot and no run is
        ever created. The header bar shows this, and the app toasts it once
        when it changes. Without a board.toml there is nothing to report.
        """
        config = self._data.config
        if config is None:
            return ""
        if config.board_errors:
            return f"board.toml [board] has {len(config.board_errors)} error(s) — ephor flows check"
        problems: list[str] = []
        if config.flow_errors:
            problems.append(
                f"board.toml has {len(config.flow_errors)} flow error(s), no flow runs"
                " — ephor flows check"
            )
        if self._data.jira_missing:
            problems.append(f"Jira not configured — missing {self._data.jira_missing}")
        result = self._data.result
        if result is not None and result.status not in _HEALTHY:
            problems.append(f"board: {result.message}")
        # Once this dashboard has tried a tick the snapshot should be at most
        # a couple of intervals old. Before that (or with Jira off, when no
        # tick ever runs here) it is old by design for a moment, so only the
        # board's own stale limit counts — a snapshot from yesterday is
        # still news, tick or no tick.
        age = self.snapshot_age(now)
        if age is not None:
            limit = self.stale_limit()
            if result is not None:
                limit = min(limit, _STALE_INTERVALS * SNAPSHOT_REFRESH_INTERVAL)
            if age > limit:
                problems.append(f"snapshot {_age_words(age)} old")
        return " · ".join(problems)

    # --- the board line ---------------------------------------------------

    def snapshot_age(self, now: datetime | None = None) -> float | None:
        """Seconds since the snapshot was saved: its ``taken_at``, else the file's mtime."""
        snap = self._data.snapshot
        age = snap.age_sec(now) if snap is not None else None
        if age is None and self._data.snapshot_mtime is not None:
            moment = (now or datetime.now(UTC)).timestamp()
            age = max(0.0, moment - self._data.snapshot_mtime)
        return age

    def stale_limit(self) -> float:
        """How old the snapshot may be before it is shown as stale."""
        config = self._data.config
        return config.flows.max_event_age if config is not None else DEFAULT_STALE_SEC

    def is_stale(self, now: datetime | None = None) -> bool:
        age = self.snapshot_age(now)
        return age is not None and age > self.stale_limit()

    def board_line(self, off_board: int = 0, now: datetime | None = None) -> str:
        """``board · 5m ago · 12 tickets · 2 off-board`` — the first line of the status lanes.

        Drawn whether or not a tick has run, and whether or not Jira is
        configured, so the age of what you are looking at is never a guess.
        ``off_board`` is how many sessions are on a ticket the board does
        not show.
        """
        age = self.snapshot_age(now)
        if age is None:
            when = f"[{_WARN}]no snapshot yet[/]"
        elif self.is_stale(now):
            when = f"[bold {_WARN}]{_age_words(age)} ago — stale[/]"
        else:
            when = f"{_age_words(age)} ago"
        count = len(self._on_board())
        parts = ["[bold #58a6ff]board[/]", when, f"{count} ticket{'s' if count != 1 else ''}"]
        if off_board:
            parts.append(f"{off_board} off-board")
        return " · ".join(parts)

    @property
    def settings(self) -> BoardSettings:
        """The columns to draw: board.toml's when its ``[board]`` is sound, else the defaults."""
        config = self._data.config
        if config is not None and config.lanes_ok:
            return config.board
        return BoardSettings()

    @property
    def board_errors(self) -> tuple[str, ...]:
        config = self._data.config
        return config.board_errors if config is not None else ()

    # --- placement --------------------------------------------------------

    def ticket(self, key: str) -> TicketState | None:
        snap = self._data.snapshot
        return snap.tickets.get(key) if snap is not None else None

    def status_of(self, key: str, live_status: str = "") -> str:
        """The best-known status of ``key`` (see the module docstring for the order)."""
        record = self.ticket(key)
        if record is not None and record.on_board and record.status:
            return record.status
        if live_status:
            return live_status
        stored = self._data.work_status.get(key)
        if stored:
            return stored
        return record.status if record is not None else ""

    def lane_of(self, ticket: str | None, live_status: str = "") -> str:
        """The lane a session on ``ticket`` belongs in."""
        if not ticket:
            return UNTICKETED_LANE
        status = self.status_of(ticket, live_status)
        record = self.ticket(ticket)
        # The category only describes the snapshot's status; a fresher one
        # from elsewhere is placed by name alone.
        category = record.status_category if record is not None and record.status == status else ""
        return self.settings.column_for(status, category)

    def _lane_of_record(self, record: TicketState) -> str:
        return self.settings.column_for(record.status, record.status_category)

    def column(self, lane: str) -> Column | None:
        return next((c for c in self.settings.columns if c.name == lane), None)

    def order(self) -> tuple[str, ...]:
        """Every lane, in display order."""
        return (*self.settings.lane_names(), UNTICKETED_LANE)

    def done_lanes(self) -> frozenset[str]:
        """Lanes holding finished work — folded the first time they are drawn."""
        finished = jira_api.done_statuses()
        settings = self.settings
        lanes = {
            column.name
            for column in settings.columns
            if column.name.lower() in finished or any(s in finished for s in column.statuses)
        }
        by_lane: dict[str, list[TicketState]] = {}
        for record in self._on_board().values():
            by_lane.setdefault(self._lane_of_record(record), []).append(record)
        for lane, records in by_lane.items():
            if records and all(r.is_done for r in records):
                lanes.add(lane)
        return frozenset(lanes)

    def apply_default_folds(
        self, collapsed: set[str], occupied: frozenset[str] = frozenset()
    ) -> None:
        """Fold each finished lane the first time it is seen.

        ``occupied`` names the lanes holding a session. ``Other`` is folded
        only while it holds nothing but done tickets: a session lands there
        when nobody knows its ticket's status, and folding would hide it.
        """
        for lane in self.done_lanes() - self._defaulted:
            if lane == OTHER_LANE and lane in occupied:
                continue
            collapsed.add(lane)
            self._defaulted.add(lane)

    def reset_folds(self) -> None:
        """Forget which lanes were defaulted (the user left the status grouping)."""
        self._defaulted.clear()

    # --- rows -------------------------------------------------------------

    def _on_board(self) -> dict[str, TicketState]:
        snap = self._data.snapshot
        return snap.on_board() if snap is not None else {}

    def _compute_uniform_assignee(self) -> bool:
        return len({t.assignee.strip().lower() for t in self._on_board().values()}) <= 1

    def row_data(self, key: str) -> TicketRowData:
        """What ticket row ``key`` draws."""
        record = self.ticket(key)
        snap = self._data.snapshot
        return TicketRowData(
            key=key,
            record=record,
            badge=self.badge_for(key),
            prs=tuple(snap.prs_for(key)) if snap is not None else (),
            show_assignee=not self._uniform_assignee,
            column=self.column(self._lane_of_record(record)) if record is not None else None,
        )

    def prs_for(self, key: str) -> tuple[PrState, ...]:
        snap = self._data.snapshot
        return tuple(snap.prs_for(key)) if snap is not None else ()

    def tickets_without_session(
        self, session_tickets: set[str], needle: str = ""
    ) -> dict[str, list[tuple[str, TicketState]]]:
        """Board tickets with no live session, by lane, longest in their status first.

        Tickets whose time in status is unknown come last; ties go by key.

        ``session_tickets`` must come from *every* live session, not only
        the ones a filter left visible — a ticket whose session is merely
        filtered out still has a session, and drawing it as a sessionless
        row would say otherwise. ``needle`` filters the ticket rows
        themselves, on key, status, title and assignee.
        """
        wanted = needle.strip().lower()
        settings = self.settings
        lanes: dict[str, list[tuple[str, TicketState]]] = {}
        on_board = self._on_board()
        for key in sorted(on_board, key=lambda k: (_entered_sort(on_board[k]), ticket_sort_key(k))):
            if key in session_tickets:
                continue
            record = on_board[key]
            if wanted:
                haystack = " ".join((key, record.status, record.title, record.assignee)).lower()
                if wanted not in haystack:
                    continue
            lanes.setdefault(settings.column_for(record.status, record.status_category), []).append(
                (key, record)
            )
        return lanes

    def lane_counts(
        self,
        sessions: Mapping[str, list[AgentState]],
        tickets: Mapping[str, list[tuple[str, TicketState]]],
        ticket_of: Callable[[AgentState], str],
    ) -> dict[str, LaneCounts]:
        """Every lane header's numbers, counted once for the whole refresh."""
        counts: dict[str, LaneCounts] = {}
        for lane in {*sessions, *tickets}:
            members = sessions.get(lane, [])
            extras = tickets.get(lane, [])
            keys = {k for a in members if (k := ticket_of(a))} | {k for k, _ in extras}
            glyphs = [b[:1] for k in keys if (b := self.badge_for(k))]
            counts[lane] = LaneCounts(
                rows=len(members) + len(extras),
                tickets=len(keys),
                working=sum(1 for a in members if a.status is AgentStatus.WORKING),
                needs=sum(1 for a in members if a.status in ATTENTION_STATUSES) + glyphs.count("⏸"),
                queued=glyphs.count("⧗"),
                blocked=glyphs.count("⊘"),
                stuck=glyphs.count("⚠"),
            )
        return counts

    def rows(
        self,
        sessions: Mapping[str, list[AgentState]],
        tickets: Mapping[str, list[tuple[str, TicketState]]],
        collapsed: set[str],
    ) -> list[tuple[str, AgentState | None]]:
        """The lane rows in display order: (row id, agent or None).

        Every configured column gets a header even when empty — it is a
        board, and an empty column is information. ``Other`` and
        ``Unticketed`` appear only when something is in them. Inside a lane,
        sessions come first in the order given (the dashboard's stable
        order), then ticket rows.
        """
        configured = {column.name for column in self.settings.columns}
        rows: list[tuple[str, AgentState | None]] = []
        for lane in self.order():
            members = sessions.get(lane, [])
            extras = tickets.get(lane, [])
            if lane not in configured and not members and not extras:
                continue
            rows.append((group_row_id(lane), None))
            if lane in collapsed:
                continue
            rows.extend((agent.session_id, agent) for agent in members)
            rows.extend((virtual_row_id(key), None) for key, _ in extras)
        return rows

    def header_markup(
        self, lane: str, counts: LaneCounts, collapsed: bool, *, stale: bool = False
    ) -> str:
        """One lane header: ``▾ In Progress 20 · 3/4 wip · ▶2 working · !1 needs you · ⊘1``.

        The wip part appears only for a column with a ``wip`` limit, in the
        warning colour once the lane holds more tickets than that. A stale
        snapshot dims every header: the counts are as old as it is.
        """
        caret = "▸" if collapsed else "▾"
        parts = [f"[bold #58a6ff]{caret} {markup_text(lane)}[/] [bold]{counts.rows}[/]"]
        column = self.column(lane)
        wip = column.wip if column is not None else None
        if wip:
            style = f"bold {_ALERT}" if counts.tickets > wip else "dim"
            parts.append(f"[{style}]{counts.tickets}/{wip} wip[/]")
        if counts.working:
            parts.append(f"[#A3BE8C]▶{counts.working} working[/]")
        if counts.needs:
            parts.append(f"[bold #BF616A]!{counts.needs} needs you[/]")
        if counts.queued:
            parts.append(f"[#8be9fd]⧗{counts.queued} queued[/]")
        if counts.blocked:
            parts.append(f"[#d29922]⊘{counts.blocked}[/]")
        if counts.stuck:
            parts.append(f"[bold {_ALERT}]⚠{counts.stuck}[/]")
        line = " · ".join(parts)
        return f"  [dim]{line}[/]" if stale else f"  {line}"


_HEALTHY = frozenset({RefreshStatus.OK, RefreshStatus.LOCKED})


def _age_words(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _entered(record: TicketState) -> datetime | None:
    return runs.parse_iso(record.entered_at) if record.entered_at else None


def _entered_sort(record: TicketState) -> tuple[int, float]:
    """Oldest in its status first; an unknown start sorts after every known one."""
    entered = _entered(record)
    return (0, entered.timestamp()) if entered is not None else (1, 0.0)


def time_in_status(
    record: TicketState, column: Column | None = None, now: datetime | None = None
) -> tuple[str, str]:
    """(``3d`` / ``~5h`` / "", style): how long the ticket has sat in its status.

    ``~`` marks an estimate (Jira's last category change, not a status
    change this board watched). Past the column's ``warn_after`` the age is
    amber, past ``alert_after`` red.
    """
    entered = _entered(record)
    if entered is None:
        return "", "dim"
    age = max(0.0, ((now or datetime.now(UTC)) - entered).total_seconds())
    text = ("~" if record.entered_estimated else "") + _age_words(age)
    alert = column.alert_after if column is not None else None
    warn = column.warn_after if column is not None else None
    if alert is not None and age > alert:
        return text, f"bold {_ALERT}"
    if warn is not None and age > warn:
        return text, _WARN
    return text, "dim"


def initials(name: str) -> str:
    """``Ann Lee`` → ``AL``; ``ann`` → ``AN``; nobody → ``··``."""
    words = printable(name).split()
    if not words:
        return "··"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[-1][0]).upper()


def _text_cell(text: str, width: int, style: str = "") -> str:
    """Ticket text padded to exactly ``width`` columns, and inert as markup."""
    if width <= 0:
        return ""
    cleaned = printable(text)
    if cell_len(cleaned) > width:
        while cleaned and cell_len(cleaned) > width - 1:
            cleaned = cleaned[:-1]
        cleaned += "…"
    # A trailing backslash would escape the closing tag; a pad space keeps
    # it from reaching one, and costs nothing when the cell is not full.
    if cleaned.endswith("\\") and cell_len(cleaned) >= width:
        cleaned = cleaned[:-1] + "…"
    padded = cleaned + " " * max(0, width - cell_len(cleaned))
    body = padded.replace("[", r"\[")
    return f"[{style}]{body}[/]" if style else body


_KEY_W = 10
_AGE_W = 4
_STATUS_W = 14


def ticket_row_markup(
    key: str,
    record: TicketState | None,
    badge: str = "",
    *,
    prs: Sequence[PrState] = (),
    show_assignee: bool = True,
    column: Column | None = None,
    width: int = DEFAULT_WIDTH,
    now: datetime | None = None,
) -> str:
    """One ticket row, dim because nothing is running on it.

    ``◇ KEY  age  status  ⚑ badge  AB title … #12 v``. The key, age and
    status are fixed-width so a lane reads as columns, and the PR cell is
    placed where a session row's work cell is (:func:`plan_row` at the same
    width), so PR and CI glyphs line up down the whole board.
    Every value is ticket text, typed by whoever edits the ticket, so all of
    it is escaped and stripped of control characters before it is markup.
    """
    if record is None:
        flag = f" [bold {_WARN}]{markup_text(badge)}[/]" if badge else ""
        return f"   [dim]◇ {markup_text(key)}[/]{flag}"
    layout = plan_row(max(MIN_WIDTH, width))
    pr_at = layout.visible_width - layout.work
    age, age_style = time_in_status(record, column, now)
    # A long project key widens its own row rather than being cut: the key
    # is the one thing on the row you would type somewhere else.
    key_w = max(_KEY_W, cell_len(printable(key)))
    parts = [
        " [dim]◇[/] ",
        _text_cell(key, key_w, "bold"),
        " ",
        _text_cell(age, _AGE_W, age_style),
        " ",
        _text_cell(record.status or "?", _STATUS_W, _WARN),
        " ",
    ]
    used = 3 + key_w + 1 + _AGE_W + 1 + _STATUS_W + 1
    if record.flagged:
        parts.append(f"[bold {_ALERT}]⚑[/] ")
        used += 2
    if badge:
        shown = printable(badge)
        parts.append(f"[bold {_WARN}]{markup_text(shown)}[/] ")
        used += cell_len(shown) + 1
    if show_assignee:
        parts.append(_text_cell(initials(record.assignee), 2, "#8b949e") + " ")
        used += 3
    parts.append(_text_cell(record.title, max(1, pr_at - used - 1), "dim") + " ")
    parts.append(render_snapshot_pr_cell(prs, layout.work))
    return "".join(parts)


class TicketRow(Static):
    """A board ticket with no session: one dim line, never a session."""

    def __init__(self, data: TicketRowData) -> None:
        super().__init__()
        self.ticket_key = data.key
        self._row = data
        self._repaint()

    def update_ticket(self, data: TicketRowData) -> None:
        self._row = data
        self._repaint()

    def on_resize(self, event: events.Resize) -> None:
        """Re-place the PR cell for the new width, as a session row re-plans."""
        self._repaint()

    def _measured_width(self) -> int:
        try:
            width = int(self.size.width)
        except (AttributeError, TypeError, ValueError):
            return DEFAULT_WIDTH
        return width if width >= MIN_WIDTH else DEFAULT_WIDTH

    def _repaint(self) -> None:
        row = self._row
        self.update(
            ticket_row_markup(
                row.key,
                row.record,
                row.badge,
                prs=row.prs,
                show_assignee=row.show_assignee,
                column=row.column,
                width=self._measured_width(),
            )
        )

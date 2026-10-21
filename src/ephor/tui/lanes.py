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

Lanes that hold finished work (a ``Done`` column, or one whose tickets are
all in Jira's done category) start folded, so a week of closed tickets does
not push the live work off the screen. A lane folded or unfolded by hand
stays that way; only a lane seen for the first time gets the default.

Both kinds of row carry a **flow badge** when their ticket has a run in
flight (``⏸ review?`` awaiting your confirmation, ``▶ review`` launched,
``⚠ review stuck``); see :func:`ephor.flows.runs.badge`.

Nothing here does I/O on the render path. :func:`gather` reads board.toml,
runs the flows tick (which refreshes the snapshot), and reads the runs and
the work records on a worker thread; the app hands the result to
:meth:`Lanes.apply` on the event loop.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime

from textual.widgets import Static

from ephor import board, jira_api, work_items
from ephor.board import OTHER_LANE, UNTICKETED_LANE, BoardConfig, BoardSettings
from ephor.constants import ATTENTION_STATUSES, AgentStatus
from ephor.flows import printable, runs, ticket_sort_key
from ephor.flows import snapshot as board_snapshot
from ephor.flows import tick as flows_tick
from ephor.flows.runs import Run, RunState
from ephor.flows.snapshot import RefreshResult, RefreshStatus, Snapshot, TicketState
from ephor.state.models import AgentState
from ephor.tui.rows import group_row_id, virtual_row_id

# The tick's cadence, from the TUI. Tickets move when a human drags a card,
# and one search covers the whole board, so this is one request a minute.
SNAPSHOT_REFRESH_INTERVAL = 60.0
# A snapshot older than this many tick intervals is reported as stale.
_STALE_INTERVALS = 2.0


def markup_text(text: str, limit: int | None = None) -> str:
    """:func:`~ephor.flows.printable` text made inert for Textual markup.

    Ticket text is typed by whoever edits the ticket, and the widgets that
    show it parse Textual markup — not Rich's. ``rich.markup.escape`` leaves
    Textual-only syntax such as ``[$accent]`` live, and its doubled trailing
    backslash still reads, to Textual, as escaping the ``[`` of the next
    closing tag. So every ``[`` is escaped, and a trailing backslash gets a
    space after it so it cannot reach the tag that follows.
    """
    cleaned = printable(text, limit).replace("[", r"\[")
    return cleaned + " " if cleaned.endswith("\\") else cleaned


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


def gather(*, refresh: bool) -> LaneData:
    """Read the board's inputs, running a flows tick first when asked. Blocking.

    The tick runs only when there is a board.toml whose ``[board]`` is valid
    and Jira is configured; otherwise this just reads what is on disk, which
    is how lanes still render from a snapshot another process wrote. With no
    flow rules the tick is exactly the snapshot refresh.
    """
    config = board.load()
    result: RefreshResult | None = None
    if refresh and config is not None and config.lanes_ok and jira_api.is_configured():
        result = flows_tick.run(config).refresh
    snap = result.snapshot if result is not None and result.ok else board_snapshot.load()
    statuses = {key: w.jira_status for key, w in work_items.load_all().items() if w.jira_status}
    return LaneData(
        config=config,
        snapshot=snap,
        work_status=statuses,
        result=result,
        runs=tuple(runs.list_runs()),
        jira_missing=jira_api.missing_credentials() if config is not None else "",
    )


class Lanes:
    """The app's lane state: config, snapshot, and which folds were defaulted."""

    def __init__(self, data: LaneData | None = None) -> None:
        self._data = data or LaneData()
        # Lanes that have already been given their default fold. A lane is
        # defaulted once, so unfolding Done by hand is not undone a minute
        # later by the next refresh.
        self._defaulted: set[str] = set()
        self._badges: dict[str, str] = runs.badges_by_ticket(self._data.runs)

    # --- inputs -----------------------------------------------------------

    def apply(self, data: LaneData) -> None:
        self._data = data
        self._badges = runs.badges_by_ticket(data.runs)

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
        snap = self._data.snapshot
        # Only once this dashboard has tried a tick: the snapshot read at
        # start-up is old by design until the first one lands.
        if result is not None and snap is not None:
            age = snap.age_sec(now)
            if age is not None and age > _STALE_INTERVALS * SNAPSHOT_REFRESH_INTERVAL:
                problems.append(f"snapshot {_age_words(age)} old")
        return " · ".join(problems)

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
        return self.settings.column_for(self.status_of(ticket, live_status))

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
            by_lane.setdefault(settings.column_for(record.status), []).append(record)
        for lane, records in by_lane.items():
            if lane != OTHER_LANE and records and all(r.is_done for r in records):
                lanes.add(lane)
        return frozenset(lanes)

    def apply_default_folds(self, collapsed: set[str]) -> None:
        """Fold each finished lane the first time it is seen."""
        for lane in self.done_lanes() - self._defaulted:
            collapsed.add(lane)
            self._defaulted.add(lane)

    def reset_folds(self) -> None:
        """Forget which lanes were defaulted (the user left the status grouping)."""
        self._defaulted.clear()

    # --- rows -------------------------------------------------------------

    def _on_board(self) -> dict[str, TicketState]:
        snap = self._data.snapshot
        return snap.on_board() if snap is not None else {}

    def tickets_without_session(
        self, session_tickets: set[str], needle: str = ""
    ) -> dict[str, list[tuple[str, TicketState]]]:
        """Board tickets with no live session, by lane, in key order.

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
        for key in sorted(on_board, key=ticket_sort_key):
            if key in session_tickets:
                continue
            record = on_board[key]
            if wanted:
                haystack = " ".join((key, record.status, record.title, record.assignee)).lower()
                if wanted not in haystack:
                    continue
            lanes.setdefault(settings.column_for(record.status), []).append((key, record))
        return lanes

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
        self, lane: str, members: list[AgentState], sessionless: int, collapsed: bool
    ) -> str:
        """One lane header: name, sessions, tickets without one, who needs you."""
        caret = "▸" if collapsed else "▾"
        needs = sum(1 for a in members if a.status in ATTENTION_STATUSES)
        working = sum(1 for a in members if a.status is AgentStatus.WORKING)
        parts = [f"[bold #58a6ff]{caret} {markup_text(lane)}[/]"]
        parts.append(f"[dim]{len(members)} session(s)[/]")
        if sessionless:
            parts.append(f"[dim]{sessionless} ticket(s) without a session[/]")
        if working:
            parts.append(f"[#A3BE8C]{working} working[/]")
        if needs:
            parts.append(f"[bold #BF616A]{needs} need you[/]")
        return "  " + "  ·  ".join(parts)


_HEALTHY = frozenset({RefreshStatus.OK, RefreshStatus.LOCKED})


def _age_words(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def ticket_row_markup(key: str, record: TicketState | None, badge: str = "") -> str:
    """One ticket row: key, status, flow badge, title — dimmed, because nothing is running.

    The badge comes after the fixed-width key and status columns, as on a
    session row, so a badged row lines up with the rest of its lane.
    Every value is ticket text, typed by whoever edits the ticket, so all of
    it is escaped and stripped of control characters before it is markup.
    """
    flag = f"[bold #EBCB8B]{markup_text(badge)}[/] " if badge else ""
    if record is None:
        return f"   [dim]◇ {markup_text(key)}[/] {flag}".rstrip()
    status = markup_text(record.status, 18) or "?"
    title = markup_text(record.title, 120)
    who = markup_text(record.assignee, 24)
    tail = f"  [dim]{who}[/]" if who else ""
    return (
        f"   [dim]◇[/] [bold]{markup_text(key):<10}[/] "
        f"[#EBCB8B]{status:<14}[/] {flag}[dim]{title}[/]{tail}"
    )


class TicketRow(Static):
    """A board ticket with no session: one dim line, never a session."""

    def __init__(self, key: str, record: TicketState | None, badge: str = "") -> None:
        super().__init__(ticket_row_markup(key, record, badge))
        self.ticket_key = key

    def update_ticket(self, record: TicketState | None, badge: str = "") -> None:
        self.update(ticket_row_markup(self.ticket_key, record, badge))

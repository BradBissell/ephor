"""The ``f`` screen: every flow run in flight, and the two keys that answer one.

A flow in ``mode = "confirm"`` (every flow that starts an agent) does nothing
until a human says so. The badge on a row says a run is waiting; this screen
is where you answer it — ``y`` approves, ``N`` pressed twice within two
seconds denies, ``escape`` closes. Deny is shifted and confirmed because it
cannot be undone: a denied run stays denied until its event happens again.
The list re-reads the run files every two seconds while the screen is open,
and the panel under it shows exactly what the highlighted run will execute.

The keys are deliberately not ``a`` / ``d``. Those answer *permission
requests* on the dashboard, and a muscle-memory ``a`` meant for a flow must
never grant a session permission to run a tool, nor the other way round.
For the same reason no key inside this screen ever reaches a session: the
dashboard's ``enter`` (jump to a session's pane) and ``space`` (mark one)
are priority bindings that would otherwise still fire under a modal, and a
row's Selected message would otherwise bubble up to the app's jump. Both
are swallowed here.

Approving only moves the run to ``claimed`` (a compare-and-set on its file:
see :mod:`ephor.flows.runs`); the tick does the dispatching, guards and all.
So the app is asked for an immediate tick as soon as a run is approved,
rather than leaving it for the next one-minute pass.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static

from ephor.board import BoardConfig
from ephor.flows import runs
from ephor.flows.runs import Run, RunState
from ephor.flows.snapshot import TicketState
from ephor.tui.lanes import markup_text

# Finished runs shown under the live ones, newest first, for context ("the
# review I approved an hour ago did launch"). `ephor flows ls --all` has the
# rest.
RECENT_FINISHED = 10
_RECENT_SEC = 24 * 3600.0
# How often the open screen re-reads the run files.
LIST_REFRESH_SEC = 2.0
# `N` must be pressed again within this long to deny.
DENY_CONFIRM_WINDOW_SEC = 2.0

_QUEUED_STYLE = "#8be9fd"

_STATE_STYLE: dict[RunState, str] = {
    RunState.AWAITING_CONFIRM: "bold #EBCB8B",
    RunState.CLAIMED: "#8be9fd",
    RunState.BLOCKED: "#d29922",
    RunState.LAUNCHED: "#A3BE8C",
    RunState.STUCK: "bold #f85149",
    RunState.FAILED: "#f85149",
}


def ordered_runs(all_runs: Sequence[Run], now: datetime | None = None) -> list[Run]:
    """Awaiting first (oldest first — it has waited longest), then the rest in flight, then recent finished."""
    moment = now or datetime.now(UTC)
    awaiting = sorted(
        (r for r in all_runs if r.state is RunState.AWAITING_CONFIRM),
        key=lambda r: (r.created_at, r.run_id),
    )
    active = [
        r
        for r in all_runs
        if r.state is not RunState.AWAITING_CONFIRM and runs.shows_badge(r, moment)
    ]
    active.sort(key=lambda r: (r.updated_at, r.run_id), reverse=True)
    shown = {r.run_id for r in (*awaiting, *active)}
    finished = [
        r
        for r in all_runs
        if r.run_id not in shown
        and r.terminal
        and (runs.age_sec(r.updated_at, moment) or 0.0) < _RECENT_SEC
    ]
    finished.sort(key=lambda r: (r.updated_at, r.run_id), reverse=True)
    return [*awaiting, *active, *finished[:RECENT_FINISHED]]


def run_markup(run: Run, now: datetime | None = None) -> str:
    """One line for one run: state, id, how long ago, why."""
    style = _STATE_STYLE.get(run.state, "dim")
    age = runs.age_sec(run.updated_at, now)
    when = "?" if age is None else _short_age(age)
    stale = " [#d29922]stale[/]" if run.stale else ""
    reason = f"  [dim]{markup_text(run.reason, 80)}[/]" if run.reason else ""
    status = f" [dim]· {markup_text(run.label, 32)}[/]" if run.label else ""
    state = str(run.state)
    if runs.is_queued(run):
        style, state = _QUEUED_STYLE, "queued"
    return (
        f"[{style}]{state:<16}[/] [bold]{markup_text(run.run_id)}[/]"
        f"{status} [dim]{when}[/]{stale}{reason}"
    )


def _short_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def run_detail_markup(
    run: Run | None,
    config: BoardConfig | None = None,
    tickets: Mapping[str, TicketState] | None = None,
    queue: Sequence[Run] = (),
) -> str:
    """The panel under the list: the highlighted run's ticket, flow, trigger and what it runs.

    The rule is looked up in board.toml by ``run.flow`` and its template
    expanded the way the tick will expand it, so what you approve is what
    you read. A rule that has since been removed or renamed is said so —
    the tick will refuse that run anyway.
    """
    if run is None:
        return "[dim]no run selected[/]"
    record = (tickets or {}).get(run.ticket)
    title = run.title or (record.title if record is not None else "")
    lines = [f"[bold]{markup_text(run.ticket)}[/] [dim]{markup_text(title, 100)}[/]"]
    trigger = f"{markup_text(run.trigger)} {markup_text(run.label, 60)}".strip()
    lines.append(f"flow [bold]{markup_text(run.flow)}[/] · {trigger}")
    if runs.is_queued(run):
        ahead = [r.run_id for r in queue]
        place = ahead.index(run.run_id) + 1 if run.run_id in ahead else 0
        where = f"#{place} of {len(ahead)}" if place else "in the queue"
        lines.append(
            f"[{_QUEUED_STYLE}]queued {where}[/] [dim]— launches when an agent slot frees"
            f" ({markup_text(run.reason, 60)})[/]"
        )
    rule = config.rule(run.flow) if config is not None else None
    if rule is None:
        where = "board.toml" if config is not None else "board.toml (none found)"
        lines.append(f"[#d29922]flow {markup_text(run.flow)} is not in {where}[/]")
        return "\n".join(lines)
    status = run.matched_status or (record.status if record is not None else "")
    try:
        rendered = rule.render(ticket=run.ticket, title=title, status=status)
    except (KeyError, ValueError, IndexError):
        rendered = rule.template
    field = "start" if rule.launches_agent else "command"
    extra = (
        f" [dim]({markup_text(rule.provider)}{', worktree' if rule.worktree else ''},"
        f" {rule.mode})[/]"
        if rule.launches_agent
        else f" [dim]({rule.mode})[/]"
    )
    lines.append(f"[#8be9fd]{field} =[/] {markup_text(rendered, 300)}{extra}")
    return "\n".join(lines)


class RunRow(Static):
    def __init__(self, run: Run) -> None:
        super().__init__(run_markup(run))
        self.flow_run = run

    def show(self, run: Run) -> None:
        self.flow_run = run
        self.update(run_markup(run))


class FlowsScreen(ModalScreen[None]):
    """A modal list of flow runs with approve / deny."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "approve", "approve"),
        Binding("N", "deny", "deny (twice)"),
        # Lower-case n used to deny. It now only says where deny went, and
        # still keeps the dashboard's `n` (next attention) from firing.
        Binding("n", "deny_moved", show=False),
        Binding("escape", "close", "close"),
        Binding("f", "close", "close", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        # Priority, so they are this screen's before the app's priority
        # jump / mark bindings: Enter on a run must not switch the tmux
        # client to whichever session the dashboard cursor was on.
        Binding("enter", "nothing", show=False, priority=True),
        Binding("space", "nothing", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    FlowsScreen {
        align: center middle;
    }
    #flows-panel {
        width: 90%;
        max-width: 140;
        height: auto;
        max-height: 80%;
        border: round $primary;
        background: $surface;
        padding: 0 1;
    }
    #flows-list {
        height: auto;
        max-height: 24;
    }
    #flows-title, #flows-status {
        height: 1;
    }
    #flows-detail {
        height: auto;
        max-height: 6;
        border-top: solid $primary-darken-2;
    }
    """

    def __init__(
        self,
        all_runs: Sequence[Run],
        *,
        on_change: Callable[[Run], None] | None = None,
        paused: bool = False,
        config: BoardConfig | None = None,
        tickets: Mapping[str, TicketState] | None = None,
        reload: Callable[[], Sequence[Run]] | None = None,
    ) -> None:
        super().__init__()
        self._runs = ordered_runs(all_runs)
        self._on_change = on_change
        self._paused = paused
        self._config = config
        self._tickets: Mapping[str, TicketState] = tickets or {}
        # Where the periodic refresh reads runs from (a blocking read, run
        # on a worker thread). Tests swap it; the default is the run files.
        self._reload = reload or runs.list_runs
        self._rows: list[RunRow] = []
        self._status: Static | None = None
        self._title: Static | None = None
        self._detail: Static | None = None
        self._deny_armed: str | None = None
        self._deny_armed_at: float = 0.0
        self._reloading = False

    def _title_markup(self) -> str:
        """The title, with the awaiting count as the rows show it now."""
        shown = [row.flow_run for row in self._rows] if self._rows else self._runs
        awaiting = sum(1 for r in shown if r.state is RunState.AWAITING_CONFIRM)
        paused = "  [bold #f85149]PAUSED[/]" if self._paused else ""
        return f"[bold]Flows[/]  [dim]{awaiting} awaiting · y approve · N N deny · esc close[/]{paused}"

    def _refresh_title(self) -> None:
        if self._title is not None:
            self._title.update(self._title_markup())

    def compose(self) -> ComposeResult:
        with Vertical(id="flows-panel"):
            self._title = Static(self._title_markup(), id="flows-title")
            yield self._title
            self._rows = [RunRow(run) for run in self._runs]
            items = [ListItem(row) for row in self._rows]
            if not items:
                items = [ListItem(Static("[dim]no flow runs in flight[/]"))]
            yield ListView(*items, id="flows-list")
            self._detail = Static(self._detail_markup(0), id="flows-detail")
            yield self._detail
            self._status = Static("", id="flows-status")
            yield self._status

    def on_mount(self) -> None:
        self.query_one(ListView).focus()
        self.set_interval(LIST_REFRESH_SEC, self._schedule_reload)

    # --- the detail panel and the live list ---------------------------------

    def _detail_markup(self, index: int | None) -> str:
        shown = [row.flow_run for row in self._rows] or self._runs
        if index is None or not 0 <= index < len(shown):
            index = 0
        run = shown[index] if shown else None
        return run_detail_markup(run, self._config, self._tickets, runs.queue(self._runs))

    def _refresh_detail(self) -> None:
        if self._detail is None:
            return
        with contextlib.suppress(Exception):
            self._detail.update(self._detail_markup(self.query_one(ListView).index))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        event.stop()
        self._refresh_detail()

    def _schedule_reload(self) -> None:
        if self._reloading:
            return
        self._reloading = True
        self._reload_worker()

    @work(thread=True, exit_on_error=False, group="flows-reload")
    def _reload_worker(self) -> None:
        found: Sequence[Run] | None = None
        try:
            found = list(self._reload())
        finally:
            with contextlib.suppress(Exception):
                self.app.call_from_thread(self._land_reload, found)

    def _land_reload(self, found: Sequence[Run] | None) -> None:
        self.run_worker(self._apply_reload(found), group="flows-apply", exclusive=True)

    async def _apply_reload(self, found: Sequence[Run] | None) -> None:
        """Fold a fresh read of the run files in, keeping the cursor on its run."""
        self._reloading = False
        if found is None or not self.is_mounted:
            return
        fresh = ordered_runs(found)
        current = [row.flow_run for row in self._rows]
        if [r.run_id for r in fresh] == [r.run_id for r in current]:
            for row, run in zip(self._rows, fresh, strict=True):
                if run != row.flow_run:
                    row.show(run)
            self._runs = fresh
            self._refresh_title()
            self._refresh_detail()
            return
        view = self.query_one(ListView)
        index = view.index
        keep = current[index].run_id if index is not None and 0 <= index < len(current) else None
        self._runs = fresh
        self._rows = [RunRow(run) for run in fresh]
        items = [ListItem(row) for row in self._rows]
        if not items:
            items = [ListItem(Static("[dim]no flow runs in flight[/]"))]
        await view.clear()
        await view.mount(*items)
        ids = [r.run_id for r in fresh]
        view.index = ids.index(keep) if keep in ids else (0 if fresh else None)
        self._refresh_title()
        self._refresh_detail()

    # --- actions ------------------------------------------------------------

    def _cursor_row(self) -> RunRow | None:
        view = self.query_one(ListView)
        index = view.index
        if index is None or not self._rows or not 0 <= index < len(self._rows):
            return None
        return self._rows[index]

    def _say(self, text: str) -> None:
        if self._status is not None:
            self._status.update(text)

    def _answer(self, *, approve: bool) -> None:
        row = self._cursor_row()
        if row is None:
            self._say("no run selected")
            return
        if row.flow_run.state is not RunState.AWAITING_CONFIRM:
            self._say(
                f"[dim]{markup_text(row.flow_run.run_id)} is {row.flow_run.state} — only a run "
                "awaiting confirmation can be approved or denied[/]"
            )
            return
        # On the event loop: wait for runs/.lock only briefly, and report
        # "busy, try again" rather than freeze the whole dashboard behind it.
        run_id = row.flow_run.run_id
        if approve:
            updated, message = runs.approve(run_id, lock_timeout=runs.UI_LOCK_TIMEOUT_SEC)
        else:
            updated, message = runs.deny(run_id, lock_timeout=runs.UI_LOCK_TIMEOUT_SEC)
        self._say(markup_text(message))
        if updated is None:
            # Someone else answered it, or a tick expired it: show what it is now.
            current = runs.load(row.flow_run.run_id)
            if current is not None:
                row.show(current)
            self._refresh_title()
            return
        row.show(updated)
        self._refresh_title()
        self._refresh_detail()
        if self._on_change is not None:
            self._on_change(updated)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # A click (or Enter) on a run row selects nothing in the dashboard
        # behind this screen.
        event.stop()

    def action_nothing(self) -> None:
        """Enter and space: deliberately inert here (see the module docstring)."""

    def action_approve(self) -> None:
        self._answer(approve=True)

    def action_deny(self) -> None:
        """`N` arms; a second `N` on the same run within two seconds denies it."""
        row = self._cursor_row()
        if row is None or row.flow_run.state is not RunState.AWAITING_CONFIRM:
            self._answer(approve=False)  # says why there is nothing to deny
            return
        run_id = row.flow_run.run_id
        now = time.monotonic()
        if self._deny_armed == run_id and now - self._deny_armed_at < DENY_CONFIRM_WINDOW_SEC:
            self._deny_armed = None
            self._answer(approve=False)
            return
        self._deny_armed = run_id
        self._deny_armed_at = now
        self._say(f"press N again within 2s to deny {markup_text(run_id)}")

    def action_deny_moved(self) -> None:
        self._say("deny is N now (press it twice) — y approves")

    def action_close(self) -> None:
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        self.query_one(ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(ListView).action_cursor_up()

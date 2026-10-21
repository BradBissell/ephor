"""The ``f`` screen: every flow run in flight, and the two keys that answer one.

A flow in ``mode = "confirm"`` (every flow that starts an agent) does nothing
until a human says so. The badge on a row says a run is waiting; this screen
is where you answer it — ``y`` approves, ``n`` denies, ``escape`` closes.

The keys are deliberately not ``a`` / ``d``. Those answer *permission
requests* on the dashboard, and a muscle-memory ``a`` meant for a flow must
never grant a session permission to run a tool, nor the other way round.

Approving only moves the run to ``claimed`` (a compare-and-set on its file:
see :mod:`ephor.flows.runs`); the tick does the dispatching, guards and all.
So the app is asked for an immediate tick as soon as a run is approved,
rather than leaving it for the next one-minute pass.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static

from ephor.flows import runs
from ephor.flows.runs import Run, RunState
from ephor.tui.lanes import markup_text

# Finished runs shown under the live ones, newest first, for context ("the
# review I approved an hour ago did launch"). `ephor flows ls --all` has the
# rest.
RECENT_FINISHED = 10
_RECENT_SEC = 24 * 3600.0

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
    return (
        f"[{style}]{run.state!s:<16}[/] [bold]{markup_text(run.run_id)}[/]"
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
        Binding("n", "deny", "deny"),
        Binding("escape", "close", "close"),
        Binding("f", "close", "close", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
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
    """

    def __init__(
        self,
        all_runs: Sequence[Run],
        *,
        on_change: Callable[[Run], None] | None = None,
        paused: bool = False,
    ) -> None:
        super().__init__()
        self._runs = ordered_runs(all_runs)
        self._on_change = on_change
        self._paused = paused
        self._rows: list[RunRow] = []
        self._status: Static | None = None

    def compose(self) -> ComposeResult:
        awaiting = sum(1 for r in self._runs if r.state is RunState.AWAITING_CONFIRM)
        paused = "  [bold #f85149]PAUSED[/]" if self._paused else ""
        with Vertical(id="flows-panel"):
            yield Static(
                f"[bold]Flows[/]  [dim]{awaiting} awaiting · y approve · n deny · esc close[/]{paused}",
                id="flows-title",
            )
            self._rows = [RunRow(run) for run in self._runs]
            items = [ListItem(row) for row in self._rows]
            if not items:
                items = [ListItem(Static("[dim]no flow runs in flight[/]"))]
            yield ListView(*items, id="flows-list")
            self._status = Static("", id="flows-status")
            yield self._status

    def on_mount(self) -> None:
        self.query_one(ListView).focus()

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
        verb = runs.approve if approve else runs.deny
        updated, message = verb(row.flow_run.run_id)
        self._say(markup_text(message))
        if updated is None:
            # Someone else answered it, or a tick expired it: show what it is now.
            current = runs.load(row.flow_run.run_id)
            if current is not None:
                row.show(current)
            return
        row.show(updated)
        if self._on_change is not None:
            self._on_change(updated)

    def action_approve(self) -> None:
        self._answer(approve=True)

    def action_deny(self) -> None:
        self._answer(approve=False)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        self.query_one(ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(ListView).action_cursor_up()

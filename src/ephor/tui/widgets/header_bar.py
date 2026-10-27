"""Top counter strip — one badge per status bucket.

Driven by StatusSummary; pure presentational, no scanning of state files here.
"""

from __future__ import annotations

from textual.widgets import Static

from ephor.state.models import StatusSummary
from ephor.tui.lanes import markup_text

# Hex colors mirror tui/theme.tcss; rich markup needs literal hex, not $vars.
# (label, summary-attr, hex-color) — display order kept stable.
_BADGES: tuple[tuple[str, str, str], ...] = (
    ("PERM", "waiting_permission", "#f85149"),
    ("WAIT", "waiting_answer", "#d29922"),
    ("ERR", "error", "#d2a8ff"),
    ("WORK", "working", "#3fb950"),
    ("IDLE", "idle", "#b9cac9"),
    ("DEAD", "dead", "#3b4048"),
)
_PRIMARY = "#00ffff"
_FLOW = "#EBCB8B"
_FLOW_BAD = "#f85149"


def format_header(summary: StatusSummary, flows_awaiting: int = 0, flows_problem: str = "") -> str:
    """Pure formatter — extracted from the widget so tests can assert on text.

    ``flows_awaiting`` is how many flow runs wait for your confirmation; the
    cell appears only when there are some, since most dashboards have no
    board.toml at all. ``flows_problem`` (see
    :meth:`ephor.tui.lanes.Lanes.health`) adds a ``FLOWS ✗`` cell saying why
    the board or its flows are not working.
    """
    cells: list[str] = []
    for label, attr, color in _BADGES:
        count = getattr(summary, attr, 0)
        if count:
            cells.append(f"[bold {color}]{label}[/] [bold]{count}[/]")
        else:
            cells.append(f"[dim]{label} 0[/]")
    cells.append(f"[bold {_PRIMARY}]TOTAL {summary.total}[/]")
    if flows_awaiting:
        cells.append(f"[bold {_FLOW}]FLOW? {flows_awaiting}[/] [dim](f)[/]")
    if flows_problem:
        cells.append(f"[bold {_FLOW_BAD}]FLOWS ✗[/] [dim]{markup_text(flows_problem, 90)}[/]")
    return "  ".join(cells)


class HeaderBar(Static):
    """Status counters across the top of the dashboard."""

    def update_summary(
        self, summary: StatusSummary, flows_awaiting: int = 0, flows_problem: str = ""
    ) -> None:
        self.update(format_header(summary, flows_awaiting, flows_problem))

"""The ``?`` screen: every key the dashboard answers to, and what the flow badges mean.

Built from the app's own ``BINDINGS`` (as the command palette is), so a new
key shows up here without a second list to keep in step. The badge legend
comes from :data:`ephor.flows.runs.BADGE_LEGEND`, the same table the badges
are drawn from.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ephor.flows import runs
from ephor.tui.lanes import markup_text

# Keys drawn as words in Textual's binding names, shown as the key you press.
_KEY_NAMES = {
    "slash": "/",
    "question_mark": "?",
    "left_square_bracket": "[",
    "right_square_bracket": "]",
    "space": "space",
    "enter": "enter",
    "escape": "esc",
}

# Keys inside the `f` screen, which are not app bindings.
FLOWS_KEYS: tuple[tuple[str, str], ...] = (
    ("y", "approve the highlighted run"),
    ("N N", "deny it (press twice within 2s)"),
    ("j / k", "move; the panel below shows what the run will execute"),
    ("esc / f", "close"),
)


def key_label(key: str) -> str:
    return ", ".join(_KEY_NAMES.get(k.strip(), k.strip()) for k in key.split(","))


def help_markup(bindings: Iterable[Binding]) -> str:
    """The whole help text: dashboard keys, flows-screen keys, badge legend."""
    lines = ["[bold]Keys[/]"]
    for binding in bindings:
        if not binding.description or binding.key in {"ctrl+c", "escape"}:
            continue
        key = markup_text(key_label(binding.key))
        lines.append(f"  [bold #58a6ff]{key:<7}[/] {markup_text(binding.description)}")
    lines.append("")
    lines.append("[bold]Flows screen (f)[/]")
    lines.extend(f"  [bold #58a6ff]{k:<7}[/] {markup_text(v)}" for k, v in FLOWS_KEYS)
    lines.append("")
    lines.append("[bold]Flow badges[/]")
    lines.extend(
        f"  [bold #EBCB8B]{glyph:<7}[/] {markup_text(meaning)}"
        for glyph, meaning in runs.BADGE_LEGEND
    )
    lines.append("")
    lines.append("[bold]Ticket rows[/]")
    lines.append(
        "  [dim]3d / ~3d[/]  time in status (~ estimated); amber / red past the column's limits"
    )
    lines.append("  [bold #f85149]⚑[/]        flagged in Jira")
    lines.append(
        "  [#7ee787]#12[/] v     PR from the board snapshot, then CI: v pass, x fail, ~ pending"
    )
    return "\n".join(lines)


class HelpScreen(ModalScreen[None]):
    """A modal with the key table and the flow badge legend."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close"),
        Binding("question_mark", "close", "close", show=False),
        Binding("q", "close", "close", show=False),
        Binding("enter", "close", show=False, priority=True),
        Binding("space", "close", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-panel {
        width: 90%;
        max-width: 100;
        height: auto;
        max-height: 90%;
        border: round $primary;
        background: $surface;
        padding: 0 1;
    }
    """

    def __init__(self, bindings: Iterable[Binding]) -> None:
        super().__init__()
        self._text = help_markup(bindings)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-panel"):
            yield Static(self._text, id="help-text")

    def action_close(self) -> None:
        self.dismiss(None)

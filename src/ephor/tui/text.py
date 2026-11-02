"""Ticket and Jira text made safe for the widgets that show it.

Kept apart from :mod:`ephor.tui.lanes` so the widgets can use it without
importing the lanes (which import the widgets).
"""

from __future__ import annotations

from ephor.flows import printable


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

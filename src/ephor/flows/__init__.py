"""Ticket flows: the board's view of Jira, and what moving a ticket should do.

:mod:`ephor.board` is the configuration. This package is the part that
*acts* on it, and it is built as a short, idempotent tick rather than a
daemon: each run reads where every ticket on the board sits now, compares
that with the last time it looked, writes the answer down, and exits. A tick
that crashes, or two that race, leave nothing half-done behind — the next one
simply looks again.

The modules:

* :mod:`ephor.flows.snapshot` — search Jira for the board, diff the result
  against ``flows/snapshot.json``, and persist it. On its own that is
  everything the status lanes need; the flow tick builds on the same
  functions rather than running a second search.
* :mod:`ephor.flows.tick` — the tick itself: rules, adopt mode, guards and
  dispatch, run between the snapshot's diff and its write.
* :mod:`ephor.flows.triggers` — the ``on_pr`` and ``on_session``
  observations (``gh`` per ticket, the session state files), diffed against
  the snapshot's previous ones like the statuses are.
* :mod:`ephor.flows.runs` — one file per flow firing, claimed by an
  exclusive create and moved through its states by compare-and-set.
* :mod:`ephor.flows.runners` — how a run is launched: a detached tmux window.
* :mod:`ephor.flows.cli` — the ``ephor flows ...`` subcommands, including
  ``watch``, the loop that keeps flows running while the dashboard is closed.

The user-facing reference is ``docs/flows.md``.

All state lives under :func:`flows_dir`, beside the session state directory.
"""

from __future__ import annotations

import re
from pathlib import Path

from ephor.config import state_dir


def flows_dir() -> Path:
    """Where flow state lives (``$XDG_STATE_HOME/ephor/flows``)."""
    return state_dir().parent / "flows"


def printable(text: str, limit: int | None = None) -> str:
    """``text`` with control characters removed, for a terminal or a widget.

    Ticket titles are typed by anyone with edit rights on the ticket. An
    escape sequence in one must not reach the terminal that prints it, and a
    newline must not split one row of the board into two. Stored values are
    left exactly as Jira returned them; this is applied only where they are
    displayed.
    """
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text or "")
    cleaned = " ".join(cleaned.split())
    if limit is not None and len(cleaned) > limit:
        return cleaned[: max(0, limit - 1)] + "…"
    return cleaned


_KEY_PARTS = re.compile(r"^([A-Z][A-Z0-9]*)-(\d+)$")


def ticket_sort_key(key: str) -> tuple[str, int, str]:
    """Order DR-9 before DR-10: project, then number, then the raw key."""
    match = _KEY_PARTS.match(key)
    if match is None:
        return (key, 0, key)
    return (match.group(1), int(match.group(2)), key)

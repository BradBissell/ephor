"""Row ids in the session list, and the one question every action must ask of them.

The dashboard's ListView holds three kinds of row, all addressed by a string
id in ``EphorApp._sid_by_row``:

* a **session** — the id is the session id, which the hook handler anchors
  to ``[A-Za-z0-9_-]``;
* a **group header** — ``"\\x00grp:<group>"``, drawn when rows are grouped;
* a **ticket row** (a *virtual* row) — ``"\\x00tkt:<KEY>"``, a ticket on the
  board that has no session, drawn only in the status-lane grouping.

The two non-session kinds start with NUL, so neither can collide with a
session id. They also have separate prefixes, because they are not
interchangeable: a header folds, marks its group and counts its members; a
ticket row does none of those things and is not a session either.

The rule the app relies on is :func:`is_session_row`. Any action that kills,
answers, summarizes, pins, resumes or jumps to a session asks it first, so a
row that is not a session can never be mistaken for one — including by
falling back to a set of marked rows the cursor is not on.
"""

from __future__ import annotations

GROUP_PREFIX = "\x00grp:"
TICKET_PREFIX = "\x00tkt:"


def group_row_id(key: str) -> str:
    return f"{GROUP_PREFIX}{key}"


def is_group_row(row_id: str | None) -> bool:
    """True when a row id names a group header instead of a session."""
    return row_id is not None and row_id.startswith(GROUP_PREFIX)


def group_key_of(row_id: str) -> str:
    """The group name inside a header row id."""
    return row_id[len(GROUP_PREFIX) :]


def virtual_row_id(ticket: str) -> str:
    return f"{TICKET_PREFIX}{ticket}"


def is_virtual_row(row_id: str | None) -> bool:
    """True when a row id names a ticket that has no session."""
    return row_id is not None and row_id.startswith(TICKET_PREFIX)


def ticket_of_virtual_row(row_id: str) -> str:
    """The ticket key inside a virtual row id."""
    return row_id[len(TICKET_PREFIX) :]


def is_session_row(row_id: str | None) -> bool:
    """True only for a row that *is* a session — never a header or a ticket row."""
    return row_id is not None and row_id != "" and not row_id.startswith("\x00")

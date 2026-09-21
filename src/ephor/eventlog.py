"""Append-only log of what happened to a session, and to its work.

The dashboard is a live view: it tells you what is true right now and forgets
everything else. A session that finishes, gets its PR merged and exits takes
its whole story with it, which makes the most ordinary morning question —
*what did the nine sessions I left running overnight actually do?* —
unanswerable from anything ephor keeps.

This is the missing tape. One line of JSON per state change, appended
forever, queryable by ticket or by session. It follows the pattern
:mod:`ephor.speech` already established for TTS events, for the same reasons:
an append-only NDJSON file needs no schema migration, no lock (a line under
``PIPE_BUF`` written to an ``O_APPEND`` fd is atomic on Linux), and no daemon
to own it.

The TUI is the writer. It is the only component that sees both the previous
and the current state of every session on each refresh, so it is the only one
that can tell a *change* from a repaint — and logging repaints at 2 Hz would
bury the signal under half a million identical lines a day.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ephor.config import state_dir

# Rotate at 8 MiB — roughly a year of a busy fleet's transitions, and small
# enough that reading the whole file back to answer `ephor log DR-8222`
# stays instant. One generation of history is kept.
MAX_LOG_BYTES = 8 * 1024 * 1024


class EventKind(StrEnum):
    """What kind of change a line records."""

    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    STATUS_CHANGED = "status_changed"
    TICKET_BOUND = "ticket_bound"
    PR_LINKED = "pr_linked"
    PR_STATE_CHANGED = "pr_state_changed"
    CI_CHANGED = "ci_changed"
    REVIEW_CHANGED = "review_changed"
    PERMISSION_DECIDED = "permission_decided"
    NOTIFIED = "notified"


@dataclass(frozen=True)
class Event:
    """One logged change."""

    kind: EventKind
    at: str  # ISO-8601 UTC
    session_id: str = ""
    ticket: str = ""
    detail: str = ""
    extra: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event | None:
        """Parse a stored line, or None when it is not one of ours."""
        try:
            kind = EventKind(str(data["kind"]))
        except (KeyError, ValueError):
            return None
        extra = data.get("extra")
        return cls(
            kind=kind,
            at=str(data.get("at") or ""),
            session_id=str(data.get("session_id") or ""),
            ticket=str(data.get("ticket") or ""),
            detail=str(data.get("detail") or ""),
            extra=extra if isinstance(extra, dict) else None,
        )


def log_path() -> Path:
    """Where the event log lives (beside the session state dir)."""
    return state_dir().parent / "events.ndjson"


def _rotate_if_needed(path: Path) -> None:
    """Move the log aside once it passes the cap, keeping one generation."""
    try:
        if path.stat().st_size < MAX_LOG_BYTES:
            return
        os.replace(path, path.with_suffix(".ndjson.1"))
    except OSError:
        return


def append(
    kind: EventKind,
    *,
    session_id: str = "",
    ticket: str = "",
    detail: str = "",
    **extra: Any,
) -> bool:
    """Record one change. False on any failure — never raises at the caller.

    Newlines in ``detail`` would split one event into two unparseable lines,
    so they are flattened rather than escaped: this field is a human-readable
    label, and a label does not need to survive a round-trip verbatim.
    """
    path = log_path()
    payload: dict[str, Any] = {
        "kind": str(kind),
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "session_id": session_id,
        "ticket": ticket,
        "detail": " ".join(detail.split()),
    }
    if extra:
        payload["extra"] = extra
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        _rotate_if_needed(path)
        line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError):
        return False
    return True


def read(
    *,
    ticket: str | None = None,
    session_id: str | None = None,
    since_sec: float | None = None,
    limit: int | None = None,
) -> list[Event]:
    """Logged events, oldest first, filtered by ticket / session / age.

    Reads the rotated generation first so a query that spans a rotation still
    returns a contiguous story rather than starting abruptly mid-morning.
    ``limit`` keeps the *newest* N, because that is what every caller means.
    """
    cutoff: str | None = None
    if since_sec is not None:
        cutoff = datetime.fromtimestamp(time.time() - since_sec, UTC).isoformat(timespec="seconds")
    want_ticket = ticket.upper() if ticket else None

    events: list[Event] = []
    path = log_path()
    for candidate in (path.with_suffix(".ndjson.1"), path):
        try:
            raw = candidate.read_text()
        except OSError:
            continue
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            event = Event.from_dict(data)
            if event is None:
                continue
            if want_ticket and event.ticket.upper() != want_ticket:
                continue
            if session_id and event.session_id != session_id:
                continue
            # ISO-8601 UTC strings with a fixed offset sort lexicographically
            # in time order, so this comparison needs no parsing.
            if cutoff and event.at < cutoff:
                continue
            events.append(event)
    if limit is not None and limit >= 0:
        return events[-limit:]
    return events

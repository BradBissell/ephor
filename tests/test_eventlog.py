"""Event log: append semantics, filtering, and rotation."""

from __future__ import annotations

from ephor import eventlog
from ephor.eventlog import EventKind


def test_append_and_read_round_trip() -> None:
    eventlog.append(EventKind.SESSION_STARTED, session_id="s1", ticket="DR-1", detail="start")
    eventlog.append(EventKind.PR_LINKED, session_id="s1", ticket="DR-1", detail="#12", url="u")

    events = eventlog.read()
    assert [e.kind for e in events] == [EventKind.SESSION_STARTED, EventKind.PR_LINKED]
    assert events[1].extra == {"url": "u"}


def test_filters_by_ticket_and_session() -> None:
    eventlog.append(EventKind.STATUS_CHANGED, session_id="s1", ticket="DR-1")
    eventlog.append(EventKind.STATUS_CHANGED, session_id="s2", ticket="DR-2")

    assert [e.ticket for e in eventlog.read(ticket="DR-2")] == ["DR-2"]
    assert [e.session_id for e in eventlog.read(session_id="s1")] == ["s1"]
    # Ticket matching is case-insensitive; the harvester's casing varies.
    assert len(eventlog.read(ticket="dr-1")) == 1


def test_limit_keeps_the_newest() -> None:
    for i in range(5):
        eventlog.append(EventKind.STATUS_CHANGED, detail=f"e{i}")
    assert [e.detail for e in eventlog.read(limit=2)] == ["e3", "e4"]


def test_newlines_in_detail_cannot_split_a_line() -> None:
    """A two-line detail would otherwise write an unparseable second line."""
    eventlog.append(EventKind.STATUS_CHANGED, detail="first\nsecond\tthird")
    events = eventlog.read()
    assert len(events) == 1
    assert events[0].detail == "first second third"
    assert eventlog.log_path().read_text().count("\n") == 1


def test_unparseable_lines_are_skipped_not_fatal() -> None:
    eventlog.append(EventKind.STATUS_CHANGED, detail="good")
    with eventlog.log_path().open("a") as fh:
        fh.write("not json\n")
        fh.write('{"kind": "no-such-kind"}\n')
    assert [e.detail for e in eventlog.read()] == ["good"]


def test_read_spans_the_rotated_generation(monkeypatch) -> None:
    """One rotation must not create a gap in the middle of the story.

    Only one generation is kept, so a log that rotates repeatedly does lose
    its oldest events — that is the cap doing its job. What must hold is that
    a *single* rotation leaves the events either side of it readable in one
    contiguous, time-ordered sequence.
    """
    for i in range(3):
        eventlog.append(EventKind.STATUS_CHANGED, detail=f"old{i}")
    # Force exactly one rotation, then write past it.
    monkeypatch.setattr(eventlog, "MAX_LOG_BYTES", 1)
    eventlog.append(EventKind.STATUS_CHANGED, detail="new0")
    monkeypatch.setattr(eventlog, "MAX_LOG_BYTES", 8 * 1024 * 1024)
    eventlog.append(EventKind.STATUS_CHANGED, detail="new1")

    assert eventlog.log_path().with_suffix(".ndjson.1").exists()
    assert [e.detail for e in eventlog.read()] == ["old0", "old1", "old2", "new0", "new1"]


def test_log_file_is_private() -> None:
    eventlog.append(EventKind.STATUS_CHANGED)
    assert eventlog.log_path().stat().st_mode & 0o777 == 0o600

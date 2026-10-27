"""Event log: append semantics, filtering, and rotation."""

from __future__ import annotations

import os
from pathlib import Path

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


def test_new_flow_kinds_round_trip() -> None:
    eventlog.append(
        EventKind.FLOW_DISPATCHED,
        ticket="DR-1",
        detail="review",
        run_id="DR-1.review.10001-1",
        flow="review",
        generation="10001-1",
        reason="",
    )
    eventlog.append(EventKind.TICKET_TRANSITIONED, ticket="DR-1", detail="In Progress -> Review")
    kinds = [e.kind for e in eventlog.read(ticket="DR-1")]
    assert kinds == [EventKind.FLOW_DISPATCHED, EventKind.TICKET_TRANSITIONED]
    assert {str(k) for k in kinds} == {"flow_dispatched", "ticket_transitioned"}
    for kind in ("flow_blocked", "flow_finished", "flow_cancelled"):
        assert EventKind(kind)


def test_rotation_and_append_happen_under_the_sidecar_lock(monkeypatch) -> None:
    """Two writers both rotating would discard the kept generation."""
    from ephor.filelock import locked

    seen: list[bool] = []
    real_rotate = eventlog._rotate_if_needed

    def spying_rotate(path):
        with locked(eventlog._lock_path(path), timeout=0) as free:
            seen.append(free)
        real_rotate(path)

    monkeypatch.setattr(eventlog, "_rotate_if_needed", spying_rotate)
    assert eventlog.append(EventKind.STATUS_CHANGED, detail="x") is True
    assert seen == [False]
    assert eventlog.log_path().with_suffix(".ndjson.lock").exists()


def test_a_second_writer_cannot_rotate_while_the_first_is_mid_rotation(monkeypatch) -> None:
    """Without the lock, B rotates under A and A's rename then discards the kept generation.

    A is parked between its size check and its rename. B, started while A is
    parked, would (unlocked) see the same oversized log, move it aside and
    write a fresh one — and A's rename would then replace the kept
    generation with B's one-line file. With the lock, B waits for A.
    """
    import threading

    monkeypatch.setattr(eventlog, "MAX_LOG_BYTES", 400)
    path = eventlog.log_path()
    while not path.exists() or path.stat().st_size < 400:
        eventlog.append(EventKind.STATUS_CHANGED, detail="before-rotation")
    original_size = path.stat().st_size

    # A generous wait for this test only, so B is still queued (not failed
    # open) when A is released however slow the machine is. If append()
    # stops taking the lock at all, this wrapper is never reached.
    real_locked = eventlog.locked
    monkeypatch.setattr(eventlog, "locked", lambda p, timeout=None: real_locked(p, timeout=5.0))

    entered, go = threading.Event(), threading.Event()
    first = [True]
    real_rotate = eventlog._rotate_if_needed

    def gated_rotate(target: Path) -> None:
        if not first[0]:
            real_rotate(target)
            return
        first[0] = False
        if target.stat().st_size >= eventlog.MAX_LOG_BYTES:
            entered.set()
            go.wait(timeout=1)
            os.replace(target, target.with_suffix(".ndjson.1"))

    monkeypatch.setattr(eventlog, "_rotate_if_needed", gated_rotate)
    writer_a = threading.Thread(
        target=eventlog.append, args=(EventKind.STATUS_CHANGED,), kwargs={"detail": "A"}
    )
    writer_b = threading.Thread(
        target=eventlog.append, args=(EventKind.STATUS_CHANGED,), kwargs={"detail": "B"}
    )
    writer_a.start()
    assert entered.wait(timeout=2)
    writer_b.start()
    writer_b.join(timeout=0.2)  # unlocked, B would finish its rotation here
    go.set()
    writer_a.join(timeout=5)
    writer_b.join(timeout=5)

    rotated = path.with_suffix(".ndjson.1")
    assert rotated.stat().st_size >= original_size
    assert "before-rotation" in rotated.read_text()
    assert {e.detail for e in eventlog.read() if e.detail in {"A", "B"}} == {"A", "B"}


def test_a_stuck_lock_holder_delays_an_append_only_briefly() -> None:
    """The TUI appends on its event-loop thread; a stopped holder must not freeze it."""
    import fcntl
    import time

    path = eventlog.log_path()
    lock = eventlog._lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        started = time.monotonic()
        assert eventlog.append(EventKind.STATUS_CHANGED, detail="through") is True
        assert time.monotonic() - started < 1.0
    finally:
        os.close(fd)
    assert [e.detail for e in eventlog.read()] == ["through"]

"""Advisory file locks for the stores ephor writes from more than one process.

For most of ephor's life the TUI was the only process that wrote work
records and event-log lines, so neither store needed a lock: a later write
simply won, and a single writer cannot race itself across processes. The
flows tick breaks that assumption. ``ephor flows tick`` runs from a TUI
worker thread, from ``ephor flows watch`` in another pane, or from a systemd
timer, and every one of them records work and appends events while the
dashboard is doing the same.

Two read-modify-write sequences are actually exposed to that:

* :func:`ephor.work_items.record` loads a record, merges one field, and
  writes it back. Two writers interleaving there lose one writer's field.
* :func:`ephor.eventlog.append` checks the log's size and rotates it. Two
  writers that both see the log over the cap both rotate, and the second
  ``rename`` throws away the whole generation the first one kept.

This module is the one ``flock(2)`` wrapper both use, plus the non-blocking
form the tick takes so that a second tick returns at once instead of queueing
behind the first.

A lock is a *sidecar* file, never the data file itself: the data files are
replaced by atomic rename, and a lock held on an inode that has just been
renamed away protects nothing.

flock locks belong to an open file description, not a process, so two
threads that each open the sidecar also exclude each other — the TUI's
worker threads get the same protection as separate processes.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from collections.abc import Iterator
from pathlib import Path

# How long a writer waits before giving up. Every critical section guarded
# here is a few file operations long, so a quarter of a second means
# something is wedged (a holder stopped with Ctrl-Z, or paused in a
# debugger), and the caller is better off proceeding than waiting. The TUI
# calls these writers on its event-loop thread, once per event, so this is
# also the most one write can freeze the dashboard; the flows tick takes its
# own lock with timeout=0 and never waits here.
DEFAULT_TIMEOUT_SEC = 0.25
_POLL_SEC = 0.01


@contextlib.contextmanager
def locked(path: Path, *, timeout: float | None = DEFAULT_TIMEOUT_SEC) -> Iterator[bool]:
    """Hold an exclusive flock on ``path`` for the ``with`` body.

    Yields True when the lock was taken and False when it was not — because
    the sidecar could not be created, or another holder kept it past
    ``timeout``. ``timeout=0`` is a single non-blocking attempt; ``None``
    waits forever.

    The body runs either way. Callers decide what an unlocked run means: the
    work-item and event-log writers proceed (their data is a convenience that
    must never take the dashboard down), while the flows tick returns without
    doing anything.
    """
    fd: int | None = None
    acquired = False
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        fd = None
    if fd is not None:
        acquired = _acquire(fd, timeout)
    try:
        yield acquired
    finally:
        if fd is not None:
            if acquired:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _acquire(fd: int, timeout: float | None) -> bool:
    if timeout is None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            return False
        return True
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_SEC)
            continue
        except OSError:
            return False
        return True

"""Sidecar flock helper: exclusion, timeouts, and fail-open on a bad path."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from ephor.filelock import locked


def test_lock_is_taken_and_released(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "x.lock"
    with locked(path) as got:
        assert got is True
        assert path.exists()
        assert path.stat().st_mode & 0o777 == 0o600
    # Released: a non-blocking attempt succeeds straight away.
    with locked(path, timeout=0) as again:
        assert again is True


def test_a_held_lock_excludes_a_second_holder(tmp_path: Path) -> None:
    """flock is per open file description, so this holds across threads too."""
    path = tmp_path / "x.lock"
    with locked(path) as outer:
        assert outer is True
        with locked(path, timeout=0) as inner:
            assert inner is False
        started = time.monotonic()
        with locked(path, timeout=0.05) as waited:
            assert waited is False
        assert time.monotonic() - started >= 0.04


def test_a_waiter_gets_the_lock_once_it_is_released(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    release = threading.Event()
    holding = threading.Event()

    def hold() -> None:
        with locked(path):
            holding.set()
            release.wait(2)

    thread = threading.Thread(target=hold)
    thread.start()
    holding.wait(2)
    threading.Timer(0.05, release.set).start()
    with locked(path, timeout=2) as got:
        assert got is True
    thread.join()


def test_an_unusable_path_yields_false_and_still_runs_the_body(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    ran = False
    with locked(blocker / "x.lock") as got:
        ran = True
        assert got is False
    assert ran

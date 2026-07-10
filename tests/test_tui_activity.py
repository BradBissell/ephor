"""Tests for the per-session CPU activity sampler.

Linux-only — skipped on platforms without /proc/<pid>/stat.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from ephor.tui.activity import (
    WINDOW,
    ActivitySampler,
    _read_cpu_jiffies,
)

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires /proc")


def test_read_cpu_jiffies_for_self_returns_int() -> None:
    j = _read_cpu_jiffies(os.getpid())
    assert j is not None
    assert j >= 0


def test_read_cpu_jiffies_for_missing_pid_returns_none() -> None:
    # PID 1 always exists; pick something that almost certainly doesn't.
    assert _read_cpu_jiffies(2_000_000_001) is None


def test_sampler_first_sample_is_baseline_only() -> None:
    s = ActivitySampler()
    s.sample(os.getpid())
    # No delta computed yet — caller gets nothing to plot.
    assert s.samples_for(os.getpid()) == []


def test_sampler_records_after_two_samples() -> None:
    s = ActivitySampler()
    pid = os.getpid()
    s.sample(pid)
    # Burn a tiny bit of CPU so the delta isn't zero on extremely idle systems.
    end = time.monotonic() + 0.05
    while time.monotonic() < end:
        pass
    s.sample(pid)
    samples = s.samples_for(pid)
    assert len(samples) == 1
    assert 0.0 <= samples[0] <= 1.0


def test_sampler_clamps_to_one() -> None:
    """Synthetic test: feed an impossibly-large jiffy delta, expect clamp to 1.0."""
    s = ActivitySampler()
    pid = 12345
    # Inject a fake "previous" sample, then patch _read_cpu_jiffies to force a huge delta.
    s._last[pid] = (time.monotonic() - 0.5, 0)
    import ephor.tui.activity as activity_module

    real = activity_module._read_cpu_jiffies
    activity_module._read_cpu_jiffies = lambda _pid: 10**9  # type: ignore[assignment]
    try:
        s.sample(pid)
    finally:
        activity_module._read_cpu_jiffies = real
    assert s.samples_for(pid) == [1.0]


def test_sampler_prunes_dead_pids() -> None:
    s = ActivitySampler()
    s._last[111] = (0.0, 0)
    s._buf[111] = __import__("collections").deque([0.5], maxlen=WINDOW)
    s._last[222] = (0.0, 0)
    s.prune(live_pids=[222])
    assert 111 not in s._last
    assert 111 not in s._buf
    assert 222 in s._last


def test_samples_for_none_returns_empty() -> None:
    s = ActivitySampler()
    assert s.samples_for(None) == []


def test_window_matches_sparkline_width() -> None:
    """If these drift apart, the sparkline column visibly stops filling."""
    from ephor.tui.widgets.session_row import _SPARK_WIDTH

    assert WINDOW == _SPARK_WIDTH

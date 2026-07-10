"""Per-session CPU activity sampler for the sparkline column.

Samples /proc/<agent_pid>/stat on each TUI refresh tick. Each sample is
the fraction of one CPU core consumed since the previous tick — multi-core
processes saturate at 1.0. The TUI keeps a ring buffer per pid; SessionRow
maps it to ▁▂▃▄▅▆▇█ glyphs.

Linux-only. Read failures (pid gone, /proc not mounted) silently drop the
buffer — sparkline just goes empty.
"""

from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Iterable
from pathlib import Path

# Must match SessionRow's _SPARK_WIDTH so the buffer fills the column.
WINDOW = 16

# clock ticks per second on Linux; used to convert /proc jiffies to seconds.
_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _read_cpu_jiffies(pid: int) -> int | None:
    """Sum of utime + stime from /proc/<pid>/stat, or None if unreadable.

    The comm field (2nd) is wrapped in parens and can contain spaces, so we
    slice past the rightmost ')' to get a clean field list.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    rparen = raw.rfind(")")
    if rparen < 0:
        return None
    fields = raw[rparen + 2 :].split()
    # After comm: state + 12 numeric fields → utime at index 11, stime at 12.
    if len(fields) < 13:
        return None
    try:
        return int(fields[11]) + int(fields[12])
    except ValueError:
        return None


class ActivitySampler:
    """Ring buffer of [0, 1] CPU samples keyed by agent_pid."""

    def __init__(self, window: int = WINDOW) -> None:
        self._window = window
        self._last: dict[int, tuple[float, int]] = {}  # pid → (wall_time, jiffies)
        self._buf: dict[int, deque[float]] = {}

    def sample(self, pid: int) -> None:
        """Record one sample for pid. First call establishes baseline only."""
        now = time.monotonic()
        jiffies = _read_cpu_jiffies(pid)
        if jiffies is None:
            self._last.pop(pid, None)
            return
        prev = self._last.get(pid)
        self._last[pid] = (now, jiffies)
        if prev is None:
            return  # baseline; need a second tick to compute a delta
        prev_t, prev_j = prev
        dt = max(now - prev_t, 1e-3)
        cpu_seconds = max(jiffies - prev_j, 0) / _CLK_TCK
        frac = cpu_seconds / dt
        if frac > 1.0:
            frac = 1.0
        self._buf.setdefault(pid, deque(maxlen=self._window)).append(frac)

    def samples_for(self, pid: int | None) -> list[float]:
        if pid is None:
            return []
        return list(self._buf.get(pid, ()))

    def prune(self, live_pids: Iterable[int]) -> None:
        """Drop buffers for pids no longer live, so dead sessions don't leak memory."""
        live = set(live_pids)
        for pid in list(self._last):
            if pid not in live:
                self._last.pop(pid, None)
                self._buf.pop(pid, None)

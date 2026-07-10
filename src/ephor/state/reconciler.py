"""Periodic state-file reconciliation.

Cleans up two kinds of drift between on-disk state and reality:

1. **Orphaned state files** — agent_pid is dead AND last_event_time is older
   than RECONCILE_FILE_STALE_SEC. The process is gone; nothing else is going
   to write to this file, so unlink it. Without this, files accumulate one
   per crashed/closed session forever.

2. **Stuck WAITING_PERMISSION / WAITING_ANSWER** — the race documented by
   the clorch reference: PermissionRequest does NOT fire Stop on denial,
   so the file stays WAITING_PERMISSION even after the user denies and the
   claude process exits. Reset to IDLE only when agent_pid is confirmed
   dead — never overwrite a legitimately pending prompt on a live process.

Run on a slow timer (~30s) from the TUI or the CLI. Cheap: a stat per file,
plus a kill(pid, 0) per file with a recorded pid. Skips its own atomic-write
tempfiles. Atomic-rename writes mean a concurrent hook write can't corrupt
state — worst case, one of the two writes wins.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ephor.config import (
    RECONCILE_FILE_STALE_SEC,
)
from ephor.constants import AgentStatus
from ephor.state.manager import _is_pid_alive

log = logging.getLogger(__name__)

# Which on-disk status strings count as "stuck waiting" — only these get
# reset to IDLE on dead-pid reconciliation. Comparing to the StrEnum's
# .value because the on-disk JSON is plain strings.
_WAITING_STATUSES: frozenset[str] = frozenset(
    {AgentStatus.WAITING_PERMISSION.value, AgentStatus.WAITING_ANSWER.value}
)


@dataclass(frozen=True)
class ReconcileResult:
    deleted: int
    reset: int

    @property
    def changed(self) -> bool:
        return self.deleted > 0 or self.reset > 0


def _last_event_age_seconds(data: dict[str, Any], fallback_path: Path) -> float:
    """Seconds since the recorded last_event_time, falling back to file mtime
    when the timestamp is missing or unparseable."""
    raw = data.get("last_event_time")
    if isinstance(raw, str) and raw:
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return max(0.0, (datetime.now(UTC) - ts).total_seconds())
        except ValueError:
            pass
    try:
        return max(0.0, time.time() - fallback_path.stat().st_mtime)
    except OSError:
        return 0.0


def _atomic_write(path: Path, data: dict[str, Any]) -> bool:
    """Write `data` to `path` via tempfile + rename. Returns success."""
    tmp = path.with_name(f".tmp.reconcile.{path.name}")
    try:
        tmp.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")))
        tmp.chmod(0o600)
        tmp.replace(path)
        return True
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False


def reconcile(
    state_dir: Path,
    file_stale_sec: int = RECONCILE_FILE_STALE_SEC,
) -> ReconcileResult:
    """Single reconciliation pass over `state_dir`. Idempotent.

    Files with dead `agent_pid`:
      - Deleted if `last_event_time` > `file_stale_sec` ago.
      - Otherwise, if status is WAITING_PERMISSION or WAITING_ANSWER, reset
        to IDLE (clears the stuck-prompt indicator the dashboard would
        otherwise highlight forever).

    Files sharing a `agent_pid` (``claude --resume`` residue: same parent
    process, fresh session_id):
      - The most-recently-active sibling per pid is the real session.
      - Older siblings are deleted if they've been idle for >file_stale_sec.

    Files with live or missing `agent_pid` are otherwise left alone — the
    live ones are healthy, the missing-pid ones predate hook coverage and
    require a different recovery path (manual cleanup or `ephor refresh-tmux`).
    """
    if not state_dir.is_dir():
        return ReconcileResult(deleted=0, reset=0)

    deleted = 0
    reset = 0

    # First pass — load everything so we can identify shared-PID groups.
    # The set is tiny (one entry per active session), so the extra read
    # is negligible vs the simplicity of grouping in-memory.
    paths_data: list[tuple[Path, dict[str, Any]]] = []
    for path in state_dir.glob("*.json"):
        if path.name.startswith(".tmp"):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        paths_data.append((path, data))

    # Identify the "winning" file per live pid: most recent last_event_time.
    # Anything else sharing that pid is resume residue.
    winners_by_pid: dict[int, tuple[Path, str]] = {}
    for path, data in paths_data:
        pid = data.get("agent_pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        if not _is_pid_alive(pid):
            continue
        ts = str(data.get("last_event_time") or "")
        existing = winners_by_pid.get(pid)
        if existing is None or ts > existing[1]:
            winners_by_pid[pid] = (path, ts)

    for path, data in paths_data:
        pid = data.get("agent_pid")
        if not isinstance(pid, int) or pid <= 0:
            continue

        if _is_pid_alive(pid):
            # Resume-residue check: a non-winning sibling for a live pid
            # is an orphan — delete after the standard staleness grace.
            winner = winners_by_pid.get(pid)
            if winner is not None and winner[0] != path:
                age = _last_event_age_seconds(data, path)
                if age > file_stale_sec:
                    try:
                        path.unlink()
                        deleted += 1
                    except OSError as exc:
                        log.warning("reconcile: could not unlink %s: %s", path, exc)
            continue

        age = _last_event_age_seconds(data, path)
        if age > file_stale_sec:
            try:
                path.unlink()
                deleted += 1
            except OSError as exc:
                log.warning("reconcile: could not unlink %s: %s", path, exc)
            continue

        status = data.get("status")
        if status in _WAITING_STATUSES:
            data["status"] = AgentStatus.IDLE.value
            data["notification"] = None
            if _atomic_write(path, data):
                reset += 1

    if deleted or reset:
        log.info("reconcile: deleted=%d reset=%d", deleted, reset)
    return ReconcileResult(deleted=deleted, reset=reset)

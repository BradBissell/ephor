"""Read-only StateManager — scans state files on disk.

Errors during scan are logged but never raise: a corrupt file should not
crash the CLI / TUI when 29 other sessions are working fine.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ephor.config import state_dir
from ephor.constants import AgentStatus
from ephor.state.models import AgentState, StatusSummary

log = logging.getLogger(__name__)


def _is_pid_alive(pid: int) -> bool:
    """True iff the given pid currently exists. Sends signal 0 (no-op delivery)
    purely as an existence test. PermissionError = pid exists but we can't
    signal it (another user) — still alive for our purposes."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class StateManager:
    """Scans the state directory and returns AgentState objects."""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory if directory is not None else state_dir()

    @property
    def directory(self) -> Path:
        return self._dir

    def scan(self) -> list[AgentState]:
        """Read every `*.json` file in the state dir.

        Sorted by `last_event_time` descending (most-recently-active first)
        so the user sees what changed most recently at the top of `ephor list`.
        Corrupt files are logged at WARNING and skipped.

        Resume dedup: ``claude --resume`` reuses the parent process's PID
        but creates a new session_id, so each resume leaves an orphaned
        state file that points at a still-alive PID. Stale-PID detection
        (the existing ``_is_pid_alive`` check) can't catch this: BOTH
        files reference the same live PID. We resolve the duplicate by
        keeping the most-recently-active sibling and marking the rest
        DEAD so the dashboard renders one row per live process.
        """
        if not self._dir.is_dir():
            return []

        agents: list[AgentState] = []
        for path in self._dir.glob("*.json"):
            # Skip atomic-write tempfiles (defensive — they shouldn't match *.json
            # since we use .tmp.XXXXXX prefix without .json suffix, but be safe).
            if path.name.startswith(".tmp"):
                continue
            try:
                agent = AgentState.from_json_file(path)
            except (OSError, ValueError, KeyError) as exc:
                log.warning("Skipping corrupt state file %s: %s", path, exc)
                continue
            # In-memory liveness check: when the state file recorded a
            # agent_pid (the handler walks up to find it) and that pid is
            # no longer running, the session is dead. Mark it DEAD here
            # so the dashboard distinguishes ghosts from live sessions.
            # State on disk is intentionally NOT mutated — reconciliation
            # (P5) will write back if/when implemented.
            if agent.agent_pid and not _is_pid_alive(agent.agent_pid):
                agent.status = AgentStatus.DEAD
            agents.append(agent)

        agents.sort(key=lambda a: a.last_event_time, reverse=True)
        _mark_resume_residue_dead(agents)
        return agents

    def get_summary(self) -> StatusSummary:
        return StatusSummary.from_agents(self.scan())


def _mark_resume_residue_dead(agents: list[AgentState]) -> None:
    """Mark older siblings sharing a live ``agent_pid`` as DEAD.

    Mutates ``agents`` in place. Caller must pre-sort by
    ``last_event_time`` descending — the first agent encountered for a
    given pid is treated as the live session, all others are
    resume-residue and get the DEAD treatment so the dashboard hides
    them. Disk-level cleanup happens in the reconciler on a longer
    timer; this function is purely a render-time fix.

    Agents already marked DEAD (dead PID) and agents without a
    ``agent_pid`` are left untouched.
    """
    seen_live_pids: set[int] = set()
    for agent in agents:
        if agent.status == AgentStatus.DEAD:
            continue
        pid = agent.agent_pid
        if not pid:
            continue
        if pid in seen_live_pids:
            agent.status = AgentStatus.DEAD
            continue
        seen_live_pids.add(pid)

"""Status enum and display helpers."""

from __future__ import annotations

from enum import StrEnum


class AgentStatus(StrEnum):
    """Status of a Claude Code session as tracked by ephor.

    Values are wire-stable strings (used in JSON state files); do not rename
    without bumping SCHEMA_VERSION in config.py.
    """

    WORKING = "WORKING"
    IDLE = "IDLE"
    WAITING_PERMISSION = "WAITING_PERMISSION"
    WAITING_ANSWER = "WAITING_ANSWER"
    ERROR = "ERROR"
    DEAD = "DEAD"


# (symbol, short label, hex color) — used by `ephor list` and the TUI.
STATUS_DISPLAY: dict[AgentStatus, tuple[str, str, str]] = {
    AgentStatus.WORKING: (">>>", "WORK", "#A3BE8C"),
    AgentStatus.IDLE: ("---", "IDLE", "#616E88"),
    AgentStatus.WAITING_PERMISSION: ("[!]", "PERM", "#BF616A"),
    AgentStatus.WAITING_ANSWER: ("[?]", "WAIT", "#EBCB8B"),
    AgentStatus.ERROR: ("[X]", "ERR ", "#B48EAD"),
    AgentStatus.DEAD: ("___", "DEAD", "#3B4252"),
}

ATTENTION_STATUSES = frozenset(
    {AgentStatus.WAITING_PERMISSION, AgentStatus.WAITING_ANSWER, AgentStatus.ERROR}
)

# How long a session can stay in WORKING with no hook activity before the
# dashboard tags it STALE. Catches sessions where agent_pid is alive but
# spinning on a model timeout / network hang — the PID liveness check
# misses these. Render-time only, no on-disk state.
STALE_HEARTBEAT_SEC = 60

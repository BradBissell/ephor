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


class CiState(StrEnum):
    """Rolled-up CI verdict for a pull request's head commit.

    Derived from ``statusCheckRollup``; ``NONE`` means the PR has no checks
    at all, which is different from ``PENDING`` (checks exist, still running).
    """

    PASSING = "PASSING"
    FAILING = "FAILING"
    PENDING = "PENDING"
    NONE = "NONE"


class ReviewState(StrEnum):
    """Human-review verdict for a pull request.

    Mirrors GitHub's ``reviewDecision`` plus a ``NONE`` for PRs that have
    neither a decision nor a requested reviewer — nobody has been asked yet.
    """

    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    NONE = "NONE"


class WorkAttention(StrEnum):
    """Why a session's *work* needs a human, independent of its agent status.

    The agent-status machine only knows what the coding agent is doing right
    now. These states come from the pull request instead, and they are the
    reason a row can be the most urgent thing on the board while its session
    sits quietly in ``IDLE`` — a finished session that left red CI behind is
    exactly that.
    """

    CI_FAILED = "CI_FAILED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    CONFLICT = "CONFLICT"
    REVIEW_REQUESTED = "REVIEW_REQUESTED"
    TICKET_DRIFT = "TICKET_DRIFT"


# (symbol, short label, hex color) — parallel to STATUS_DISPLAY, for the
# work-state cell on the session row and for `ephor list`.
WORK_ATTENTION_DISPLAY: dict[WorkAttention, tuple[str, str, str]] = {
    WorkAttention.CI_FAILED: ("x", "CI", "#BF616A"),
    WorkAttention.CHANGES_REQUESTED: ("!", "CHG", "#D08770"),
    WorkAttention.CONFLICT: ("><", "CONF", "#BF616A"),
    WorkAttention.REVIEW_REQUESTED: ("?", "RVW", "#EBCB8B"),
    WorkAttention.TICKET_DRIFT: ("~", "DRIFT", "#B48EAD"),
}

# Ordered most- to least-urgent. The row shows the first one that applies and
# `n` (next-attention) walks them in this order.
WORK_ATTENTION_PRIORITY: tuple[WorkAttention, ...] = (
    WorkAttention.CI_FAILED,
    WorkAttention.CONFLICT,
    WorkAttention.CHANGES_REQUESTED,
    WorkAttention.REVIEW_REQUESTED,
    WorkAttention.TICKET_DRIFT,
)

# How long a session can stay in WORKING with no hook activity before the
# dashboard tags it STALE. Catches sessions where agent_pid is alive but
# spinning on a model timeout / network hang — the PID liveness check
# misses these. Render-time only, no on-disk state.
STALE_HEARTBEAT_SEC = 60

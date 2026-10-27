"""Name a session's dashboard row from inside the session.

`ephor title` runs as a child of the agent it names — typically from a slash
command, where the agent's own model writes the title. The title lands in the
same summary sidecar the dashboard summarizer writes, so the row picks it up
on its next refresh.

The calling session is found the way the hook handler records it: the agent
process is an ancestor of this one, and its PID is the state file's
`agent_pid`. That works for every provider without provider-specific env.
Claude's `CLAUDE_CODE_SESSION_ID` and the tmux pane are fallbacks.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from ephor.state.models import AgentState
from ephor.summarizer import MAX_LENGTH

# Deeper than the hook's own walk: a slash command's shell may sit under a
# tool runner, a login shell and `uv run` before it reaches the agent.
_MAX_ANCESTOR_DEPTH = 16


def _read_ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", encoding="ascii", errors="ignore") as f:
            for line in f:
                if line.startswith("PPid:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return int(parts[1])
                    return None
    except OSError:
        return None
    return None


def ancestor_pids(pid: int | None = None) -> list[int]:
    """Parent, grandparent, … of `pid` (default: this process), nearest first."""
    current = os.getpid() if pid is None else pid
    ancestors: list[int] = []
    for _ in range(_MAX_ANCESTOR_DEPTH):
        parent = _read_ppid(current)
        if parent is None or parent <= 1:
            break
        ancestors.append(parent)
        current = parent
    return ancestors


def resolve_current_session(
    agents: Sequence[AgentState],
    *,
    ancestors: Sequence[int],
    env: Mapping[str, str],
) -> AgentState | None:
    """Pick the session this process runs under, or None.

    `agents` is expected most-recently-active first (as `StateManager.scan`
    returns it), so a resumed session wins over the stale file it left behind.
    """
    ancestor_set = set(ancestors)
    for agent in agents:
        if agent.agent_pid is not None and agent.agent_pid in ancestor_set:
            return agent

    claude_sid = env.get("CLAUDE_CODE_SESSION_ID", "")
    if claude_sid:
        for agent in agents:
            if agent.session_id == claude_sid:
                return agent

    pane = env.get("TMUX_PANE", "")
    if pane:
        for agent in agents:
            if agent.tmux_pane == pane:
                return agent
    return None


def normalize_title(raw: str) -> str:
    """One line, single-spaced, capped at the summary column's length."""
    return " ".join(raw.split())[:MAX_LENGTH].rstrip()

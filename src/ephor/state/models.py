"""AgentState — the on-disk schema for per-session state files.

Schema-versioned; readers MUST check `schema_version` before interpreting
fields. Writers MUST emit the current SCHEMA_VERSION.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ephor.config import KNOWN_SCHEMA_VERSIONS, SCHEMA_VERSION
from ephor.constants import AgentStatus


@dataclass(frozen=False)
class Notification:
    """Permission/question notification details, populated when status is WAITING_*."""

    type: str  # "permission" | "question"
    tool: str | None = None
    redacted_summary: str | None = None  # never include tool args / prompt text


@dataclass(frozen=False)
class AgentState:
    """Per-session state. Mirrors the on-disk JSON file 1:1."""

    session_id: str
    cwd: str
    started_at: str  # ISO-8601 UTC
    status: AgentStatus = AgentStatus.IDLE
    project_name: str = ""
    last_event: str = ""
    last_event_time: str = ""
    last_event_seq: int = 0
    tool_count: int = 0
    error_count: int = 0
    tmux_session: str | None = None
    tmux_window: str | None = None
    tmux_pane: str | None = None
    agent_pid: int | None = None  # walked up from hook's PPID; canonical for tmux mapping
    notification: Notification | None = None
    last_summary: str = ""  # v2: latest user prompt, truncated to 70 chars
    provider: str = ""  # v3: coding-agent CLI that wrote this file (claude/gemini/codex/grok)
    last_reply: str = ""  # v3: latest assistant reply captured at turn-end (non-Claude summaries)
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> str:
        """Serialise to a stable JSON string suitable for atomic-rename writes."""
        return json.dumps(_asdict_compact(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json_file(cls, path: Path) -> AgentState:
        """Read a state file; tolerate older schema versions where possible."""
        data = json.loads(path.read_text())
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentState:
        version = data.get("schema_version")
        if version is None:
            raise ValueError("state file missing schema_version — refusing to read pre-v1 file")
        if version not in KNOWN_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported schema_version={version} (expected {SCHEMA_VERSION})")

        notif_raw = data.get("notification")
        notif = Notification(**notif_raw) if notif_raw else None

        try:
            status = AgentStatus(data.get("status", "IDLE"))
        except ValueError:
            status = AgentStatus.IDLE

        # Migration notes:
        #   v1→v2: `last_summary` added; defaults empty for older files.
        #   v2→v3: `claude_pid` renamed to `agent_pid` and `provider` added.
        #          Read the legacy `claude_pid` key so pre-v3 files (or a
        #          stray old writer) still map their pid correctly.
        agent_pid = data.get("agent_pid")
        if agent_pid is None:
            agent_pid = data.get("claude_pid")
        return cls(
            session_id=data["session_id"],
            cwd=data["cwd"],
            started_at=data["started_at"],
            status=status,
            project_name=data.get("project_name", ""),
            last_event=data.get("last_event", ""),
            last_event_time=data.get("last_event_time", ""),
            last_event_seq=data.get("last_event_seq", 0),
            tool_count=data.get("tool_count", 0),
            error_count=data.get("error_count", 0),
            tmux_session=data.get("tmux_session"),
            tmux_window=data.get("tmux_window"),
            tmux_pane=data.get("tmux_pane"),
            agent_pid=agent_pid,
            notification=notif,
            last_summary=data.get("last_summary", ""),
            provider=data.get("provider", ""),
            last_reply=data.get("last_reply", ""),
            schema_version=version,
        )


def _asdict_compact(state: AgentState) -> dict[str, Any]:
    """Like dataclasses.asdict but coerces enums + drops None notification cleanly."""
    raw = asdict(state)
    raw["status"] = state.status.value
    if state.notification is None:
        raw["notification"] = None
    return raw


@dataclass(frozen=True)
class StatusSummary:
    """Aggregate counts across all known agents."""

    working: int = 0
    idle: int = 0
    waiting_permission: int = 0
    waiting_answer: int = 0
    error: int = 0
    dead: int = 0

    @property
    def total(self) -> int:
        return (
            self.working
            + self.idle
            + self.waiting_permission
            + self.waiting_answer
            + self.error
            + self.dead
        )

    @property
    def attention(self) -> int:
        return self.waiting_permission + self.waiting_answer + self.error

    @classmethod
    def from_agents(cls, agents: list[AgentState]) -> StatusSummary:
        counts: dict[AgentStatus, int] = dict.fromkeys(AgentStatus, 0)
        for a in agents:
            counts[a.status] += 1
        return cls(
            working=counts[AgentStatus.WORKING],
            idle=counts[AgentStatus.IDLE],
            waiting_permission=counts[AgentStatus.WAITING_PERMISSION],
            waiting_answer=counts[AgentStatus.WAITING_ANSWER],
            error=counts[AgentStatus.ERROR],
            dead=counts[AgentStatus.DEAD],
        )

    def status_line(self) -> str:
        """Compact one-line summary for `ephor status` / tmux widget."""
        parts: list[str] = []
        if self.attention:
            if self.waiting_permission:
                parts.append(f"P:{self.waiting_permission}")
            if self.waiting_answer:
                parts.append(f"Q:{self.waiting_answer}")
            if self.error:
                parts.append(f"E:{self.error}")
        if self.working:
            parts.append(f"W:{self.working}")
        if self.idle:
            parts.append(f"I:{self.idle}")
        return " ".join(parts) if parts else "—"

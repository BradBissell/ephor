"""Token-usage tracker for the dashboard summary line.

Parses Claude Code transcript jsonl files at ~/.claude/projects/<encoded-cwd>/<sid>.jsonl
and sums the per-turn `message.usage` totals (input + cache_creation + cache_read +
output) across all assistant messages.

Caches per (path, mtime, size). Re-parses only when one of those changes —
re-reading every transcript every 500ms would be wasteful.

Returns 0 silently for missing/corrupt transcripts; the dashboard uses this
in a non-critical summary line.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ephor.state.models import AgentState

_TRANSCRIPTS_ROOT = Path.home() / ".claude" / "projects"

# A session id safe to embed in a glob (UUID-shaped). Guards the fallback
# search below against path-traversal / glob-metachar injection from a crafted
# state file.
_SAFE_SID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def transcript_path(cwd: str, session_id: str) -> Path:
    """`/home/alice/x/y` + `sid` → ~/.claude/projects/-home-alice-x-y/sid.jsonl

    Claude encodes the session's *start* cwd into the directory name. If that
    cwd later changes (the dir was renamed, or the session moved to a git
    worktree), the encoded path no longer exists — so when the direct path is
    missing we fall back to locating the transcript by its (UUID) session id
    anywhere under the projects root. The direct path is returned unchanged in
    the common case, keeping the hot token-tracking path fast.
    """
    encoded = cwd.replace("/", "-")
    direct = _TRANSCRIPTS_ROOT / encoded / f"{session_id}.jsonl"
    if direct.exists() or not _SAFE_SID.fullmatch(session_id):
        return direct
    for match in _TRANSCRIPTS_ROOT.glob(f"*/{session_id}.jsonl"):
        return match
    return direct


def _sum_tokens_in_file(path: Path) -> int:
    """Total of input + cache_creation + cache_read + output tokens across
    every assistant message in the transcript. Tolerant of malformed lines."""
    total = 0
    try:
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    continue
                msg = entry.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                for key in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                    "output_tokens",
                ):
                    val = usage.get(key, 0)
                    if isinstance(val, int):
                        total += val
    except OSError:
        return 0
    return total


class TokenTracker:
    """Caches per-session token totals; only re-parses when transcript changes."""

    def __init__(self) -> None:
        # path → (mtime_ns, size, total)
        self._cache: dict[Path, tuple[int, int, int]] = {}

    def total_for(self, agent: AgentState) -> int:
        path = transcript_path(agent.cwd, agent.session_id)
        try:
            st = path.stat()
        except OSError:
            self._cache.pop(path, None)
            return 0
        cached = self._cache.get(path)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2]
        total = _sum_tokens_in_file(path)
        self._cache[path] = (st.st_mtime_ns, st.st_size, total)
        return total

    def total_across(self, agents: list[AgentState]) -> int:
        return sum(self.total_for(a) for a in agents)


def format_tokens(n: int) -> str:
    """142_300 → '142.3k'; 1_500_000 → '1.5M'."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"

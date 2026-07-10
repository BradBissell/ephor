"""Discover the tmux pane for each running coding-agent process.

When a session is started outside ephor's hook coverage (e.g. before `ephor init`
was run, or the SessionStart hook missed the TMUX env var), the state file's
tmux_* fields stay null and `ephor tui`'s Enter-to-jump can't help.

This module fixes that gap by walking /proc + `tmux list-panes` to
reconstruct the mapping (cwd, agent_pid) → (tmux_session, tmux_window,
tmux_pane). It scans for every supported agent binary (claude, gemini, codex,
grok), not just Claude. Use it on demand from the CLI (`ephor refresh-tmux`) or
inline from the TUI when a jump misses.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ephor.providers import KNOWN_BINARIES, PROVIDERS

# Bound on parent-walk depth so a corrupt /proc entry can't loop forever.
MAX_ANCESTOR_DEPTH = 32

# binary name → provider name, for tagging discovered processes and picking the
# right resume-flag dialect when parsing argv.
_BINARY_TO_PROVIDER: dict[str, str] = {p.binary: p.name for p in PROVIDERS.values()}


@dataclass(frozen=True)
class TmuxPaneInfo:
    """tmux fields we care about for navigation, plus liveness identifiers."""

    tmux_session: str
    tmux_window: str
    tmux_pane: str
    agent_pid: int
    cwd: str
    # Session-id parsed out of `<agent> --resume <sid>` argv; None for
    # fresh sessions that didn't resume.
    session_id: str | None = None
    # Which coding agent this process is (claude/gemini/codex/grok), from the
    # matched binary name. Empty when unknown.
    provider: str = ""


def has_tmux() -> bool:
    return shutil.which("tmux") is not None


def _list_tmux_panes() -> list[tuple[int, str, str, str, str]]:
    """Return [(pane_pid, session, window, pane_id, sid_from_pane_option), ...]
    for every pane on the running tmux server. The 5th field is the value of
    the per-pane `@ephor_sid` user option (empty string if unset — set by
    the SessionStart hook).

    Empty list on any failure."""
    try:
        out = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{pane_pid}\t#S\t#W\t#{pane_id}\t#{@ephor_sid}",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if out.returncode != 0:
        return []

    panes: list[tuple[int, str, str, str, str]] = []
    for line in out.stdout.splitlines():
        # 4 tab-separated fields when @ephor_sid is unset (some tmux versions
        # trim trailing empty fields); 5 when set. Accept both shapes.
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        sess, win, pane_id = parts[1], parts[2], parts[3]
        sid_from_pane = parts[4] if len(parts) >= 5 else ""
        panes.append((pid, sess, win, pane_id, sid_from_pane))
    return panes


def _agent_pids() -> dict[int, str]:
    """Return {pid: binary_name} for every running coding-agent process.

    Scans each supported agent binary (claude, gemini, codex, grok) with
    `pgrep -x`. Empty dict when none are running.
    """
    found: dict[int, str] = {}
    for binary in KNOWN_BINARIES:
        try:
            out = subprocess.run(
                ["pgrep", "-x", binary],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            continue
        if out.returncode != 0:
            # pgrep returns 1 when no processes match — fine, just none of these.
            continue
        for p in out.stdout.split():
            if p.strip().isdigit():
                found.setdefault(int(p), binary)
    return found


def _read_proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _read_proc_ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1])
    except (OSError, ValueError):
        return None
    return None


_VALID_SID = set("0123456789abcdefABCDEF-")


def _read_session_id_from_cmdline(pid: int, resume_flags: tuple[str, ...]) -> str | None:
    """If the agent was launched to resume a session (e.g. `claude --resume
    <sid>`, `grok -s <sid>`), extract the session_id from /proc/<pid>/cmdline.
    `resume_flags` are the provider's flags that precede a session id. Returns
    None for fresh sessions, unreadable cmdlines, or a provider with no resume
    flags."""
    if not resume_flags:
        return None
    flags = tuple(f.encode() for f in resume_flags)
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
    except OSError:
        return None
    for i, arg in enumerate(argv):
        if arg in flags and i + 1 < len(argv):
            sid = argv[i + 1].decode(errors="ignore").strip()
            # UUID-shaped (lower/upper hex + dashes). Reject anything else
            # so a bogus argv can't map to a state-file path-traversal.
            if sid and 8 <= len(sid) <= 64 and all(c in _VALID_SID for c in sid):
                return sid.lower()
    return None


def _walk_to_tmux_pane(
    start_pid: int, pane_pids: dict[int, tuple[str, str, str]]
) -> tuple[str, str, str] | None:
    """Walk parent PIDs up from start_pid; return the (session, window,
    pane_id) of the first ancestor that is a known tmux pane root, or None."""
    pid: int | None = start_pid
    for _ in range(MAX_ANCESTOR_DEPTH):
        if pid is None or pid <= 1:
            return None
        if pid in pane_pids:
            return pane_pids[pid]
        pid = _read_proc_ppid(pid)
    return None


def discover_panes() -> list[TmuxPaneInfo]:
    """Find every running coding-agent process that lives inside a tmux pane.

    Empty list when tmux isn't installed, no agents are running, or none
    of them are inside a pane (the agent was started in a plain terminal).
    """
    if not has_tmux():
        return []

    panes = _list_tmux_panes()
    if not panes:
        return []
    pane_pids: dict[int, tuple[str, str, str]] = {
        pid: (sess, win, pane_id) for pid, sess, win, pane_id, _ in panes
    }
    # Per-pane @ephor_sid set by the hook handler — definitive when present.
    sid_by_pane: dict[str, str] = {pane_id: sid for _, _, _, pane_id, sid in panes if sid}

    discovered: list[TmuxPaneInfo] = []
    for cpid, binary in _agent_pids().items():
        cwd = _read_proc_cwd(cpid)
        if cwd is None:
            continue
        match = _walk_to_tmux_pane(cpid, pane_pids)
        if match is None:
            continue
        sess, win, pane = match
        provider_name = _BINARY_TO_PROVIDER.get(binary, "")
        resume_flags = PROVIDERS[provider_name].resume_flags if provider_name else ()
        # Prefer the pane's @ephor_sid (set by SessionStart hook) over the
        # cmdline parse — survives the user's `cmd | <agent>` wrapper, exec
        # chains, and other process-tree shenanigans that hide resume args.
        session_id = sid_by_pane.get(pane) or _read_session_id_from_cmdline(cpid, resume_flags)
        discovered.append(
            TmuxPaneInfo(
                tmux_session=sess,
                tmux_window=win,
                tmux_pane=pane,
                agent_pid=cpid,
                cwd=cwd,
                session_id=session_id,
                provider=provider_name,
            )
        )
    return discovered


def enrich_state_files(state_dir: Path) -> int:
    """Walk `state_dir`, and for every state file whose tmux fields are
    None, look up the matching agent process and write tmux info back.

    Match priority:
      1. **agent_pid** (definitive). If the state file already records the
         agent pid (the handler walks up to find it), match by pid — this
         is unambiguous even when several sessions share a cwd.
      2. **cwd**, but ONLY when exactly one running agent has that cwd.
         If multiple discovered panes share a cwd, skip the file and wait
         for the next hook event (which will record agent_pid).

    Returns the number of state files updated.
    """
    import json

    if not state_dir.is_dir():
        return 0

    discovered = discover_panes()
    if not discovered:
        return 0

    by_pid: dict[int, TmuxPaneInfo] = {info.agent_pid: info for info in discovered}
    by_sid: dict[str, TmuxPaneInfo] = {
        info.session_id: info for info in discovered if info.session_id
    }

    cwd_counts: dict[str, int] = {}
    for info in discovered:
        cwd_counts[info.cwd] = cwd_counts.get(info.cwd, 0) + 1
    by_unique_cwd: dict[str, TmuxPaneInfo] = {
        info.cwd: info for info in discovered if cwd_counts[info.cwd] == 1
    }

    updated = 0
    for path in state_dir.glob("*.json"):
        if path.name.startswith(".tmp"):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue

        # Match priority:
        #   1. session_id parsed from `claude --resume <sid>` argv (definitive)
        #   2. recorded agent_pid in state file (definitive)
        #   3. unique cwd (best-effort; ambiguous cwds are skipped)
        match: TmuxPaneInfo | None = None
        is_definitive = False

        sid = data.get("session_id")
        if isinstance(sid, str) and sid in by_sid:
            match = by_sid[sid]
            is_definitive = True
        else:
            recorded_pid = data.get("agent_pid")
            if isinstance(recorded_pid, int) and recorded_pid in by_pid:
                match = by_pid[recorded_pid]
                is_definitive = True
            else:
                cwd = data.get("cwd")
                if cwd:
                    match = by_unique_cwd.get(cwd)
        if match is None:
            continue

        # Decide whether to overwrite:
        #   - Definitive match (sid or pid): always overwrite, even if the
        #     existing value is "populated" (because our prior best-effort
        #     match could have written wrong info).
        #   - cwd-only match: only fill in absent or corrupt fields.
        existing = (
            data.get("tmux_session"),
            data.get("tmux_window"),
            data.get("tmux_pane"),
        )
        new_values = (match.tmux_session, match.tmux_window, match.tmux_pane)
        if not is_definitive:
            # Treat the pre-fix "sess\twin\tpane" concat string as corrupt.
            corrupt = any(v and ("\t" in v or "	" in v) for v in existing if isinstance(v, str))
            empty = not all(existing)
            if not (corrupt or empty):
                continue
        elif existing == new_values:
            continue

        data["tmux_session"] = match.tmux_session
        data["tmux_window"] = match.tmux_window
        data["tmux_pane"] = match.tmux_pane
        # Backfill agent_pid + provider when missing — speeds up future
        # lookups by promoting agent_pid into the definitive-match channels,
        # and labels sessions discovered before any hook fired.
        if not data.get("agent_pid"):
            data["agent_pid"] = match.agent_pid
        if not data.get("provider") and match.provider:
            data["provider"] = match.provider

        # Atomic-rename write so a partial write can't corrupt the file
        # the watcher is reading concurrently.
        tmp = path.with_name(f".tmp.enrich.{path.name}")
        try:
            tmp.write_text(json.dumps(data))
            tmp.chmod(0o600)
            tmp.replace(path)
            updated += 1
        except OSError:
            with _suppress():
                tmp.unlink()
    return updated


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, OSError)

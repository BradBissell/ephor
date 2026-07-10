"""tmux navigation: jump to the pane/window for a given AgentState.

This is the only navigation surface ephor supports. There's no ghostty
automation, no X11 window-poking, no AppleScript. If the session isn't in a
tmux pane (because the user runs claude in a plain terminal), navigation
returns NotInTmux and the caller can show a hint.

The functions here shell out to `tmux` directly — fast, no library deps,
matches the reference clorch's pure-tmux navigator (~85% lifted, with
defensive checks added).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ephor.state.models import AgentState


class JumpResult(Enum):
    """Outcome of an attempt to jump to a session's tmux pane."""

    OK = "ok"
    NO_TMUX_INFO = "no_tmux_info"  # state file lacks tmux_* fields
    SESSION_NOT_FOUND = "session_not_found"  # tmux doesn't have that session anymore
    TMUX_MISSING = "tmux_missing"  # tmux not on PATH
    FAILED = "failed"  # something else went wrong (subprocess error, etc.)


@dataclass(frozen=True)
class JumpOutcome:
    result: JumpResult
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.result is JumpResult.OK


def _run_tmux(
    *args: str, check: bool = False, timeout: float = 2.0
) -> subprocess.CompletedProcess[str]:
    """Invoke tmux with args. Returns the completed process. Never raises on
    timeout / non-zero unless check=True; that's the caller's job."""
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def has_tmux() -> bool:
    return shutil.which("tmux") is not None


def _session_exists(name: str) -> bool:
    proc = _run_tmux("has-session", "-t=" + name)
    return proc.returncode == 0


# /proc-based ancestry walk — Linux-only, matches the depth cap used by
# tmux/discover.py so a deeply-nested subagent still resolves to its pane.
_MAX_ANCESTOR_DEPTH = 8


def _read_ppid(pid: int) -> int | None:
    """Return the parent pid for `pid`, or None if it can't be read."""
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


def _pid_descends_from(descendant: int, ancestor: int) -> bool:
    """Walk PPIDs up from `descendant`; return True if `ancestor` is in the chain.

    Used to decide whether a tmux pane (whose pane_pid is the foreground
    process leader) currently hosts a particular agent_pid — claude may sit
    several frames above pane_pid (sh → ephor-launcher → claude → subagent).
    """
    cur: int | None = descendant
    for _ in range(_MAX_ANCESTOR_DEPTH):
        if cur is None or cur <= 1:
            return False
        if cur == ancestor:
            return True
        cur = _read_ppid(cur)
    return False


def _pane_hosts_pid(pane_id: str, target_pid: int) -> bool:
    """Whether the given tmux pane currently hosts `target_pid` in its tree.

    The pane's `pane_pid` is its foreground process leader — `target_pid`
    may be that leader or one of its descendants. Walks PPIDs up from
    `target_pid` to find a match. Returns False on any tmux/proc failure
    (callers treat False as "rediscover").
    """
    proc = _run_tmux("display-message", "-p", "-t", pane_id, "#{pane_pid}")
    if proc.returncode != 0:
        return False
    raw = proc.stdout.strip()
    if not raw.isdigit():
        return False
    return _pid_descends_from(target_pid, int(raw))


def jump_to(state: AgentState) -> JumpOutcome:
    """Switch the user's current tmux client to the session/window/pane the
    given AgentState was last seen in."""
    if not has_tmux():
        return JumpOutcome(JumpResult.TMUX_MISSING, "tmux not on PATH")

    sess = state.tmux_session
    win = state.tmux_window
    pane = state.tmux_pane

    if not sess or not win:
        return JumpOutcome(
            JumpResult.NO_TMUX_INFO,
            "this session never reported a tmux pane — start it inside tmux to enable jump",
        )

    if not _session_exists(sess):
        return JumpOutcome(
            JumpResult.SESSION_NOT_FOUND,
            f"tmux session '{sess}' no longer exists",
        )

    # Validate that the recorded pane still hosts the recorded agent_pid.
    # Pane IDs are stable on the tmux server but get reused when a pane is
    # closed and a new claude is started — so a `select-window` on a stale
    # pane_id silently lands the user in the *wrong* window. Failing here
    # with "can't find pane" lets the caller's existing rediscover path
    # (enrich_state_files) walk /proc + tmux and update the state file.
    if pane and state.agent_pid and not _pane_hosts_pid(pane, state.agent_pid):
        return JumpOutcome(
            JumpResult.FAILED,
            f"can't find pane: {pane} no longer hosts pid {state.agent_pid}",
        )

    # Prefer pane_id (e.g. %24) over session:windowname. Pane IDs are globally
    # unique on the tmux server and stable across renames; window names get
    # auto-set to the active command, so multiple claude panes in one session
    # all become "claude" and `select-window -t sess:claude` fails ambiguously
    # with "can't find window: claude".
    target_window = pane if pane else f"{sess}:{win}"
    proc = _run_tmux("select-window", "-t", target_window)
    if proc.returncode != 0:
        return JumpOutcome(
            JumpResult.FAILED,
            f"select-window failed: {proc.stderr.strip() or 'no stderr'}",
        )

    if pane:
        # Failures here are non-fatal — we already moved the window.
        _run_tmux("select-pane", "-t", pane)

    return JumpOutcome(JumpResult.OK)


# ---- session lifecycle: kill a session + tmux window ---------------------


@dataclass(frozen=True)
class KillOutcome:
    ok: bool
    detail: str = ""


def kill_session(state: AgentState, state_dir: Path) -> KillOutcome:
    """Terminate a Claude Code session and clean up after it.

    Steps (each best-effort, all attempted even if earlier ones fail):
      1. SIGTERM the recorded agent_pid (skip if already dead).
      2. tmux kill-window for its session:window if known.
      3. Remove the per-session state file from state_dir.

    Returns ok=True when at least one step succeeded; the dashboard should
    refresh after either way to reflect the new reality.
    """
    failures: list[str] = []
    succeeded = False

    if state.agent_pid:
        try:
            os.kill(state.agent_pid, signal.SIGTERM)
            succeeded = True
        except ProcessLookupError:
            # Already dead — nothing to do, but proceed with cleanup.
            succeeded = True
        except PermissionError:
            failures.append(f"can't signal pid {state.agent_pid}")
        except OSError as exc:
            failures.append(f"kill {state.agent_pid}: {exc}")

    if state.tmux_session and state.tmux_window and has_tmux():
        # Prefer pane_id over session:windowname for the same reason as
        # jump_to: multiple claude panes in one session all get auto-renamed
        # to "claude", making name-based targeting fail ambiguously.
        target = state.tmux_pane if state.tmux_pane else f"{state.tmux_session}:{state.tmux_window}"
        proc = _run_tmux("kill-window", "-t", target)
        if proc.returncode == 0:
            succeeded = True
        else:
            # "can't find window" means it's already gone — that's success.
            err = proc.stderr.strip().lower()
            if "find" in err or "no such" in err:
                succeeded = True
            else:
                failures.append(f"tmux kill-window: {proc.stderr.strip() or 'failed'}")

    state_file = state_dir / f"{state.session_id}.json"
    try:
        state_file.unlink()
        succeeded = True
    except FileNotFoundError:
        # State file may have been swept already; not a failure.
        pass
    except OSError as exc:
        failures.append(f"unlink {state_file.name}: {exc}")

    if not succeeded:
        return KillOutcome(False, "; ".join(failures) or "nothing to do")
    return KillOutcome(True, "; ".join(failures))


# ---- "follow the user" helper: detect which pane the user is interacting
# with from another tmux client (e.g. another Ghostty window) so the ephor
# dashboard can move its cursor to that session automatically. ------------


def detect_focused_external_pane(self_pane: str | None = None) -> str | None:
    """Return the pane_id the user is currently interacting with in any
    tmux client OTHER than the one running ephor itself, or None if it can't
    be determined.

    Selection logic:
      1. List all tmux clients and their attached session.
      2. For each client, look up that session's currently-active pane
         (the pane the client would type into right now).
      3. Drop clients whose active pane equals `self_pane` — those are
         "us looking at the dashboard."
      4. Prefer a client with `client_focused=1` (requires `set -g
         focus-events on` and a focus-aware terminal — Ghostty qualifies);
         otherwise fall back to the client with the most recent
         `client_activity` (last keystroke timestamp).

    `self_pane` should be the pane_id hosting the ephor TUI — typically
    `os.environ["TMUX_PANE"]` from the caller. If None, no exclusion is
    applied (use case: ephor running outside tmux altogether).

    Returns None on any tmux failure, when no external client exists, or
    when the focused pane couldn't be resolved.
    """
    if not has_tmux():
        return None

    clients = _run_tmux(
        "list-clients",
        "-F",
        "#{client_tty}|#{client_focused}|#{client_activity}|#{client_session}",
    )
    if clients.returncode != 0 or not clients.stdout.strip():
        return None

    # session_name → active pane_id. One pass over `list-panes -a` is
    # cheaper than a `display-message` per client and immune to TOCTOU
    # between the two queries.
    panes_proc = _run_tmux(
        "list-panes",
        "-a",
        "-F",
        "#{session_name}|#{pane_id}|#{window_active}|#{pane_active}",
    )
    if panes_proc.returncode != 0:
        return None
    active_pane_by_session: dict[str, str] = {}
    for line in panes_proc.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) != 4:
            continue
        session, pane_id, window_active, pane_active = parts
        if window_active == "1" and pane_active == "1":
            active_pane_by_session[session] = pane_id

    candidates: list[tuple[bool, int, str]] = []  # (focused, activity, pane_id)
    for line in clients.stdout.strip().splitlines():
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        _tty, focused, activity, session = parts
        pane = active_pane_by_session.get(session, "")
        if not pane:
            continue
        if self_pane and pane == self_pane:
            # That client is showing the ephor TUI — that's "us," not the
            # user looking somewhere else.
            continue
        try:
            activity_int = int(activity) if activity else 0
        except ValueError:
            activity_int = 0
        candidates.append((focused == "1", activity_int, pane))

    if not candidates:
        return None

    # Prefer focused client; otherwise the most recently active.
    focused_only = [c for c in candidates if c[0]]
    pool = focused_only or candidates
    return max(pool, key=lambda c: c[1])[2]

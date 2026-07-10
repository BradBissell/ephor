"""Filesystem paths and runtime configuration for ephor.

All paths follow XDG Base Directory Specification. Override via env vars for
testing or non-default installs.
"""

from __future__ import annotations

import os
from pathlib import Path

# Schema version for state files. Bump on any breaking change to the JSON shape.
# v2 added `last_summary` (UserPromptSubmit prompt, 70-char truncated).
# v3 renamed `claude_pid`→`agent_pid` and added `provider` (which coding-agent
# CLI wrote the file). Readers tolerate v1/v2 files in-place, mapping the legacy
# `claude_pid` key onto `agent_pid` — see AgentState.from_dict.
SCHEMA_VERSION = 3
KNOWN_SCHEMA_VERSIONS = frozenset({1, 2, 3})

# Hook handler timing budget — emit a warning above WARN, kill criterion above KILL.
HOOK_LATENCY_WARN_MS = 15
HOOK_LATENCY_KILL_MS = 50

# Reconciler thresholds — see state/reconciler.py (P5).
RECONCILE_HOOKS_STALE_SEC = 60
RECONCILE_FILE_STALE_SEC = 60

# Lock acquisition timeout in the hook handler. Past this, the handler exits
# silently without writing state — preserves fail-OPEN guarantee.
HOOK_FLOCK_TIMEOUT_SEC = 2


def _xdg_home(name: str, fallback: str) -> Path:
    """Resolve an XDG_*_HOME env var, defaulting to fallback under $HOME."""
    raw = os.environ.get(name)
    if raw:
        return Path(raw).expanduser()
    return Path(os.path.expanduser(f"~/{fallback}"))


def state_dir() -> Path:
    """Where per-session JSON state files live.

    Default: $XDG_STATE_HOME/ephor/sessions/
    Override: $EPHOR_STATE_DIR
    """
    raw = os.environ.get("EPHOR_STATE_DIR")
    if raw:
        return Path(raw).expanduser()
    return _xdg_home("XDG_STATE_HOME", ".local/state") / "ephor" / "sessions"


def pending_dir() -> Path:
    """Where TUI-issued pending permission decisions live (read by the hook)."""
    return state_dir().parent / "pending"


def config_dir() -> Path:
    """Where user-editable config (rules.yaml) lives.

    Default: $XDG_CONFIG_HOME/ephor/
    Override: $EPHOR_CONFIG_DIR
    """
    raw = os.environ.get("EPHOR_CONFIG_DIR")
    if raw:
        return Path(raw).expanduser()
    return _xdg_home("XDG_CONFIG_HOME", ".config") / "ephor"


def claude_settings_path() -> Path:
    """Path to Claude Code's settings.json that we mutate via the installer."""
    raw = os.environ.get("CLAUDE_SETTINGS_PATH")
    if raw:
        return Path(raw).expanduser()
    return Path(os.path.expanduser("~/.claude/settings.json"))


def hook_handler_path() -> Path:
    """Absolute path to the installed event_handler.sh.

    During development with `pipx install -e .` it lives in src/. When
    installed via pipx/pip, hatch bundles the hooks/ directory next to the
    package files.
    """
    package_root = Path(__file__).resolve().parent
    return package_root / "hooks" / "event_handler.sh"


def ensure_state_dirs() -> None:
    """Create state and pending dirs with restrictive permissions (0700)."""
    sd = state_dir()
    pd = pending_dir()
    for d in (sd, pd):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)

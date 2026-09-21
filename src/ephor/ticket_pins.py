"""User-declared ticket overrides, keyed by session id.

The highest-authority signal for "what is this session working on" is a
human saying so. This module is where that answer lives.

It is deliberately *not* the session's state file. Those are written by the
hook handler under a flock on every event; having the TUI write them too
would put a second writer on a file whose whole design assumes one, for the
sake of a label. A separate ephor-owned map sidesteps the race entirely —
the hook never reads it and never writes it.

Pins are small and long-lived, so the whole map is rewritten on each change
via the same atomic-rename discipline the state files use.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ephor.config import state_dir


def pins_path() -> Path:
    """Where the session-id -> ticket map lives (beside the state dir)."""
    return state_dir().parent / "ticket_pins.json"


def load() -> dict[str, str]:
    """Every pin, or an empty map when the file is missing or unreadable."""
    path = pins_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v}


def get(session_id: str | None) -> str | None:
    """The pinned ticket for ``session_id``, or None."""
    if not session_id:
        return None
    return load().get(session_id) or None


def _save(pins: dict[str, str]) -> None:
    path = pins_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ticket_pins.")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(pins, fh, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
    except OSError:
        # A pin is a convenience; failing to persist it must never take
        # down the dashboard.
        return


def pin(session_id: str, ticket: str) -> None:
    """Record ``ticket`` as the declared key for ``session_id``."""
    if not session_id or not ticket:
        return
    pins = load()
    pins[session_id] = ticket
    _save(pins)


def unpin(session_id: str) -> None:
    """Forget any pin for ``session_id``, falling back to the probe chain."""
    if not session_id:
        return
    pins = load()
    if pins.pop(session_id, None) is None:
        return
    _save(pins)

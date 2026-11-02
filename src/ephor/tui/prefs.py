"""Dashboard preferences that outlive one run: which grouping was showing.

Stored the way the speech settings are — a small JSON file in the config
directory (``$EPHOR_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/ephor``), written
when the preference changes and read once at start-up. Everything here is
best-effort: an unreadable or unwritable file means the default, never a
dashboard that will not open.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ephor.config import config_dir

PREFS_FILE = "tui.json"


def prefs_path() -> Path:
    return config_dir() / PREFS_FILE


def _read() -> dict[str, Any]:
    try:
        data = json.loads(prefs_path().read_text())
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def load_grouping(allowed: tuple[str, ...], default: str = "none") -> str:
    """The grouping the last run ended on, if it is still one this build knows."""
    value = _read().get("group_by")
    return value if isinstance(value, str) and value in allowed else default


def save_grouping(group_by: str) -> bool:
    """Remember ``group_by`` for the next launch. False when the disk said no."""
    data = _read()
    if data.get("group_by") == group_by:
        return True
    data["group_by"] = group_by
    target = prefs_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tui-", suffix=".json")
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
        Path(tmp).replace(target)
    except OSError:
        return False
    return True

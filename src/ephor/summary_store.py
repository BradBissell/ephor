"""Sidecar store for LLM-generated session summaries.

Lives at $XDG_STATE_HOME/ephor/summaries/<sid>.json — one file
per session, keyed by session_id. Kept out of AgentState so the on-disk
schema stays stable and the hook handler doesn't need to learn about
summaries.

File shape:
    {"summary": "...", "generated_at": "2026-04-30T12:34:56Z"}

Atomic writes use the same .tmp + rename pattern as the hook handler.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ephor.config import state_dir

log = logging.getLogger(__name__)


def summary_dir() -> Path:
    """Sibling of `sessions/` under the ephor state root."""
    return state_dir().parent / "summaries"


def _summary_path(directory: Path, session_id: str) -> Path:
    # Defensive: only allow safe chars in the on-disk filename.
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return directory / f"{safe}.json"


class SummaryStore:
    """Read/write per-session summary sidecar files.

    Cheap to construct; constructs the directory lazily on first write so
    a TUI started without permission to mkdir doesn't crash.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory if directory is not None else summary_dir()

    @property
    def directory(self) -> Path:
        return self._dir

    def get(self, session_id: str) -> str | None:
        """Return the cached summary for `session_id`, or None if absent."""
        path = _summary_path(self._dir, session_id)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        summary = data.get("summary")
        return summary if isinstance(summary, str) and summary else None

    def has(self, session_id: str) -> bool:
        return self.get(session_id) is not None

    def set(self, session_id: str, summary: str) -> None:
        """Atomically write a summary. No-op when summary is empty."""
        if not summary:
            return
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._dir.chmod(0o700)
        except OSError as exc:
            log.debug("could not create summary dir: %s", exc)
            return

        payload = json.dumps(
            {
                "summary": summary,
                "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
        path = _summary_path(self._dir, session_id)
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".tmp.", dir=self._dir)
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(payload)
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, path)
            except OSError:
                with _suppress():
                    os.unlink(tmp_path)
                raise
        except OSError as exc:
            log.debug("could not write summary for %s: %s", session_id, exc)

    def delete(self, session_id: str) -> None:
        """Drop a cached summary (used when a session is killed)."""
        path = _summary_path(self._dir, session_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.debug("could not delete summary for %s: %s", session_id, exc)


class _suppress:
    """contextlib.suppress(OSError) without the import (mirrors discover.py)."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, OSError)

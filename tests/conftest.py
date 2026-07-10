"""Test suite-wide safety net.

Every test gets a sandboxed CLAUDE_SETTINGS_PATH and EPHOR_STATE_DIR pointing
at the test's tmp_path by default — so a buggy test or a CLI command that
forgets to mock paths can never accidentally mutate the developer's real
~/.claude/settings.json.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _sandbox_user_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect every path that could touch user state."""
    monkeypatch.setenv("EPHOR_STATE_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("EPHOR_PENDING_DIR", str(tmp_path / "pending"))
    monkeypatch.setenv("EPHOR_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("EPHOR_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(tmp_path / "claude_settings.json"))

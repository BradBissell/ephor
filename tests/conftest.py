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
    # Ticket resolution reads both of these from the ambient environment. A
    # developer who exports either one for their own dashboard would
    # otherwise change what the suite asserts.
    monkeypatch.delenv("EPHOR_TICKET_PROJECTS", raising=False)
    monkeypatch.delenv("EPHOR_TICKET", raising=False)
    # Every opt-in integration stays off unless a test turns it on. A
    # developer who exports real Jira credentials or an ntfy topic for their
    # own dashboard must not have the suite start making network calls with
    # them — and `is_configured()` is exactly the thing several tests assert.
    for name in (
        "EPHOR_JIRA_URL",
        "EPHOR_JIRA_EMAIL",
        "EPHOR_JIRA_TOKEN",
        "EPHOR_JIRA_DONE_STATUSES",
        "JIRA_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "EPHOR_NOTIFY_URL",
        "EPHOR_NOTIFY_TOKEN",
        "EPHOR_PERMISSION_WAIT_SEC",
        "EPHOR_WORKTREE_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)

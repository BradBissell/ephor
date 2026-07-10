"""End-to-end test: install hooks → simulate hook firing → ephor list shows it.

This is the v0 smoke test that proves the whole pipeline (shell handler →
state file → CLI) works as a unit.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ephor.cli import main
from ephor.config import hook_handler_path


def _required_tools_present() -> bool:
    return all(shutil.which(t) for t in ("bash", "jq", "flock"))


@pytest.fixture(autouse=True)
def _skip_if_tools_missing() -> None:
    if not _required_tools_present():
        pytest.skip("requires bash + jq + flock on PATH")


def test_full_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: fire 3 fake hook events, verify ephor list reports them."""
    state_dir = tmp_path / "sessions"
    pending_dir = tmp_path / "pending"
    lock_dir = tmp_path / "locks"
    monkeypatch.setenv("EPHOR_STATE_DIR", str(state_dir))
    monkeypatch.setenv("EPHOR_PENDING_DIR", str(pending_dir))
    monkeypatch.setenv("EPHOR_LOCK_DIR", str(lock_dir))

    # Sandbox env for the hook handler subprocess too.
    sub_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "EPHOR_STATE_DIR": str(state_dir),
        "EPHOR_PENDING_DIR": str(pending_dir),
        "EPHOR_LOCK_DIR": str(lock_dir),
    }

    handler = hook_handler_path()

    events = [
        {
            "session_id": "e2e-session-A",
            "hook_event_name": "SessionStart",
            "cwd": "/tmp/projA",
        },
        {
            "session_id": "e2e-session-A",
            "hook_event_name": "PreToolUse",
            "cwd": "/tmp/projA",
            "tool_name": "Bash",
        },
        {
            "session_id": "e2e-session-B",
            "hook_event_name": "PermissionRequest",
            "cwd": "/tmp/projB",
            "tool_name": "Edit",
        },
    ]
    for event in events:
        result = subprocess.run(
            ["bash", str(handler)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=5,
            env=sub_env,
            check=False,
        )
        assert result.returncode == 0, f"handler failed: {result.stderr}"

    # State files exist with expected statuses.
    assert (state_dir / "e2e-session-A.json").is_file()
    assert (state_dir / "e2e-session-B.json").is_file()

    a = json.loads((state_dir / "e2e-session-A.json").read_text())
    assert a["status"] == "WORKING"
    assert a["tool_count"] == 1

    b = json.loads((state_dir / "e2e-session-B.json").read_text())
    assert b["status"] == "WAITING_PERMISSION"

    # In production, two genuinely separate claude processes have
    # distinct PIDs. This test harness fires hooks from the same pytest
    # process so the handler walks up to the same non-shell ancestor for
    # both — which now (correctly) trips the resume-residue dedup. Force
    # distinct LIVE PIDs (test process + parent) so the dashboard sees
    # two independent live sessions, matching what real concurrent claudes
    # would produce.
    import os as _os

    a["agent_pid"] = _os.getpid()
    b["agent_pid"] = _os.getppid()
    (state_dir / "e2e-session-A.json").write_text(json.dumps(a))
    (state_dir / "e2e-session-B.json").write_text(json.dumps(b))

    # ephor status reports both.
    rc = main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "total:2" in out
    assert "P:1" in out  # one PermissionRequest
    assert "W:1" in out  # one Working

    # ephor list shows both rows.
    rc = main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "projA" in out
    assert "projB" in out


def test_round_trip_install_uninstall_via_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ephor init` followed by `ephor uninstall` is byte-clean for empty input."""
    settings = tmp_path / "claude_settings.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))

    rc = main(["init"])
    capsys.readouterr()  # discard
    assert rc == 0
    assert settings.is_file()
    data = json.loads(settings.read_text())
    assert "hooks" in data

    rc = main(["uninstall"])
    capsys.readouterr()
    assert rc == 0
    after = json.loads(settings.read_text())
    # Empty starting state → empty ending state.
    assert after == {}


def test_init_dry_run_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = tmp_path / "claude_settings.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))

    rc = main(["init", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[dry-run]" in out
    assert not settings.exists()

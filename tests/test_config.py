"""Tests for config path resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ephor import config


def test_state_dir_defaults_to_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("EPHOR_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    expected = tmp_path / "ephor" / "sessions"
    assert config.state_dir() == expected


def test_state_dir_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom"
    monkeypatch.setenv("EPHOR_STATE_DIR", str(custom))
    assert config.state_dir() == custom


def test_state_dir_falls_back_to_dot_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EPHOR_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    sd = config.state_dir()
    assert "ephor" in str(sd)
    assert sd.name == "sessions"


def test_pending_dir_is_sibling_of_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EPHOR_STATE_DIR", str(tmp_path / "sessions"))
    assert config.pending_dir() == tmp_path / "pending"


def test_ensure_state_dirs_creates_with_0700(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EPHOR_STATE_DIR", str(tmp_path / "sessions"))
    config.ensure_state_dirs()
    assert (tmp_path / "sessions").is_dir()
    assert (tmp_path / "pending").is_dir()
    # Permissions: low 9 bits == 0o700
    assert (tmp_path / "sessions").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "pending").stat().st_mode & 0o777 == 0o700


def test_hook_handler_path_points_at_packaged_script() -> None:
    p = config.hook_handler_path()
    assert p.name == "event_handler.sh"
    assert p.is_file(), f"hook handler missing at {p}"
    assert os.access(p, os.X_OK) or p.read_text().startswith("#!"), (
        "hook handler must be executable or have a shebang"
    )

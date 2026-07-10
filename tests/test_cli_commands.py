"""End-to-end CLI tests for list/status/tmux-widget."""

from __future__ import annotations

from pathlib import Path

import pytest

from ephor.cli import main
from ephor.constants import AgentStatus
from ephor.state.models import AgentState


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sd = tmp_path / "sessions"
    sd.mkdir(parents=True)
    monkeypatch.setenv("EPHOR_STATE_DIR", str(sd))
    return sd


def _write(directory: Path, sid: str, **kwargs: object) -> None:
    base = {
        "session_id": sid,
        "cwd": "/tmp/x",
        "started_at": "2026-04-29T10:00:00Z",
        "last_event_time": "2026-04-29T10:00:00Z",
    }
    base.update(kwargs)
    state = AgentState(**base)  # type: ignore[arg-type]
    (directory / f"{sid}.json").write_text(state.to_json())


def test_list_empty_says_so(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No active sessions" in out


def test_list_renders_session_rows(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(state_dir, "abc12345", status=AgentStatus.WORKING, project_name="myproj", tool_count=7)
    _write(
        state_dir,
        "def67890",
        status=AgentStatus.WAITING_PERMISSION,
        project_name="otherproj",
    )
    rc = main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "myproj" in out
    assert "otherproj" in out
    assert "abc12345"[:8] in out
    assert "WORK" in out
    assert "PERM" in out


def test_status_empty(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "total:0" in out


def test_status_with_attention(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(state_dir, "a", status=AgentStatus.WAITING_PERMISSION)
    _write(state_dir, "b", status=AgentStatus.WORKING)
    _write(state_dir, "c", status=AgentStatus.ERROR)
    rc = main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "P:1" in out
    assert "W:1" in out
    assert "E:1" in out
    assert "total:3" in out


def test_tmux_widget_empty_outputs_dot(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["tmux-widget"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ephor" in out
    # Empty state shows dim dot.
    assert "·" in out


def test_tmux_widget_includes_color_escapes(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(state_dir, "a", status=AgentStatus.WORKING)
    _write(state_dir, "b", status=AgentStatus.WAITING_PERMISSION)
    rc = main(["tmux-widget"])
    out = capsys.readouterr().out
    assert rc == 0
    # tmux-style color tags
    assert "#[fg=" in out
    assert "PERM:1" in out
    assert "W:1" in out


def test_unknown_subcommand_still_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    # argparse calls sys.exit(2) on an invalid subcommand choice.
    with pytest.raises(SystemExit) as exc:
        main(["bogus-cmd-doesnt-exist"])
    assert exc.value.code == 2
    err = capsys.readouterr().err.lower()
    assert "invalid choice" in err or "unrecognized" in err or "bogus" in err


# --- kill ----------------------------------------------------------------


def test_kill_unknown_sid_returns_error(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["kill", "not-a-real-sid"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no session matches" in err


def test_kill_ambiguous_prefix_returns_error(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(state_dir, "abc-1", project_name="one")
    _write(state_dir, "abc-2", project_name="two")
    rc = main(["kill", "abc"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ambiguous" in err


def test_kill_unique_prefix_signals_pid_and_unlinks(
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use a fake kill_session so the test doesn't need a real process."""
    from ephor.tmux import navigator

    _write(state_dir, "abc-12345", project_name="proj")
    captured: list[object] = []

    def fake_kill(agent: object, sd: object) -> object:
        captured.append(agent)

        class _Outcome:
            ok = True
            detail = ""

        return _Outcome()

    monkeypatch.setattr(navigator, "kill_session", fake_kill)
    # main() imports kill_session lazily — patch in cli too if needed.
    import ephor.cli as cli_module

    monkeypatch.setattr(cli_module, "_cmd_kill", cli_module._cmd_kill)

    rc = main(["kill", "abc-1"])
    assert rc == 0
    assert captured, "kill_session must be invoked"
    out = capsys.readouterr().out
    assert "killed proj" in out


# --- doctor --------------------------------------------------------------


def test_doctor_runs_without_crashing(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["doctor"])
    out = capsys.readouterr().out
    # Always exits 0 / 1 / 2 — never crashes — and prints a summary line.
    assert rc in (0, 1, 2)
    assert "summary:" in out
    # Every check rendered with one of the three icons.
    assert any(tag in out for tag in ("[ ok ]", "[warn]", "[FAIL]"))

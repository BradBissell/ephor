"""Smoke tests to verify P0 install + CLI plumbing."""

from __future__ import annotations

import subprocess
import sys

import ephor
from ephor.cli import main


def test_version_attribute() -> None:
    assert ephor.__version__ == "0.2.0"


def test_main_no_args_launches_tui(monkeypatch) -> None:
    # Bare `ephor` is the dashboard launcher. Stub the TUI runner so the test
    # doesn't start Textual; just assert main() dispatches to it.
    import ephor.cli as cli

    called = {}

    def fake_tui() -> int:
        called["tui"] = True
        return 0

    monkeypatch.setattr(cli, "_cmd_tui", fake_tui)
    rc = main([])
    assert rc == 0
    assert called.get("tui") is True


def test_unknown_subcommand_exits_two(capsys) -> None:
    import pytest

    # argparse calls sys.exit(2) on an invalid choice → SystemExit.
    with pytest.raises(SystemExit) as exc:
        main(["bogus-subcommand-that-does-not-exist"])
    assert exc.value.code == 2
    err = capsys.readouterr().err.lower()
    assert "invalid choice" in err or "unrecognized" in err or "bogus" in err


def test_version_via_subprocess() -> None:
    """End-to-end: invoking the installed entrypoint prints the version."""
    result = subprocess.run(
        [sys.executable, "-m", "ephor", "--version"],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert "ephor 0.2.0" in result.stdout

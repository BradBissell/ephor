"""Tests for the Jira-ticket extraction helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claude_orchestrator import jira as jira_module
from claude_orchestrator.jira import extract_ticket, ticket_for_cwd


def test_extract_ticket_finds_key_at_start() -> None:
    assert extract_ticket("DR-8222") == "DR-8222"


def test_extract_ticket_finds_key_in_branch_name() -> None:
    assert extract_ticket("feature/DR-8222-add-thing") == "DR-8222"


def test_extract_ticket_finds_key_in_path() -> None:
    assert extract_ticket("/home/me/projects/work/aim-myt/DR-1234/src") == "DR-1234"


def test_extract_ticket_returns_none_when_absent() -> None:
    assert extract_ticket("main") is None
    assert extract_ticket("") is None
    assert extract_ticket(None) is None


def test_extract_ticket_rejects_lowercase() -> None:
    assert extract_ticket("feature/dr-8222") is None


def test_extract_ticket_handles_multi_letter_project() -> None:
    assert extract_ticket("ABC-1") == "ABC-1"
    assert extract_ticket("PROJ123-99") == "PROJ123-99"


def test_ticket_for_cwd_returns_none_for_nonexistent_when_no_match(tmp_path: Path) -> None:
    assert ticket_for_cwd(tmp_path / "no-such-dir") is None


def test_ticket_for_cwd_extracts_from_dir_name(tmp_path: Path) -> None:
    target = tmp_path / "DR-7777"
    target.mkdir()
    # Branch lookup will fail (not a git repo) — must still find from path.
    assert ticket_for_cwd(target) == "DR-7777"


def test_ticket_for_cwd_prefers_branch_over_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "DR-1111"
    target.mkdir()

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=list(args),  # type: ignore[arg-type]
            returncode=0,
            stdout="feature/DR-2222-do-stuff\n",
            stderr="",
        )

    monkeypatch.setattr(jira_module.subprocess, "run", fake_run)
    # Branch wins because it's a fresher signal than the directory name.
    assert ticket_for_cwd(target) == "DR-2222"


def test_ticket_for_cwd_handles_none() -> None:
    assert ticket_for_cwd(None) is None

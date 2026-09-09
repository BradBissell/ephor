"""Tests for the Jira-ticket extraction helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ephor import jira as jira_module
from ephor.jira import extract_ticket, ticket_for_agent, ticket_for_cwd
from ephor.state.models import AgentState


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


@pytest.fixture(autouse=True)
def _clear_cwd_cache() -> None:
    """Memoized cwd→ticket answers must not leak between tests."""
    jira_module.clear_cwd_cache()


def test_extract_ticket_skips_encoding_lookalikes() -> None:
    assert extract_ticket("fixed UTF-8 decoding in the parser") is None
    assert extract_ticket("bumped hashing to SHA-256") is None
    assert extract_ticket("normalized timestamps to ISO-8601") is None


def test_extract_ticket_finds_real_key_after_a_lookalike() -> None:
    assert extract_ticket("UTF-8 fix for DR-4242") == "DR-4242"


def test_ticket_for_cwd_memoizes_the_branch_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row renderer calls this twice a second per session — one fork only."""
    target = tmp_path / "workdir"
    target.mkdir()
    calls: list[int] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return subprocess.CompletedProcess(
            args=list(args),  # type: ignore[arg-type]
            returncode=0,
            stdout="feature/DR-3333-thing\n",
            stderr="",
        )

    monkeypatch.setattr(jira_module.subprocess, "run", fake_run)
    assert ticket_for_cwd(target) == "DR-3333"
    assert ticket_for_cwd(target) == "DR-3333"
    assert ticket_for_cwd(target) == "DR-3333"
    assert len(calls) == 1
    # use_cache=False re-probes, and clear_cwd_cache() drops the memo.
    assert ticket_for_cwd(target, use_cache=False) == "DR-3333"
    assert len(calls) == 2


def _agent(**kwargs: object) -> AgentState:
    base: dict[str, object] = {
        "session_id": "sid-1",
        "cwd": "/tmp/not-a-ticket-dir",
        "started_at": "2026-01-01T00:00:00Z",
    }
    base.update(kwargs)
    return AgentState(**base)  # type: ignore[arg-type]


def test_ticket_for_agent_prefers_cwd_over_prose(tmp_path: Path) -> None:
    target = tmp_path / "DR-1000"
    target.mkdir()
    agent = _agent(cwd=str(target), tmux_window="DR-2000", last_summary="work on DR-3000")
    assert ticket_for_agent(agent, summary="DR-4000: doing things") == "DR-1000"


def test_ticket_for_agent_falls_back_to_tmux_window(tmp_path: Path) -> None:
    plain = tmp_path / "shared-checkout"
    plain.mkdir()
    agent = _agent(cwd=str(plain), tmux_window="DR-2000-implement", last_summary="do the thing")
    assert ticket_for_agent(agent) == "DR-2000"


def test_ticket_for_agent_falls_back_to_user_prompt(tmp_path: Path) -> None:
    plain = tmp_path / "shared-checkout"
    plain.mkdir()
    agent = _agent(cwd=str(plain), last_summary="implement DR-3000 end to end")
    assert ticket_for_agent(agent) == "DR-3000"


def test_ticket_for_agent_uses_llm_summary_last(tmp_path: Path) -> None:
    plain = tmp_path / "shared-checkout"
    plain.mkdir()
    agent = _agent(cwd=str(plain))
    assert ticket_for_agent(agent, summary="DR-4000: refactoring the parser") == "DR-4000"


def test_ticket_for_agent_returns_none_with_no_signal(tmp_path: Path) -> None:
    plain = tmp_path / "shared-checkout"
    plain.mkdir()
    agent = _agent(cwd=str(plain), last_summary="fix the UTF-8 bug")
    assert ticket_for_agent(agent, summary="tidying up the tests") is None

"""Tests for the Jira-ticket extraction helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ephor import gitinfo as gitinfo_module
from ephor import jira as jira_module
from ephor.jira import (
    TicketSource,
    extract_ticket,
    match_for_agent,
    ticket_for_agent,
    ticket_for_cwd,
)
from ephor.state.models import AgentState


def _fake_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    branch: str = "main",
    toplevel: str = "/repo",
    upstream: str = "",
    commits: str = "",
    calls: list[list[str]] | None = None,
) -> None:
    """Stub every git probe gitinfo makes, dispatching on the argv.

    One helper for all of them because the probes now share a module: a
    test that only cares about the branch still has to answer the upstream
    and log calls that follow it.
    """

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(list(args))
        if "log" in args:
            out = commits
        elif "@{u}" in args:
            out = upstream
        else:
            out = f"{branch}\n{toplevel}\n"
        return subprocess.CompletedProcess(
            args=args,
            returncode=0 if out else 1,
            stdout=out,
            stderr="",
        )

    monkeypatch.setattr(gitinfo_module.subprocess, "run", fake_run)


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

    _fake_git(monkeypatch, branch="feature/DR-2222-do-stuff", toplevel=str(target))
    # Branch wins because it's a fresher signal than the directory name.
    assert ticket_for_cwd(target) == "DR-2222"


def test_ticket_for_cwd_handles_none() -> None:
    assert ticket_for_cwd(None) is None


@pytest.fixture(autouse=True)
def _clear_cwd_cache() -> None:
    """Memoized cwd→ticket and git answers must not leak between tests."""
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
    calls: list[list[str]] = []
    _fake_git(monkeypatch, branch="feature/DR-3333-thing", toplevel=str(target), calls=calls)
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


# ---- the git tier, against real repositories -------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True)


def _repo(path: Path, *, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    (path / "README").write_text("hi\n")
    _git(path, "add", "README")
    _git(path, "commit", "-qm", "chore: initial")
    return path


def test_match_for_cwd_reports_the_branch_as_its_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "proj")
    _git(repo, "checkout", "-q", "-b", "feature/DR-2222-do-stuff")
    match = jira_module.match_for_cwd(repo)
    assert match is not None
    assert (match.key, match.source) == ("DR-2222", TicketSource.BRANCH)
    assert match.confident


def test_match_for_cwd_falls_back_to_branch_commits(tmp_path: Path) -> None:
    """The case the old chain missed: a conventionally-named branch."""
    repo = _repo(tmp_path / "proj")
    _git(repo, "checkout", "-q", "-b", "fix/refresh-tick-missing-listview")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "feat(DR-4242): harvest tickets from commit subjects")
    match = jira_module.match_for_cwd(repo)
    assert match is not None
    assert (match.key, match.source) == ("DR-4242", TicketSource.COMMIT)


def test_match_for_cwd_ignores_an_ancestor_outside_the_worktree(tmp_path: Path) -> None:
    """A worktree living under an unrelated DR-shaped directory.

    Scanning every path component let the ancestor claim every session in
    the checkout; the scan now stops at the worktree root.
    """
    repo = _repo(tmp_path / "DR-100-scratch" / "proj")
    nested = repo / "src"
    nested.mkdir()
    assert jira_module.match_for_cwd(nested) is None


def test_match_for_cwd_still_reads_the_worktree_directory_name(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "aim-myt" / "DR-8222")
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    match = jira_module.match_for_cwd(nested)
    assert match is not None
    assert (match.key, match.source) == ("DR-8222", TicketSource.PATH)


def test_match_for_cwd_uses_the_upstream_when_the_local_branch_is_generic(
    tmp_path: Path,
) -> None:
    origin = _repo(tmp_path / "origin")
    _git(origin, "checkout", "-q", "-b", "DR-5150-remote-name")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "-b", "DR-5150-remote-name", str(origin), str(clone)],
        capture_output=True,
        check=True,
    )
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "T")
    # Rename the local branch to something that says nothing; the upstream
    # still points at the ticket-named remote branch.
    _git(clone, "branch", "-m", "work")
    match = jira_module.match_for_cwd(clone)
    assert match is not None
    assert (match.key, match.source) == ("DR-5150", TicketSource.UPSTREAM)


# ---- declarations outrank inference ---------------------------------------


def test_declared_ticket_beats_the_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "proj")
    _git(repo, "checkout", "-q", "-b", "feature/DR-1-wrong")
    agent = _agent(cwd=str(repo), ticket="DR-9000")
    match = match_for_agent(agent)
    assert match is not None
    assert (match.key, match.source) == ("DR-9000", TicketSource.DECLARED)


def test_a_pin_beats_everything(tmp_path: Path) -> None:
    from ephor import ticket_pins

    repo = _repo(tmp_path / "proj")
    _git(repo, "checkout", "-q", "-b", "feature/DR-1-wrong")
    agent = _agent(session_id="sid-pinned", cwd=str(repo), ticket="DR-9000")
    ticket_pins.pin("sid-pinned", "DR-7777")
    match = match_for_agent(agent)
    assert match is not None
    assert (match.key, match.source) == ("DR-7777", TicketSource.PIN)
    ticket_pins.unpin("sid-pinned")
    assert ticket_for_agent(agent) == "DR-9000"


# ---- the PR as a ticket source --------------------------------------------


def test_ticket_is_read_back_out_of_the_pull_request(tmp_path: Path) -> None:
    from ephor.github import PullRequest

    plain = tmp_path / "shared-checkout"
    plain.mkdir()
    agent = _agent(cwd=str(plain))
    pr = PullRequest(
        number=3,
        url="https://example.com/3",
        state="OPEN",
        title="DR-6060 make the thing work",
    )
    match = match_for_agent(agent, pr=pr)
    assert match is not None
    assert (match.key, match.source) == ("DR-6060", TicketSource.PR)
    assert match.confident


def test_pr_does_not_override_a_git_derived_key(tmp_path: Path) -> None:
    from ephor.github import PullRequest

    repo = _repo(tmp_path / "proj")
    _git(repo, "checkout", "-q", "-b", "DR-1111-real")
    agent = _agent(cwd=str(repo))
    pr = PullRequest(number=3, url="u", state="OPEN", title="DR-6060 something else")
    assert ticket_for_agent(agent, pr=pr) == "DR-1111"


# ---- confidence ------------------------------------------------------------


def test_prose_sources_are_not_confident(tmp_path: Path) -> None:
    plain = tmp_path / "shared-checkout"
    plain.mkdir()
    for kwargs, expected in (
        ({"tmux_window": "DR-2000-implement"}, TicketSource.TMUX),
        ({"last_summary": "implement DR-3000"}, TicketSource.PROMPT),
    ):
        agent = _agent(cwd=str(plain), **kwargs)
        match = match_for_agent(agent)
        assert match is not None
        assert match.source is expected
        assert not match.confident


def test_llm_summary_is_the_least_confident_source(tmp_path: Path) -> None:
    plain = tmp_path / "shared-checkout"
    plain.mkdir()
    match = match_for_agent(_agent(cwd=str(plain)), summary="DR-4000: refactoring")
    assert match is not None
    assert match.source is TicketSource.SUMMARY
    assert not match.confident


# ---- the project allowlist -------------------------------------------------


def test_allowlist_rejects_a_key_from_another_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPHOR_TICKET_PROJECTS", "DR, ABC")
    assert extract_ticket("ABC-12 and DR-1") == "ABC-12"
    assert extract_ticket("bumped to XYZ-9000") is None
    # And it subsumes the denylist without needing a new entry each year.
    assert extract_ticket("patched CVE-2031 in the parser") is None


def test_allowlist_is_case_insensitive_on_the_configured_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EPHOR_TICKET_PROJECTS", "dr")
    assert extract_ticket("fix DR-42") == "DR-42"


def test_denylist_still_applies_when_no_projects_are_configured() -> None:
    assert extract_ticket("bumped hashing to SHA-256") is None

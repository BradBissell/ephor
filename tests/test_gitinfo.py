"""Tests for the git probe layer, against real repositories.

These shell out to the real `git` rather than stubbing subprocess: the whole
point of the module is that specific invocations behave a particular way
(`--abbrev-ref HEAD` printing the literal "HEAD" when detached, `--not <trunk>`
yielding an empty range on the trunk itself), and a stub would assert our
belief about git instead of git's behaviour.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ephor import gitinfo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    gitinfo.clear_cache()


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _repo(path: Path, *, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    (path / "README").write_text("hi\n")
    _git(path, "add", "README")
    _git(path, "commit", "-qm", "chore: initial commit")
    return path


def _commit(repo: Path, message: str, filename: str = "f.txt") -> None:
    (repo / filename).write_text(message)
    _git(repo, "add", filename)
    _git(repo, "commit", "-qm", message)


def test_context_reports_branch_and_toplevel(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "proj")
    ctx = gitinfo.context(repo)
    assert ctx.branch == "main"
    assert ctx.toplevel is not None
    assert Path(ctx.toplevel).resolve() == repo.resolve()
    assert ctx.is_repo
    assert not ctx.detached


def test_context_from_a_subdirectory_still_finds_the_worktree_root(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "proj")
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    ctx = gitinfo.context(nested)
    assert ctx.toplevel is not None
    assert Path(ctx.toplevel).resolve() == repo.resolve()


def test_context_flags_a_detached_head_instead_of_calling_it_a_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "proj")
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    ctx = gitinfo.context(repo)
    # `rev-parse --abbrev-ref HEAD` prints the string "HEAD" here; treating
    # that as a branch name is the bug this guards.
    assert ctx.detached
    assert ctx.branch is None
    assert ctx.is_repo


def test_context_is_empty_outside_a_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    ctx = gitinfo.context(plain)
    assert not ctx.is_repo
    assert ctx.branch is None


def test_context_is_empty_for_missing_and_none(tmp_path: Path) -> None:
    assert not gitinfo.context(tmp_path / "nope").is_repo
    assert not gitinfo.context(None).is_repo


def test_context_memoizes_until_cleared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path / "proj")
    assert gitinfo.context(repo).branch == "main"
    _git(repo, "checkout", "-q", "-b", "DR-1/x")
    # Still the memoized answer — the TTL is the staleness budget.
    assert gitinfo.context(repo).branch == "main"
    assert gitinfo.context(repo, use_cache=False).branch == "DR-1/x"


def test_branch_commit_text_reads_commits_unique_to_the_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "proj")
    _git(repo, "checkout", "-q", "-b", "fix/no-key-in-the-name")
    _commit(repo, "feat(DR-8222): harvest tickets from commits")
    text = gitinfo.branch_commit_text(repo)
    assert text is not None
    assert "DR-8222" in text


def test_branch_commit_text_is_empty_on_the_trunk(tmp_path: Path) -> None:
    """A session on main must not inherit a key from unrelated history."""
    repo = _repo(tmp_path / "proj")
    _commit(repo, "feat(DR-1234): something that landed ages ago")
    assert gitinfo.branch_commit_text(repo) is None


def test_branch_commit_text_survives_a_repo_without_the_candidate_trunks(
    tmp_path: Path,
) -> None:
    """--ignore-missing is what keeps the single invocation working here."""
    repo = _repo(tmp_path / "proj", branch="trunk")
    _commit(repo, "feat(DR-7): on a branch nobody named main")
    # No main/master/develop exists; the command must still succeed rather
    # than erroring out on the unknown revisions.
    text = gitinfo.branch_commit_text(repo)
    assert text is not None
    assert "DR-7" in text


def test_branch_commit_text_is_none_outside_a_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gitinfo.branch_commit_text(plain) is None


def test_upstream_branch_reports_the_tracking_ref(tmp_path: Path) -> None:
    origin = _repo(tmp_path / "origin")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "T")
    assert gitinfo.upstream_branch(clone) == "origin/main"


def test_upstream_branch_is_none_without_a_remote(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "proj")
    assert gitinfo.upstream_branch(repo) is None


# --- worktree collisions ---------------------------------------------------


def test_collisions_pairs_sessions_sharing_a_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two agents in one tree interleave edits; ephor can at least say so."""
    monkeypatch.setattr(
        gitinfo,
        "worktree_of",
        lambda cwd: {"/a": "/repo", "/b": "/repo", "/c": "/other"}.get(str(cwd)),
    )
    found = gitinfo.collisions([("s1", "/a"), ("s2", "/b"), ("s3", "/c")])
    assert found == {"s1": ("s2",), "s2": ("s1",)}


def test_collisions_ignores_non_repo_and_unknown_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitinfo, "worktree_of", lambda cwd: None)
    assert gitinfo.collisions([("s1", "/a"), ("s2", "/b")]) == {}
    assert gitinfo.collisions([("s1", None), ("", "/a")]) == {}


def test_collisions_reports_every_member_of_a_larger_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gitinfo, "worktree_of", lambda cwd: "/repo")
    found = gitinfo.collisions([("s1", "/a"), ("s2", "/b"), ("s3", "/c")])
    assert set(found) == {"s1", "s2", "s3"}
    assert set(found["s1"]) == {"s2", "s3"}


def test_worktree_of_resolves_a_real_checkout(tmp_path: Path) -> None:
    gitinfo.clear_cache()
    assert gitinfo.worktree_of(tmp_path) is None
    assert gitinfo.worktree_of(None) is None

"""Launcher: ticket validation, naming conventions, worktree and window setup."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ephor import launcher, work_items


def test_normalize_ticket_accepts_keys_and_rejects_everything_else() -> None:
    assert launcher.normalize_ticket("dr-8222") == "DR-8222"
    assert launcher.normalize_ticket("  DR-8222 ") == "DR-8222"
    assert launcher.normalize_ticket("not-a-key") is None
    assert launcher.normalize_ticket("DR-") is None
    assert launcher.normalize_ticket("") is None


def test_branch_name_slugifies_the_title() -> None:
    assert (
        launcher.branch_name("DR-1", "Add retry to upload client")
        == "DR-1-add-retry-to-upload-client"
    )
    assert launcher.branch_name("DR-1", "") == "DR-1"
    # Punctuation and leading/trailing separators never reach the ref name.
    assert launcher.branch_name("DR-1", "  fix: the thing!  ") == "DR-1-fix-the-thing"


def test_worktree_path_is_a_sibling_by_default(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    assert launcher.worktree_path(repo, "DR-1") == tmp_path / "myrepo-DR-1"


def test_worktree_root_env_overrides_the_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPHOR_WORKTREE_ROOT", str(tmp_path / "trees"))
    repo = tmp_path / "myrepo"
    assert launcher.worktree_path(repo, "DR-1") == tmp_path / "trees" / "myrepo-DR-1"


def test_start_refuses_a_non_ticket() -> None:
    result = launcher.start("nonsense", dry_run=True)
    assert result.ok is False
    assert "not a ticket key" in result.message


def test_start_refuses_an_unknown_provider(tmp_path: Path) -> None:
    result = launcher.start("DR-1", provider="notanagent", cwd=".", dry_run=True)
    assert result.ok is False
    assert "unknown provider" in result.message


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin",
    }
    for args in (
        ["init", "-b", "main"],
        ["commit", "--allow-empty", "-m", "root"],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=env)


def test_ensure_worktree_creates_then_reuses(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    target, message = launcher.ensure_worktree(repo, "DR-1", title="add thing", base="main")
    assert target is not None and target.is_dir()
    assert "created worktree" in message

    # A second start on the same ticket must land in the tree that already
    # holds its commits, not a fresh one off today's trunk.
    again, message = launcher.ensure_worktree(repo, "DR-1", base="main")
    assert again == target
    assert "reusing" in message


def test_ensure_worktree_refuses_an_unresolvable_base(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    target, message = launcher.ensure_worktree(not_a_repo, "DR-1", base="main")
    assert target is None
    assert "does not resolve" in message


def test_ensure_worktree_refuses_a_base_that_does_not_exist(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    target, message = launcher.ensure_worktree(repo, "DR-1", base="origin/nonexistent")
    assert target is None
    assert "does not resolve" in message


def test_ensure_worktree_adopts_an_existing_branch_rather_than_resetting_it(
    tmp_path: Path,
) -> None:
    """An abandoned branch may hold unpushed commits; -B would discard them."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    env = {"PATH": "/usr/bin:/bin"}
    subprocess.run(
        ["git", "-C", str(repo), "branch", "DR-1"], check=True, capture_output=True, env=env
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "unpushed"],
        check=True,
        capture_output=True,
        env={
            **env,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    head_before = launcher._rev(repo, "DR-1")

    target, message = launcher.ensure_worktree(repo, "DR-1", base="main")
    assert target is not None
    assert "existing branch" in message
    assert launcher._rev(repo, "DR-1") == head_before


def test_ensure_worktree_records_the_base_sha_it_branched_from(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _, message = launcher.ensure_worktree(repo, "DR-1", base="main")
    assert launcher._rev(repo, "main")[:8] in message


def test_fetch_base_is_a_noop_for_a_local_ref(tmp_path: Path) -> None:
    """HEAD, a branch name or a sha has no remote to fetch — and that is fine."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert launcher.fetch_base(repo, "main") is False
    assert launcher.fetch_base(repo, "HEAD") is False
    # An unknown remote is also not fetched rather than shelling out blindly.
    assert launcher.fetch_base(repo, "nosuchremote/main") is False


def test_fetch_base_runs_for_a_real_remote(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    upstream = tmp_path / "upstream"
    _init_repo(upstream)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(upstream)],
        check=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    calls: list[list[str]] = []
    real_run = launcher._run

    def spy(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
        calls.append(args)
        return real_run(args, cwd)

    monkeypatch.setattr(launcher, "_run", spy)
    assert launcher.fetch_base(repo, "origin/main") is True
    assert any("fetch" in a for a in calls)


def test_link_env_files_symlinks_rather_than_copies(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=1")
    (repo / ".env.local").write_text("OTHER=2")
    (repo / "README.md").write_text("not an env file")
    target = tmp_path / "tree"
    target.mkdir()

    assert launcher.link_env_files(repo, target) == 2
    assert (target / ".env").is_symlink()
    assert (target / ".env").read_text() == "SECRET=1"
    assert not (target / "README.md").exists()
    # Idempotent: a second run adds nothing.
    assert launcher.link_env_files(repo, target) == 0


def test_start_records_the_work_item_and_declares_the_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    opened: dict[str, object] = {}

    def fake_window(name: str, cwd: Path, command: str, env: dict[str, str]) -> tuple[str, str]:
        opened.update({"name": name, "cwd": cwd, "command": command, "env": env})
        return "@7", "opened tmux window @7"

    monkeypatch.setattr(launcher, "open_window", fake_window)
    result = launcher.start("DR-9", title="add thing", base="main", cwd=repo)

    assert result.ok is True
    assert result.window == "@7"
    # The whole point: the agent's environment carries the key, so the hook
    # records it as fact instead of the dashboard inferring it.
    assert opened["env"]["EPHOR_TICKET"] == "DR-9"
    assert opened["name"] == "DR-9"

    item = work_items.load("DR-9")
    assert item is not None
    assert item.confirmed is True
    assert item.worktree == str(tmp_path / "repo-DR-9")


def test_resume_refuses_providers_that_cannot_resume(tmp_path: Path) -> None:
    result = launcher.resume("sid", provider="gemini", cwd=str(tmp_path))
    assert result.ok is False
    assert "no resume flag" in result.message


def test_resume_uses_the_providers_own_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_window(name: str, cwd: Path, command: str, env: dict[str, str]) -> tuple[str, str]:
        seen["command"] = command
        return "@1", "ok"

    monkeypatch.setattr(launcher, "open_window", fake_window)
    result = launcher.resume("abc123", provider="claude", cwd=str(tmp_path), ticket="DR-1")
    assert result.ok is True
    assert seen["command"] == "claude --resume abc123"


def test_resume_reports_a_vanished_worktree(tmp_path: Path) -> None:
    result = launcher.resume("sid", provider="claude", cwd=str(tmp_path / "gone"))
    assert result.ok is False
    assert "no longer exists" in result.message


# --- environment files -----------------------------------------------------


def test_env_links_cover_nested_monorepo_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A monorepo keeps .env next to the app that reads it, not at the root."""
    monkeypatch.setenv("EPHOR_ENV_LINKS", "applications/api/.env, applications/ui/.env")
    repo = tmp_path / "repo"
    (repo / "applications" / "api").mkdir(parents=True)
    (repo / "applications" / "ui").mkdir(parents=True)
    (repo / "applications" / "api" / ".env").write_text("API=1")
    (repo / "applications" / "ui" / ".env").write_text("UI=1")
    target = tmp_path / "tree"
    target.mkdir()

    assert launcher.link_env_files(repo, target) == 2
    assert (target / "applications" / "api" / ".env").read_text() == "API=1"
    assert (target / "applications" / "ui" / ".env").is_symlink()


def test_env_links_skip_paths_that_climb_out_of_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPHOR_ENV_LINKS", "../outside/.env, /etc/passwd")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / ".env").write_text("NOPE=1")
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "tree"
    target.mkdir()

    assert launcher.link_env_files(repo, target) == 0
    assert not (target / ".env").exists()


def test_env_links_ignore_a_missing_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPHOR_ENV_LINKS", "applications/api/.env")
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "tree"
    target.mkdir()
    assert launcher.link_env_files(repo, target) == 0


# --- window shape ----------------------------------------------------------


def test_no_agent_opens_a_shell_and_still_declares_the_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    seen: dict[str, object] = {}

    def fake_window(name: str, cwd: Path, command: str, env: dict[str, str]) -> tuple[str, str]:
        seen.update({"name": name, "command": command, "env": env})
        return "@3", "ok"

    monkeypatch.setattr(launcher, "open_window", fake_window)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    result = launcher.start("DR-1", base="main", cwd=repo, launch_agent=False)

    assert result.ok is True
    assert seen["command"] == "/bin/zsh"
    assert seen["env"] == {"EPHOR_TICKET": "DR-1"}


def test_window_name_can_be_overridden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        launcher,
        "open_window",
        lambda name, cwd, command, env: (seen.update({"name": name}), ("@1", "ok"))[1],
    )
    launcher.start("DR-1", base="main", cwd=repo, window_name="DR-1-add-retry")
    assert seen["name"] == "DR-1-add-retry"

"""Launcher: ticket validation, naming conventions, worktree and window setup."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ephor import gitinfo, launcher, work_items


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


def test_default_base_follows_the_remotes_default_branch(tmp_path: Path) -> None:
    """A flow's worktree branches off the repo's own trunk, not always origin/main."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            env={"PATH": "/usr/bin:/bin"},
        )

    # Nothing known about origin at all: the historical default.
    assert launcher.default_base(repo) == launcher.FALLBACK_BASE
    # No origin/HEAD, but an origin/master exists.
    git("update-ref", "refs/remotes/origin/master", "HEAD")
    assert launcher.default_base(repo) == "origin/master"
    # origin/HEAD, as a clone records it, wins.
    git("update-ref", "refs/remotes/origin/develop", "HEAD")
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
    assert launcher.default_base(repo) == "origin/develop"
    # Not a repo: still an answer.
    assert launcher.default_base(tmp_path / "missing") == launcher.FALLBACK_BASE


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

    def fake_window(
        name: str, cwd: Path, command: str, env: dict[str, str], **kwargs: object
    ) -> tuple[str, str, str]:
        opened.update({"name": name, "cwd": cwd, "command": command, "env": env, **kwargs})
        return "@7", "%7", "opened tmux window @7"

    monkeypatch.setattr(launcher, "open_window", fake_window)
    result = launcher.start("DR-9", title="add thing", base="main", cwd=repo)

    assert result.ok is True
    assert result.window == "@7"
    assert result.pane == "%7"
    # An interactive `ephor start` keeps its old shape: switch to the new
    # window, in the current session.
    assert opened["detached"] is False
    assert opened["target_session"] == ""
    # The whole point: the agent's environment carries the key, so the hook
    # records it as fact instead of the dashboard inferring it.
    assert opened["env"]["EPHOR_TICKET"] == "DR-9"
    assert opened["name"] == "DR-9"

    item = work_items.load("DR-9")
    assert item is not None
    assert item.confirmed is True
    assert item.worktree == str(tmp_path / "repo-DR-9")


def test_start_passes_extra_env_to_the_window_but_keeps_its_own_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    tree = tmp_path / "repo-DR-9"
    repo.mkdir()
    tree.mkdir()
    opened: dict[str, object] = {}

    def fake_window(
        name: str,
        cwd: Path,
        command: str,
        env: dict[str, str],
        *,
        detached: bool = False,
        target_session: str = "",
    ) -> tuple[str, str, str]:
        opened.update(name=name, cwd=cwd, env=env, detached=detached, target_session=target_session)
        return "@7", "%7", "opened tmux window @7"

    monkeypatch.setattr(gitinfo, "worktree_of", lambda cwd: str(repo))
    monkeypatch.setattr(
        gitinfo, "context", lambda cwd, use_cache=True: gitinfo.GitContext(branch="DR-9-x")
    )
    monkeypatch.setattr(
        launcher, "ensure_worktree", lambda root, ticket, title="", base="": (tree, "made")
    )
    monkeypatch.setattr(launcher, "open_window", fake_window)
    result = launcher.start(
        "DR-9",
        cwd=repo,
        prompt="/review-pr DR-9",
        window_name="flow:DR-9:review",
        detached=True,
        target_session="flows",
        extra_env={
            "EPHOR_FLOW_RUN": "DR-9.review.10-1",
            "EPHOR_FLOW_PAYLOAD": "/p.json",
            "EPHOR_TICKET": "DR-666",
            "EPHOR_PROVIDER": "evil",
        },
    )
    assert result.ok and result.pane == "%7"
    env = opened["env"]
    assert isinstance(env, dict)
    assert env["EPHOR_FLOW_RUN"] == "DR-9.review.10-1"
    assert env["EPHOR_FLOW_PAYLOAD"] == "/p.json"
    # The launcher's own keys win over anything the caller passes in.
    assert env["EPHOR_TICKET"] == "DR-9"
    assert env["EPHOR_PROVIDER"] == "claude"
    assert (opened["name"], opened["cwd"]) == ("flow:DR-9:review", tree)
    assert (opened["detached"], opened["target_session"]) == (True, "flows")


def test_resume_refuses_providers_that_cannot_resume(tmp_path: Path) -> None:
    result = launcher.resume("sid", provider="gemini", cwd=str(tmp_path))
    assert result.ok is False
    assert "no resume flag" in result.message


def test_resume_uses_the_providers_own_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_window(
        name: str, cwd: Path, command: str, env: dict[str, str], **kwargs: object
    ) -> tuple[str, str, str]:
        seen["command"] = command
        return "@1", "%1", "ok"

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

    def fake_window(
        name: str, cwd: Path, command: str, env: dict[str, str], **kwargs: object
    ) -> tuple[str, str, str]:
        seen.update({"name": name, "command": command, "env": env})
        return "@3", "%3", "ok"

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
        lambda name, cwd, command, env, **kw: (seen.update({"name": name}), ("@1", "%1", "ok"))[1],
    )
    launcher.start("DR-1", base="main", cwd=repo, window_name="DR-1-add-retry")
    assert seen["name"] == "DR-1-add-retry"


# --- detached / targeted windows (flows) ----------------------------------


def _capture_run(monkeypatch: pytest.MonkeyPatch, stdout: str = "@4 %9", code: int = 0):
    calls: list[list[str]] = []

    def fake_run(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
        calls.append(args)
        return code, stdout, "boom" if code else ""

    monkeypatch.setattr(launcher, "_run", fake_run)
    return calls


def test_open_window_by_default_switches_to_the_window_in_the_current_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_run(monkeypatch)
    window, pane, message = launcher.open_window("DR-1", tmp_path, "claude", {"A": "b c"})
    assert (window, pane) == ("@4", "%9")
    assert "@4" in message
    args = calls[0]
    assert "-d" not in args
    assert "-t" not in args
    assert args[args.index("-F") + 1] == "#{window_id} #{pane_id}"
    assert args[-1] == "exec env A='b c' claude"


def test_open_window_detached_into_a_named_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flow launch must not steal focus, and may target another session."""
    calls = _capture_run(monkeypatch)
    window, pane, _ = launcher.open_window(
        "flow:DR-1:review", tmp_path, "claude", {}, detached=True, target_session="work"
    )
    assert (window, pane) == ("@4", "%9")
    args = calls[0]
    assert "-d" in args
    # `=` forces an exact session match; tmux would otherwise fall back to a
    # prefix (`work-old`) and open the window somewhere nobody is looking.
    assert args[args.index("-t") + 1] == "=work:"
    assert args[args.index("-n") + 1] == "flow:DR-1:review"


def test_open_window_refuses_a_session_name_tmux_would_misread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_run(monkeypatch)
    for bad in ("work:1", "a.b", "has space", "-x"):
        window, pane, message = launcher.open_window("w", tmp_path, "sh", {}, target_session=bad)
        assert window is None and pane == ""
        assert "invalid tmux session" in message
    assert calls == []


def test_open_window_reports_tmux_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_run(monkeypatch, stdout="", code=1)
    window, pane, message = launcher.open_window("w", tmp_path, "sh", {})
    assert window is None and pane == ""
    assert "boom" in message


def test_open_window_tolerates_a_tmux_that_prints_no_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture_run(monkeypatch, stdout="@4")
    window, pane, _ = launcher.open_window("w", tmp_path, "sh", {})
    assert (window, pane) == ("@4", "")


def test_start_passes_detached_and_target_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    seen: dict[str, object] = {}

    def fake_window(
        name: str, cwd: Path, command: str, env: dict[str, str], **kwargs: object
    ) -> tuple[str, str, str]:
        seen.update(kwargs)
        seen["command"] = command
        return "@2", "%2", "ok"

    monkeypatch.setattr(launcher, "open_window", fake_window)
    result = launcher.start(
        "DR-1",
        base="main",
        cwd=repo,
        prompt="/review-pr DR-1",
        detached=True,
        target_session="work",
    )
    assert result.ok is True
    assert result.pane == "%2"
    assert seen["detached"] is True
    assert seen["target_session"] == "work"
    assert seen["command"] == "claude '/review-pr DR-1'"


def test_agent_invocation_quotes_the_prompt_and_declares_the_ticket() -> None:
    command, env = launcher.agent_invocation("claude", ticket="DR-1", prompt="it's $HOME")
    assert command == "claude 'it'\"'\"'s $HOME'"
    assert env == {"EPHOR_PROVIDER": "claude", "EPHOR_TICKET": "DR-1"}
    command, env = launcher.agent_invocation("claude", ticket="")
    assert command == "claude"
    assert env == {"EPHOR_PROVIDER": "claude"}
    with pytest.raises(KeyError):
        launcher.agent_invocation("notanagent", ticket="DR-1")

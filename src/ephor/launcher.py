"""Starting work, so ephor stops having to guess what the work is.

:mod:`ephor.jira` runs a nine-step inference chain to answer "which ticket is
this session working on", grades each answer's trustworthiness, and draws the
shaky ones in amber. It is good, and it exists because ephor historically had
no way to *know* — it only ever observed sessions other tools had launched.

This module is that other tool. ``ephor start DR-8222`` creates the worktree,
opens a tmux window named for the ticket, exports ``EPHOR_TICKET`` into the
agent's environment so the hook records it as fact, and writes the
:class:`~ephor.work_items.WorkItem` before the agent has run a single turn.
The inference chain does not go away — it still serves every session ephor did
not launch — but it stops being the primary path.

**The single-writer invariant is preserved.** ``docs/architecture.md`` promises
that the hook is the only writer of session state files, and that promise is
what makes the concurrency story simple enough to reason about. Nothing here
writes a session file. The launcher writes *work* records, whose only writer is
ephor itself, and hands the ticket to the agent through the environment —
where the hook picks it up on the first event and records it the way it records
everything else.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ephor import eventlog, gitinfo, work_items
from ephor.providers import get_provider

_TIMEOUT_SEC = 20.0
_TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,7}$")


@dataclass(frozen=True)
class StartResult:
    """What ``ephor start`` did, or why it did not."""

    ok: bool
    message: str
    worktree: str = ""
    branch: str = ""
    window: str = ""


def _run(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def normalize_ticket(raw: str) -> str | None:
    """Uppercased ticket key, or None when ``raw`` is not shaped like one."""
    key = (raw or "").strip().upper()
    return key if _TICKET_RE.match(key) else None


def slugify(text: str, limit: int = 40) -> str:
    """Branch-safe slug of ``text`` — lowercase, hyphens, no leading dash."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    return slug[:limit].strip("-")


def branch_name(ticket: str, title: str = "") -> str:
    """Conventional branch for a ticket: ``DR-8222`` or ``DR-8222-add-retry``."""
    slug = slugify(title)
    return f"{ticket}-{slug}" if slug else ticket


def worktree_path(repo_root: Path, ticket: str) -> Path:
    """Where a ticket's worktree goes: a sibling of the repo, named for the key.

    Sibling rather than nested, because a worktree inside the repo it came
    from is a directory git has to be told to ignore in every tool that walks
    the tree — and one of those tools is always the coding agent.

    ``$EPHOR_WORKTREE_ROOT`` overrides the parent directory for anyone whose
    checkouts are not organised this way.
    """
    override = os.environ.get("EPHOR_WORKTREE_ROOT")
    parent = Path(override).expanduser() if override else repo_root.parent
    return parent / f"{repo_root.name}-{ticket}"


def repo_root(cwd: str | Path | None = None) -> Path | None:
    """The git repo containing ``cwd`` (default: the process's own cwd)."""
    root = gitinfo.worktree_of(str(cwd) if cwd else os.getcwd())
    return Path(root) if root else None


def fetch_base(root: Path, base: str) -> bool:
    """Fetch the remote tip ``base`` names, so branching off it is not stale.

    This is the difference between a PR whose diff is the ticket and a PR
    whose diff is the ticket plus three weeks of other people's commits.
    ``origin/main`` in a checkout that has not fetched today points at
    whatever main was the last time it did, and ``git worktree add`` will
    branch off that quite happily.

    A local ref (``HEAD``, a branch name, a sha) has nothing to fetch and is
    not an error. Neither is an offline fetch — the caller verifies where the
    branch actually landed, which is the check that matters.
    """
    remote, _, ref = base.partition("/")
    if not ref or remote not in _remotes(root):
        return False
    _run(["/usr/bin/env", "git", "-C", str(root), "fetch", remote, ref])
    return True


def _remotes(root: Path) -> set[str]:
    code, out, _ = _run(["/usr/bin/env", "git", "-C", str(root), "remote"])
    if code != 0:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def _rev(root: Path, ref: str) -> str:
    code, out, _ = _run(["/usr/bin/env", "git", "-C", str(root), "rev-parse", ref])
    return out.strip() if code == 0 else ""


def branch_exists(root: Path, branch: str) -> bool:
    code, _, _ = _run(
        ["/usr/bin/env", "git", "-C", str(root), "rev-parse", "--verify", f"refs/heads/{branch}"]
    )
    return code == 0


def ensure_worktree(
    root: Path, ticket: str, *, title: str = "", base: str = "origin/main"
) -> tuple[Path | None, str]:
    """Create (or reuse) the worktree for ``ticket``. Returns (path, message).

    Reuse is the common case on a second ``ephor start`` for the same ticket —
    a resumed chunk of work belongs in the tree that already holds its commits,
    not in a fresh one branched off today's trunk.

    Three things happen in a deliberate order, and each one exists because
    skipping it produces a PR nobody wants to review:

    1. **Fetch the base** before branching, so ``origin/main`` means today's
       main rather than the last time this checkout synced.
    2. **Never reset an existing branch.** An abandoned branch from a previous
       attempt may hold commits that were never pushed; adopting it is
       recoverable, resetting it onto the base is not.
    3. **Verify where the branch landed.** If HEAD is not the base, say so and
       refuse, rather than handing back a worktree rooted somewhere else.
    """
    target = worktree_path(root, ticket)
    if target.is_dir():
        return target, f"reusing existing worktree {target}"

    branch = branch_name(ticket, title)
    fetched = fetch_base(root, base)
    base_sha = _rev(root, base)
    if not base_sha:
        return None, f"base ref {base!r} does not resolve — is it fetched, and spelled right?"

    if branch_exists(root, branch):
        # Adopt it. The branch may carry unpushed commits from an earlier
        # attempt, and `-B` would silently throw them away.
        code, _, err = _run(
            ["/usr/bin/env", "git", "-C", str(root), "worktree", "add", str(target), branch]
        )
        if code != 0:
            return None, f"git worktree add failed: {err or 'unknown error'}"
        return target, f"created worktree {target} on the existing branch {branch}"

    code, _, err = _run(
        [
            "/usr/bin/env",
            "git",
            "-C",
            str(root),
            "worktree",
            "add",
            "-b",
            branch,
            str(target),
            base,
        ]
    )
    if code != 0:
        return None, f"git worktree add failed: {err or 'unknown error'}"

    head = _rev(target, "HEAD")
    if head and head != base_sha:
        return None, (
            f"worktree {target} is on {head[:8]}, not {base} ({base_sha[:8]}) — "
            "refusing a branch rooted on the wrong base"
        )
    note = "" if fetched else " (local base, nothing to fetch)"
    return target, f"created worktree {target} on {branch} @ {base_sha[:8]}{note}"


def env_link_paths() -> tuple[str, ...]:
    """Extra repo-relative paths to symlink into a new worktree.

    The top-level ``.env*`` glob covers a single-package repo and nothing
    else. A monorepo keeps its environment files next to the applications
    that read them — ``applications/api/.env``, ``applications/ui/.env`` —
    and a worktree missing those fails on the agent's first test run, which
    is a slow and confusing way to find out.

    ``$EPHOR_ENV_LINKS`` is a comma-separated list of those paths.
    """
    raw = os.environ.get("EPHOR_ENV_LINKS") or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def link_env_files(root: Path, target: Path) -> int:
    """Symlink the repo's environment files into a new worktree.

    A fresh worktree has no untracked files, which means no ``.env`` — and an
    agent whose first action is to run the test suite discovers that the slow
    way. Symlinks rather than copies so a later edit to the real file is
    picked up by every worktree at once.

    Covers the top-level ``.env*`` glob plus whatever :func:`env_link_paths`
    names. Returns how many links were made; already-present destinations are
    left alone, so a second call adds nothing.
    """
    sources: list[tuple[Path, Path]] = []
    with contextlib.suppress(OSError):
        sources.extend((p, target / p.name) for p in root.glob(".env*") if p.is_file())
    for relative in env_link_paths():
        # A path that climbs out of the repo is a configuration mistake, not
        # an instruction to link something outside it.
        if relative.startswith("/") or ".." in Path(relative).parts:
            continue
        source = root / relative
        if source.is_file():
            sources.append((source, target / relative))

    linked = 0
    for source, destination in sources:
        if destination.exists() or destination.is_symlink():
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(source)
        except OSError:
            continue
        linked += 1
    return linked


def open_window(name: str, cwd: Path, command: str, env: dict[str, str]) -> tuple[str | None, str]:
    """Open a tmux window running ``command`` in ``cwd``. Returns (window id, msg).

    The environment is applied with ``env`` inside the window's own shell
    rather than inherited from ephor's process, because the point is to set
    ``EPHOR_TICKET`` for the *agent*, and ephor may itself have been started
    from a session that has one.
    """
    prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(env.items()))
    full = f"exec env {prefix} {command}" if prefix else f"exec {command}"
    code, out, err = _run(
        [
            "/usr/bin/env",
            "tmux",
            "new-window",
            "-P",
            "-F",
            "#{window_id}",
            "-n",
            name,
            "-c",
            str(cwd),
            full,
        ]
    )
    if code != 0:
        return None, f"tmux new-window failed: {err or 'unknown error'}"
    return out or None, f"opened tmux window {out}"


def start(
    ticket_raw: str,
    *,
    title: str = "",
    provider: str = "claude",
    base: str = "origin/main",
    cwd: str | Path | None = None,
    prompt: str = "",
    window_name: str = "",
    launch_agent: bool = True,
    dry_run: bool = False,
) -> StartResult:
    """Create the worktree, open the window, launch the agent, record the work.

    ``launch_agent=False`` opens the window on a plain shell instead. That is
    what an operator wants when they are going to drive the work themselves
    in this pane and just need somewhere to run a dev server and tail logs.
    """
    ticket = normalize_ticket(ticket_raw)
    if ticket is None:
        return StartResult(False, f"{ticket_raw!r} is not a ticket key (expected e.g. DR-8222)")

    root = repo_root(cwd)
    if root is None:
        return StartResult(
            False, "not inside a git repository — run from a checkout, or cd into one"
        )

    try:
        agent = get_provider(provider)
    except (KeyError, ValueError):
        return StartResult(False, f"unknown provider {provider!r}")

    if dry_run:
        target = worktree_path(root, ticket)
        what = agent.binary if launch_agent else "a shell"
        return StartResult(
            True,
            f"would create {target} on {branch_name(ticket, title)} and run {what}",
            worktree=str(target),
            branch=branch_name(ticket, title),
        )

    target, message = ensure_worktree(root, ticket, title=title, base=base)
    if target is None:
        return StartResult(False, message)
    link_env_files(root, target)

    if launch_agent:
        command = agent.binary
        if prompt:
            command = f"{agent.binary} {shlex.quote(prompt)}"
        env = {"EPHOR_TICKET": ticket, "EPHOR_PROVIDER": agent.name}
    else:
        # A login shell, so the operator lands in their normal environment
        # with EPHOR_TICKET already exported for whatever they start by hand.
        command = os.environ.get("SHELL") or "/bin/sh"
        env = {"EPHOR_TICKET": ticket}
    window, window_message = open_window(window_name or ticket, target, command, env)
    if window is None:
        return StartResult(False, window_message, worktree=str(target))

    context = gitinfo.context(target, use_cache=False)
    work_items.record(
        ticket,
        repo=root.name,
        branch=context.branch or branch_name(ticket, title),
        worktree=str(target),
        confirmed=True,
    )
    eventlog.append(
        eventlog.EventKind.SESSION_STARTED,
        ticket=ticket,
        detail=f"{agent.display_name} in {target.name}",
        provider=agent.name,
        worktree=str(target),
    )
    return StartResult(
        True,
        f"{message}; {window_message}",
        worktree=str(target),
        branch=context.branch or branch_name(ticket, title),
        window=window,
    )


def resume(
    session_id: str,
    *,
    provider: str,
    cwd: str,
    ticket: str = "",
    name: str = "",
) -> StartResult:
    """Reopen a finished session in a new tmux window, in its original cwd.

    Every provider that can resume takes the session id behind a flag it
    already declares in :mod:`ephor.providers`; the ones that cannot are told
    so plainly rather than launched into a fresh, confusingly-empty session.
    """
    try:
        agent = get_provider(provider)
    except (KeyError, ValueError):
        return StartResult(False, f"unknown provider {provider!r}")
    if not agent.resume_flags:
        return StartResult(False, f"{agent.display_name} has no resume flag — start a new session")
    target = Path(cwd).expanduser()
    if not target.is_dir():
        return StartResult(False, f"{cwd} no longer exists")

    command = f"{agent.binary} {agent.resume_flags[0]} {shlex.quote(session_id)}"
    env = {"EPHOR_PROVIDER": agent.name}
    if ticket:
        env["EPHOR_TICKET"] = ticket
    window, message = open_window(name or ticket or agent.name, target, command, env)
    if window is None:
        return StartResult(False, message, worktree=str(target))
    eventlog.append(
        eventlog.EventKind.SESSION_STARTED,
        session_id=session_id,
        ticket=ticket,
        detail=f"resumed {agent.display_name}",
        provider=agent.name,
    )
    return StartResult(
        True, f"resumed {session_id[:8]} — {message}", worktree=str(target), window=window
    )

"""Git facts about a working directory, memoized for the render path.

Both the Jira harvester and the PR resolver need to know the same three
things about a session's ``cwd``: which branch is checked out, which
worktree it belongs to, and (when the branch name is uninformative) what
the branch's own commits say. This module owns those probes so neither
caller forks ``git`` on its own schedule and the answers stay consistent
between them.

Everything here is best-effort: a missing ``git``, a non-repo directory, a
timeout or a detached HEAD resolve to "unknown" rather than raising.

Answers are memoized with a short TTL because the dashboard asks on every
500ms repaint for every session. The TTL is the staleness budget: a branch
switch shows up within :data:`CACHE_TTL_SEC`, which is plenty for a label
and cheap enough that the render path never notices.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# How long a resolved answer is trusted. Short enough that a branch switch
# surfaces quickly, long enough that a dozen sessions repainting twice a
# second cost at most a dozen forks per TTL window.
CACHE_TTL_SEC = 20.0

_GIT_TIMEOUT_SEC = 2.0

# Revisions treated as "the trunk" when isolating a branch's own commits.
# Passed together with --ignore-missing so the ones that don't exist in a
# given repo are silently skipped; a session sitting *on* the trunk yields
# an empty range and therefore no commit-derived ticket, which is exactly
# the behaviour we want (no inheriting a key from unrelated history).
_TRUNK_CANDIDATES = (
    "main",
    "master",
    "develop",
    "origin/main",
    "origin/master",
    "origin/develop",
)

# How many of the branch's commits to read. A ticket key, when present at
# all, is in the first commit of the branch; the cap only bounds the cost
# of a long-running branch.
_COMMIT_SCAN_LIMIT = 25


@dataclass(frozen=True)
class GitContext:
    """What a single ``git rev-parse`` tells us about a directory."""

    branch: str | None = None
    toplevel: str | None = None
    detached: bool = False

    @property
    def is_repo(self) -> bool:
        return self.toplevel is not None


_EMPTY = GitContext()

_context_cache: dict[str, tuple[GitContext, float]] = {}


def clear_cache() -> None:
    """Drop every memoized git answer (manual refresh, and tests)."""
    _context_cache.clear()


def _git(cwd: Path, args: list[str]) -> str | None:
    """Run ``git`` in ``cwd``; stdout on success, None on any failure."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["/usr/bin/env", "git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def context(cwd: str | Path | None, *, use_cache: bool = True) -> GitContext:
    """Branch + worktree root for ``cwd``, or an empty context.

    One fork answers both questions: ``rev-parse`` accepts several queries
    per invocation and prints one line each, so the common case costs a
    single subprocess instead of two.
    """
    if cwd is None:
        return _EMPTY
    key = str(cwd)
    if use_cache:
        hit = _context_cache.get(key)
        if hit is not None and hit[1] > time.monotonic():
            return hit[0]
    found = _context_uncached(cwd)
    _context_cache[key] = (found, time.monotonic() + CACHE_TTL_SEC)
    return found


def _context_uncached(cwd: str | Path) -> GitContext:
    try:
        path = Path(cwd).expanduser()
    except (TypeError, ValueError):
        return _EMPTY
    if not path.is_dir():
        return _EMPTY
    out = _git(path, ["rev-parse", "--abbrev-ref", "HEAD", "--show-toplevel"])
    if not out:
        return _EMPTY
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if len(lines) < 2:
        return _EMPTY
    raw_branch, toplevel = lines[0], lines[1]
    # `--abbrev-ref HEAD` prints the literal string "HEAD" on a detached
    # checkout. Treating that as a branch name would be harmless here but
    # misleading downstream, so record the detachment instead.
    detached = raw_branch == "HEAD"
    return GitContext(
        branch=None if detached else (raw_branch or None),
        toplevel=toplevel or None,
        detached=detached,
    )


def upstream_branch(cwd: str | Path | None) -> str | None:
    """Fully-qualified upstream of the checked-out branch, or None.

    Rescues two cases the local branch name misses: a detached HEAD, and a
    locally-generic branch (``work``, ``wip``) that was pushed to a
    ticket-named remote branch.
    """
    if cwd is None:
        return None
    try:
        path = Path(cwd).expanduser()
    except (TypeError, ValueError):
        return None
    if not path.is_dir():
        return None
    out = _git(path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if not out:
        return None
    return out.strip() or None


def branch_commit_text(cwd: str | Path | None, limit: int = _COMMIT_SCAN_LIMIT) -> str | None:
    """Subjects + bodies of the commits unique to this branch, or None.

    The range is ``HEAD`` minus every trunk candidate, so a session sitting
    on ``main`` reads back nothing rather than inheriting a key from
    unrelated history. ``--ignore-missing`` keeps the single invocation
    working in a repo that has only some of the candidate trunks.
    """
    if cwd is None:
        return None
    try:
        path = Path(cwd).expanduser()
    except (TypeError, ValueError):
        return None
    if not path.is_dir():
        return None
    out = _git(
        path,
        [
            "log",
            "--ignore-missing",
            "--no-merges",
            f"-n{max(1, limit)}",
            "--format=%s%n%b",
            "HEAD",
            "--not",
            *_TRUNK_CANDIDATES,
        ],
    )
    if not out:
        return None
    return out.strip() or None


def worktree_of(cwd: str | Path | None) -> str | None:
    """The worktree root containing ``cwd``, or None when it is not a repo.

    A thin, memoized alias over :func:`context` for callers that only want
    the identity of the checkout — which is what "are these two sessions
    about to stomp each other" reduces to.
    """
    return context(cwd).toplevel


def collisions(sessions: Sequence[tuple[str, str | None]]) -> dict[str, tuple[str, ...]]:
    """Which sessions share a worktree with which others.

    ``sessions`` is ``(session_id, cwd)`` pairs; the result maps a session id
    to the *other* ids checked out in the same worktree, and omits sessions
    that are alone in theirs.

    Two agents editing one working tree is the classic way a parallel run
    destroys an afternoon: they interleave writes to the same files, each
    one's tests see the other's half-finished edits, and the resulting diff
    belongs to neither. Isolated worktrees are the standard fix, but nothing
    enforces one — a second session started with a plain ``cd`` lands in the
    first's tree and neither agent can tell. ephor already records every
    session's cwd, so it can at least say so out loud.

    This deliberately reports rather than prevents. ephor does not own the
    sessions and a shared checkout is sometimes exactly what the user meant
    (one session reading while another writes); a locked-out agent would be
    a worse failure than a warned one.
    """
    by_worktree: dict[str, list[str]] = {}
    for session_id, cwd in sessions:
        if not session_id:
            continue
        root = worktree_of(cwd)
        if not root:
            continue
        by_worktree.setdefault(root, []).append(session_id)
    found: dict[str, tuple[str, ...]] = {}
    for ids in by_worktree.values():
        if len(ids) < 2:
            continue
        for session_id in ids:
            found[session_id] = tuple(other for other in ids if other != session_id)
    return found

"""GitHub pull-request lookup for a session's working directory.

Answers one question: *what PR does this session's work live on?* — so the
dashboard's Jira cell can be a live link straight to the review.

Two probes, in order of confidence:

  1. ``gh pr view`` in ``cwd`` — the PR whose head is the checked-out
     branch. Exact, and the common case for a worktree-per-ticket flow.
  2. ``gh pr list --search <TICKET>`` — the newest PR mentioning the Jira
     key. Covers sessions sitting on ``main`` or on a branch whose PR
     hasn't been pushed from this worktree.

Every probe is best-effort: a missing ``gh``, an unauthenticated CLI, a
non-repo directory or a timeout all resolve to "no PR" rather than raising.

Results are cached per directory with a TTL — a hit is long-lived (PR URLs
don't move), a miss expires quickly so a PR opened mid-session is picked up
without a restart. Nothing here may be called from the render path; the
subprocess costs hundreds of milliseconds. The TUI warms the cache from a
background worker and reads :meth:`PrResolver.cached` while drawing.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# A hit is effectively permanent for the life of a branch; re-probe hourly
# so a PR that gets merged/closed eventually re-colors.
HIT_TTL_SEC = 3600.0
# A miss is re-probed often — "no PR yet" flips to "PR open" the moment the
# session pushes, and the user wants the link without restarting ephor.
MISS_TTL_SEC = 90.0

_GH_TIMEOUT_SEC = 8.0
_PR_FIELDS = "number,url,state,title,isDraft"


@dataclass(frozen=True)
class PullRequest:
    """The bits of a PR the dashboard cares about."""

    number: int
    url: str
    state: str  # OPEN | MERGED | CLOSED
    title: str = ""
    draft: bool = False

    @property
    def is_open(self) -> bool:
        return self.state.upper() == "OPEN"


def _gh(args: list[str], cwd: Path) -> str | None:
    """Run ``gh`` in ``cwd``; stdout on success, None on any failure."""
    env = dict(os.environ, GH_PAGER="cat", NO_COLOR="1", GH_PROMPT_DISABLED="1")
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["/usr/bin/env", "gh", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_SEC,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _parse_pr(payload: object) -> PullRequest | None:
    if not isinstance(payload, dict):
        return None
    url = payload.get("url")
    number = payload.get("number")
    if not isinstance(url, str) or not url or not isinstance(number, int):
        return None
    return PullRequest(
        number=number,
        url=url,
        state=str(payload.get("state") or ""),
        title=str(payload.get("title") or ""),
        draft=bool(payload.get("isDraft")),
    )


def pr_for_branch(cwd: Path) -> PullRequest | None:
    """PR whose head branch is checked out in ``cwd``, or None."""
    out = _gh(["pr", "view", "--json", _PR_FIELDS], cwd)
    if not out:
        return None
    try:
        return _parse_pr(json.loads(out))
    except (json.JSONDecodeError, ValueError):
        return None


def pr_for_ticket(cwd: Path, ticket: str) -> PullRequest | None:
    """Newest PR in ``cwd``'s repo mentioning ``ticket``, or None.

    Searches all states so a just-merged PR still resolves — the dashboard
    colors merged links differently rather than hiding them.
    """
    out = _gh(
        [
            "pr",
            "list",
            "--state",
            "all",
            "--search",
            ticket,
            "--limit",
            "1",
            "--json",
            _PR_FIELDS,
        ],
        cwd,
    )
    if not out:
        return None
    try:
        items = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(items, list) or not items:
        return None
    return _parse_pr(items[0])


@dataclass
class _Entry:
    pr: PullRequest | None
    expires_at: float


class PrResolver:
    """TTL cache over the ``gh`` probes, keyed by working directory.

    :meth:`cached` is render-path safe (dict lookup, never blocks).
    :meth:`resolve` shells out and is only safe from a worker thread.
    """

    def __init__(self, hit_ttl: float = HIT_TTL_SEC, miss_ttl: float = MISS_TTL_SEC) -> None:
        self._hit_ttl = hit_ttl
        self._miss_ttl = miss_ttl
        self._cache: dict[str, _Entry] = {}

    @staticmethod
    def _key(cwd: str | Path | None) -> str | None:
        if not cwd:
            return None
        try:
            return str(Path(cwd).expanduser())
        except (TypeError, ValueError):
            return None

    def cached(self, cwd: str | Path | None) -> PullRequest | None:
        """Cached PR for ``cwd``, or None if unknown/absent/expired."""
        key = self._key(cwd)
        if key is None:
            return None
        entry = self._cache.get(key)
        if entry is None or entry.expires_at <= time.monotonic():
            return None
        return entry.pr

    def needs_refresh(self, cwd: str | Path | None) -> bool:
        """True when ``cwd`` has no live cache entry and is worth probing."""
        key = self._key(cwd)
        if key is None:
            return False
        entry = self._cache.get(key)
        return entry is None or entry.expires_at <= time.monotonic()

    def resolve(self, cwd: str | Path | None, ticket: str | None = None) -> PullRequest | None:
        """Probe (or return a live cache entry) for ``cwd``. Blocking."""
        key = self._key(cwd)
        if key is None:
            return None
        entry = self._cache.get(key)
        now = time.monotonic()
        if entry is not None and entry.expires_at > now:
            return entry.pr
        path = Path(key)
        if not path.is_dir():
            self._store(key, None)
            return None
        pr = pr_for_branch(path)
        if pr is None and ticket:
            pr = pr_for_ticket(path, ticket)
        self._store(key, pr)
        return pr

    def _store(self, key: str, pr: PullRequest | None) -> None:
        ttl = self._hit_ttl if pr is not None else self._miss_ttl
        self._cache[key] = _Entry(pr=pr, expires_at=time.monotonic() + ttl)

    def invalidate(self, cwd: str | Path | None = None) -> None:
        """Drop one directory's entry, or the whole cache when ``cwd`` is None."""
        if cwd is None:
            self._cache.clear()
            return
        key = self._key(cwd)
        if key is not None:
            self._cache.pop(key, None)

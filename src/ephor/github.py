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

Results are cached per checkout *branch* — ``<worktree>@<branch>``, not
``cwd`` — with a TTL: a hit is long-lived (PR URLs don't move), a miss
expires quickly so a PR opened mid-session is picked up without a restart.
The branch in the key is what makes a shared checkout behave: a PR belongs
to a branch, so switching branches has to miss the cache rather than serve
the previous branch's review for the rest of the hour-long hit TTL.

Nothing here may be called from the render path; the subprocess costs
hundreds of milliseconds. The TUI warms the cache from a background worker
and reads :meth:`PrResolver.cached` while drawing.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ephor import gitinfo
from ephor.constants import WORK_ATTENTION_PRIORITY, CiState, ReviewState, WorkAttention

# A closed or merged PR is finished work: its URL and state will not change
# again, so hold it for an hour and stop asking.
HIT_TTL_SEC = 3600.0
# A miss is re-probed often — "no PR yet" flips to "PR open" the moment the
# session pushes, and the user wants the link without restarting ephor.
MISS_TTL_SEC = 90.0
# An *open* PR is live work: CI flips, reviews land, the branch goes stale.
# The dashboard is only as honest as this number, so it is minutes not hours.
OPEN_TTL_SEC = 180.0
# Checks that are still running resolve on their own clock, usually within a
# few minutes. Re-probe on that timescale so a green build lands on the board
# roughly when it lands on GitHub.
PENDING_TTL_SEC = 60.0

_GH_TIMEOUT_SEC = 8.0
# `body` and `headRefName` cost nothing extra on a call we already make,
# and they let the Jira harvester read a ticket key back *out* of the PR for
# sessions whose branch name carries none. See ephor.jira.match_for_agent.
#
# The review/CI fields are the same story: `gh` resolves them in the one
# round-trip we were already paying for, and they are what turns the ticket
# cell from a link into a *work state* — a session that finished and left red
# CI behind is the most urgent row on the board, and without these it renders
# as a quiet IDLE row. See ephor.constants.WorkAttention.
_PR_FIELDS = (
    "number,url,state,title,isDraft,body,headRefName,"
    "statusCheckRollup,reviewDecision,reviewRequests,mergeStateStatus,mergeable"
)


@dataclass(frozen=True)
class PullRequest:
    """The bits of a PR the dashboard cares about."""

    number: int
    url: str
    state: str  # OPEN | MERGED | CLOSED
    title: str = ""
    draft: bool = False
    body: str = ""
    head_ref: str = ""
    ci: CiState = CiState.NONE
    review: ReviewState = ReviewState.NONE
    failing_checks: tuple[str, ...] = ()
    merge_state: str = ""  # CLEAN | BLOCKED | DIRTY | BEHIND | UNSTABLE | UNKNOWN
    mergeable: str = ""  # MERGEABLE | CONFLICTING | UNKNOWN
    reviewers_requested: int = 0

    @property
    def is_open(self) -> bool:
        return self.state.upper() == "OPEN"

    @property
    def has_conflict(self) -> bool:
        """True when GitHub says this branch can no longer merge cleanly.

        Two fields answer this and they disagree in different situations, so
        either one asserting a conflict is taken at face value: ``mergeable``
        is computed lazily and reads ``UNKNOWN`` right after a push, while
        ``mergeStateStatus`` distinguishes a genuine ``DIRTY`` tree from the
        merely ``BLOCKED`` state of an un-approved branch.
        """
        return self.mergeable.upper() == "CONFLICTING" or self.merge_state.upper() == "DIRTY"

    def attention(self) -> WorkAttention | None:
        """The most urgent thing this PR needs from a human, or None.

        Only open PRs generate attention: a merged or closed PR is finished
        work and its red CI is history, not a task.
        """
        if not self.is_open:
            return None
        found = {
            WorkAttention.CI_FAILED: self.ci is CiState.FAILING,
            WorkAttention.CONFLICT: self.has_conflict,
            WorkAttention.CHANGES_REQUESTED: self.review is ReviewState.CHANGES_REQUESTED,
            # A draft is a work-in-progress the author has not asked anyone to
            # look at, so a pending review on one is not yet a human's problem.
            WorkAttention.REVIEW_REQUESTED: (
                not self.draft
                and self.review is ReviewState.REVIEW_REQUIRED
                and self.reviewers_requested > 0
            ),
        }
        for kind in WORK_ATTENTION_PRIORITY:
            if found.get(kind):
                return kind
        return None


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


# Check conclusions that mean "this check is done and it did not pass". A
# SKIPPED or NEUTRAL check is not a failure — treating it as one would light
# up the board for every conditionally-skipped job in a matrix.
_FAILING_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "ERROR"})
_PASSING_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})


def _rollup_entries(payload: dict[str, object]) -> list[dict[str, object]]:
    """The check contexts in ``statusCheckRollup``, whatever shape gh used.

    ``gh pr view`` returns a flat list of contexts; ``gh pr list`` nests the
    same contexts under ``[0].contexts.nodes``. Both reach here, so normalize
    rather than making the caller care which probe produced the payload.
    """
    raw = payload.get("statusCheckRollup")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        nested = item.get("contexts")
        if isinstance(nested, dict) and isinstance(nested.get("nodes"), list):
            entries.extend(n for n in nested["nodes"] if isinstance(n, dict))
        else:
            entries.append(item)
    return entries


def _check_name(entry: dict[str, object]) -> str:
    """Display name of a check context — CheckRun uses `name`, Status uses `context`."""
    for key in ("name", "context"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return "check"


def _parse_checks(payload: dict[str, object]) -> tuple[CiState, tuple[str, ...]]:
    """Roll the per-check results up into one verdict plus the failing names.

    A single failure decides the whole rollup, which matches how a human reads
    a PR page: one red check means the branch is not mergeable yet, however
    many green ones sit beside it.
    """
    entries = _rollup_entries(payload)
    if not entries:
        return CiState.NONE, ()
    failing: list[str] = []
    pending = False
    for entry in entries:
        # CheckRun carries status+conclusion; the older StatusContext carries
        # a single `state`. Read whichever this entry has.
        status = str(entry.get("status") or "").upper()
        conclusion = str(entry.get("conclusion") or "").upper()
        legacy = str(entry.get("state") or "").upper()
        verdict = conclusion or legacy
        if status and status != "COMPLETED" and not legacy:
            pending = True
            continue
        if verdict in _FAILING_CONCLUSIONS:
            failing.append(_check_name(entry))
        elif verdict in _PASSING_CONCLUSIONS:
            continue
        else:
            # PENDING, EXPECTED, QUEUED, IN_PROGRESS or something gh grew
            # since: not a failure, not yet a pass.
            pending = True
    if failing:
        return CiState.FAILING, tuple(failing)
    if pending:
        return CiState.PENDING, ()
    return CiState.PASSING, ()


def _parse_review(payload: dict[str, object]) -> tuple[ReviewState, int]:
    """Review verdict and how many reviewers are still on the hook."""
    requests = payload.get("reviewRequests")
    requested = len(requests) if isinstance(requests, list) else 0
    raw = str(payload.get("reviewDecision") or "").upper()
    try:
        decision = ReviewState(raw)
    except ValueError:
        # No decision yet. An outstanding request still means someone has
        # been asked, which is the distinction REVIEW_REQUIRED exists to make.
        decision = ReviewState.REVIEW_REQUIRED if requested else ReviewState.NONE
    return decision, requested


def _parse_pr(payload: object) -> PullRequest | None:
    if not isinstance(payload, dict):
        return None
    url = payload.get("url")
    number = payload.get("number")
    if not isinstance(url, str) or not url or not isinstance(number, int):
        return None
    ci, failing = _parse_checks(payload)
    review, requested = _parse_review(payload)
    return PullRequest(
        number=number,
        url=url,
        state=str(payload.get("state") or ""),
        title=str(payload.get("title") or ""),
        draft=bool(payload.get("isDraft")),
        body=str(payload.get("body") or ""),
        head_ref=str(payload.get("headRefName") or ""),
        ci=ci,
        review=review,
        failing_checks=failing,
        merge_state=str(payload.get("mergeStateStatus") or ""),
        mergeable=str(payload.get("mergeable") or ""),
        reviewers_requested=requested,
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
    """TTL cache over the ``gh`` probes, keyed by worktree + branch.

    :meth:`cached` is render-path safe (dict lookup, never blocks).
    :meth:`resolve` shells out and is only safe from a worker thread.
    """

    def __init__(
        self,
        hit_ttl: float = HIT_TTL_SEC,
        miss_ttl: float = MISS_TTL_SEC,
        open_ttl: float = OPEN_TTL_SEC,
    ) -> None:
        self._hit_ttl = hit_ttl
        self._miss_ttl = miss_ttl
        self._open_ttl = open_ttl
        self._cache: dict[str, _Entry] = {}

    @staticmethod
    def _key(cwd: str | Path | None) -> str | None:
        """Cache key for ``cwd`` — its worktree and branch when it has them.

        A PR belongs to a branch, so that is what the entry has to be keyed
        on. Falling back to the plain path keeps non-repo directories (and
        detached checkouts) working; they simply cache per directory as
        before. The git lookup behind this is memoized with its own short
        TTL, so this stays cheap enough for the render path.
        """
        if not cwd:
            return None
        try:
            path = str(Path(cwd).expanduser())
        except (TypeError, ValueError):
            return None
        ctx = gitinfo.context(path)
        if ctx.toplevel and ctx.branch:
            return f"{ctx.toplevel}@{ctx.branch}"
        return path

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
        # `key` may be a <worktree>@<branch> pair; the probes need the
        # directory the session is actually sitting in.
        try:
            path = Path(str(cwd)).expanduser()
        except (TypeError, ValueError):
            self._store(key, None)
            return None
        if not path.is_dir():
            self._store(key, None)
            return None
        pr = pr_for_branch(path)
        if pr is None and ticket:
            pr = pr_for_ticket(path, ticket)
        self._store(key, pr)
        return pr

    def _ttl_for(self, pr: PullRequest | None) -> float:
        """How long this answer stays trustworthy.

        A finished PR is immutable and gets the long hit TTL. An open one is
        live work whose CI and review state move underneath us, so it expires
        in minutes — and while its checks are actually running, in one.
        """
        if pr is None:
            return self._miss_ttl
        if not pr.is_open:
            return self._hit_ttl
        if pr.ci is CiState.PENDING:
            return min(self._open_ttl, PENDING_TTL_SEC)
        return self._open_ttl

    def _store(self, key: str, pr: PullRequest | None) -> None:
        self._cache[key] = _Entry(pr=pr, expires_at=time.monotonic() + self._ttl_for(pr))

    def invalidate(self, cwd: str | Path | None = None) -> None:
        """Drop one directory's entry, or the whole cache when ``cwd`` is None."""
        if cwd is None:
            self._cache.clear()
            return
        key = self._key(cwd)
        if key is not None:
            self._cache.pop(key, None)

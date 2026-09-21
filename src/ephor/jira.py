"""Jira ticket resolution for a session.

Answers "which ticket is this session working on", and says how confident
it is. The chain runs most-trustworthy first and stops at the first hit:

  0. A pin the user typed in the dashboard (:mod:`ephor.ticket_pins`).
  1. ``agent.ticket`` — the key the launcher declared via ``$EPHOR_TICKET``.
  2. The git branch checked out at ``cwd``.
  3. That branch's upstream, which rescues a detached HEAD and a locally
     generic branch (``work``) pushed to a ticket-named remote one.
  4. The path, scanned from ``cwd`` up to the *worktree root* — a
     worktree-per-ticket flow puts the key right there
     (``~/projects/work/aim-myt/DR-8222``). Stopping at the root matters:
     scanning every ancestor lets an unrelated ``~/DR-100-scratch/`` two
     levels up claim the session.
  5. The branch's own commit subjects and bodies, which catch the very
     common case of a conventionally-named branch
     (``fix/refresh-tick-missing-listview``) whose commits say
     ``feat(DR-8222): …``.
  6. The pull request's title, body and head ref, when one is already
     resolved — the ``gh`` call was paid for anyway.
  7. The tmux window label, which the start-work flow names after the
     ticket even when the session runs from a shared checkout.
  8. The session's latest user prompt ("implement DR-8222" is how most of
     these sessions begin).
  9. The LLM summary, last — it is generated text, and the summarizer
     already prefixes it with the cwd-derived key, so anything new it
     contributes is the model's own reading of the transcript.

Steps 0-6 are facts; 7-9 are readings of prose. :class:`TicketMatch` keeps
the distinction so the dashboard can render a guess differently from a
certainty instead of presenting both as settled.

All probes are best-effort. Failures return ``None``; nothing here ever
raises into a caller.
"""

from __future__ import annotations

import re
import time
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from ephor import gitinfo, ticket_pins, work_items
from ephor.config import ticket_projects

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ephor.github import PullRequest
    from ephor.state.models import AgentState

# Matches PROJECT-NUMBER. Project is 2-10 uppercase letters (Jira's own
# default), number is 1-7 digits. Anchored on word boundaries so embedded
# variants like ``feature/DR-8222-add-foo`` still match cleanly.
_TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,7})\b")


# Standards and encodings that are Jira-shaped but never Jira keys. Without
# this, an LLM summary saying "fixed UTF-8 decoding" or "bumped to AES-256"
# would light up the ticket column with a bogus key. Consulted only when
# `$EPHOR_TICKET_PROJECTS` is unset — naming your projects is strictly more
# precise than enumerating everything that isn't one, and this list can
# only ever grow (it wants a new CVE year every January).
_NOT_TICKETS = frozenset(
    {
        "UTF-8",
        "UTF-16",
        "UTF-32",
        "ISO-8601",
        "ISO-9001",
        "SHA-1",
        "SHA-256",
        "SHA-512",
        "AES-128",
        "AES-256",
        "RSA-2048",
        "RSA-4096",
        "IPV-4",
        "IPV-6",
        "HTTP-2",
        "HTTP-3",
        "BASE-64",
        "CVE-2024",
        "CVE-2025",
        "CVE-2026",
    }
)

# How long a resolved cwd->ticket answer is trusted. The row renderer asks
# for a ticket on every 500ms repaint for every session; without this the
# dashboard forks `git` per session twice a second. A branch switch still
# shows up within the TTL, which is plenty for a label.
_CWD_CACHE_TTL_SEC = 20.0


class TicketSource(StrEnum):
    """Where a ticket key came from, in descending order of authority."""

    PIN = "pin"
    DECLARED = "declared"
    BRANCH = "branch"
    UPSTREAM = "upstream"
    PATH = "path"
    COMMIT = "commit"
    PR = "pr"
    PROMOTED = "promoted"
    TMUX = "tmux"
    PROMPT = "prompt"
    SUMMARY = "summary"

    @property
    def confident(self) -> bool:
        """True when the key came from a fact rather than a reading of prose.

        A branch name, a worktree path or a commit trailer *is* the answer.
        A tmux label, a prompt or a model summary merely mentions something
        ticket-shaped, which is right most of the time and embarrassing the
        rest of it — the dashboard dims these.
        """
        return self in _CONFIDENT_SOURCES


_CONFIDENT_SOURCES = frozenset(
    {
        TicketSource.PIN,
        TicketSource.DECLARED,
        TicketSource.BRANCH,
        TicketSource.UPSTREAM,
        TicketSource.PATH,
        TicketSource.COMMIT,
        TicketSource.PR,
        # A promoted key was established by one of the sources above during
        # some earlier probe and written down. It is the same fact, read back
        # from :mod:`ephor.work_items` instead of re-derived.
        TicketSource.PROMOTED,
    }
)


class TicketMatch:
    """A resolved ticket key plus the probe that produced it."""

    __slots__ = ("key", "source")

    def __init__(self, key: str, source: TicketSource) -> None:
        self.key = key
        self.source = source

    @property
    def confident(self) -> bool:
        return self.source.confident

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TicketMatch):
            return NotImplemented
        return self.key == other.key and self.source == other.source

    def __hash__(self) -> int:
        return hash((self.key, self.source))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TicketMatch({self.key!r}, {self.source.value})"


_cwd_cache: dict[str, tuple[TicketMatch | None, float]] = {}


def extract_ticket(text: str | None) -> str | None:
    """Return the first Jira-shaped key in ``text`` or ``None``.

    When ``$EPHOR_TICKET_PROJECTS`` names the projects in play, only those
    prefixes match. Otherwise, well-known standards (``UTF-8``,
    ``SHA-256``, ...) that share the ``LETTERS-DIGITS`` shape are skipped
    so a stray mention in prose doesn't masquerade as a ticket.
    """
    if not text:
        return None
    allowed = ticket_projects()
    for m in _TICKET_RE.finditer(text):
        key = m.group(1)
        if allowed:
            if key.split("-", 1)[0].upper() not in allowed:
                continue
            return key
        if key.upper() in _NOT_TICKETS:
            continue
        return key
    return None


def clear_cwd_cache() -> None:
    """Drop every memoized answer, git facts included (manual refresh, tests)."""
    global _promotion_index_expires
    _cwd_cache.clear()
    _promotion_index_expires = 0.0
    gitinfo.clear_cache()


def match_for_cwd(cwd: str | Path | None, *, use_cache: bool = True) -> TicketMatch | None:
    """Resolve a ticket for ``cwd`` from git + path facts alone.

    Answers are memoized for ``_CWD_CACHE_TTL_SEC`` because the probes fork
    subprocesses and the dashboard calls this on every repaint. Pass
    ``use_cache=False`` to force a fresh probe, here and in the git layer
    underneath.
    """
    if cwd is None:
        return None
    key = str(cwd)
    if use_cache:
        hit = _cwd_cache.get(key)
        if hit is not None and hit[1] > time.monotonic():
            return hit[0]
    found = _match_for_cwd_uncached(cwd, use_cache=use_cache)
    _cwd_cache[key] = (found, time.monotonic() + _CWD_CACHE_TTL_SEC)
    return found


def ticket_for_cwd(cwd: str | Path | None, *, use_cache: bool = True) -> str | None:
    """The key :func:`match_for_cwd` found for ``cwd``, or ``None``.

    Safe to call with a missing / non-existent path — it just falls
    through to None.
    """
    match = match_for_cwd(cwd, use_cache=use_cache)
    return match.key if match is not None else None


def _match_for_cwd_uncached(
    cwd: str | Path | None, *, use_cache: bool = True
) -> TicketMatch | None:
    if cwd is None:
        return None
    try:
        p = Path(cwd).expanduser()
    except (TypeError, ValueError):
        return None
    if not p.exists():
        # Even if the dir is gone, the basename may still encode a key.
        found = extract_ticket(p.name) or extract_ticket(str(p))
        return TicketMatch(found, TicketSource.PATH) if found else None

    # `use_cache=False` has to reach all the way down: a caller asking for a
    # fresh answer means the branch may have moved, and gitinfo keeps its own
    # memo that would otherwise serve the stale one right back.
    ctx = gitinfo.context(p, use_cache=use_cache)

    # 1. Branch name.
    found = extract_ticket(ctx.branch)
    if found:
        return TicketMatch(found, TicketSource.BRANCH)

    # 2. Upstream — covers a detached HEAD, and a local branch named `work`
    #    that was pushed as `origin/DR-8222-thing`.
    if ctx.is_repo:
        found = extract_ticket(gitinfo.upstream_branch(p))
        if found:
            return TicketMatch(found, TicketSource.UPSTREAM)

    # 3. Path components, most-specific first. Scoped to the worktree when
    #    we know where it starts, so an unrelated ancestor can't claim the
    #    session; unscoped only when this isn't a repo at all.
    found = extract_ticket(_path_scan_text(p, ctx.toplevel))
    if found:
        return TicketMatch(found, TicketSource.PATH)

    # 4. The branch's own commits. Last in the git tier because it is the
    #    only probe that costs a second fork, and the three above already
    #    cover a worktree-per-ticket flow.
    if ctx.is_repo:
        found = extract_ticket(gitinfo.branch_commit_text(p))
        if found:
            return TicketMatch(found, TicketSource.COMMIT)
    return None


def _path_scan_text(path: Path, toplevel: str | None) -> str | None:
    """Path components to scan, most-specific first, newline-joined.

    Stops at the worktree root when there is one. ``~/projects/DR-100-old/
    ephor`` is a real shape: without the stop, every session in that
    checkout reports DR-100.
    """
    try:
        resolved = path.resolve()
    except (OSError, ValueError):
        resolved = path
    parts = list(resolved.parts)
    if toplevel:
        try:
            root = Path(toplevel).resolve()
        except (OSError, ValueError):
            root = Path(toplevel)
        if resolved == root or root in resolved.parents:
            # Keep the worktree root's own name plus anything below it.
            parts = parts[len(root.parts) - 1 :]
    return "\n".join(reversed(parts)) or None


def match_for_agent(
    agent: AgentState,
    summary: str | None = None,
    pr: PullRequest | None = None,
) -> TicketMatch | None:
    """Best-effort ticket for a dashboard session, with its provenance.

    See the module docstring for the full probe order. ``pr``, when the
    dashboard already has one cached, contributes the review's own title
    and body — free, since the ``gh`` round trip is already spent, and it
    resolves sessions whose branch name says nothing.
    """
    pinned = ticket_pins.get(getattr(agent, "session_id", None))
    if pinned:
        return TicketMatch(pinned, TicketSource.PIN)

    declared = extract_ticket(getattr(agent, "ticket", None))
    if declared:
        return TicketMatch(declared, TicketSource.DECLARED)

    found = match_for_cwd(agent.cwd)
    if found is not None:
        return found

    if pr is not None:
        from_pr = extract_ticket(f"{pr.head_ref}\n{pr.title}\n{pr.body}")
        if from_pr:
            return TicketMatch(from_pr, TicketSource.PR)

    # Nothing factual is available *now*. Something factual may have been
    # available earlier: a session whose branch has since been deleted, or
    # whose cwd was removed when its worktree was cleaned up, was once
    # resolved by git and written down. Read that back before falling through
    # to the prose tiers, so a settled answer is never re-downgraded to a
    # guess by the passage of time.
    promoted = promoted_for_session(getattr(agent, "session_id", None))
    if promoted:
        return TicketMatch(promoted, TicketSource.PROMOTED)

    for candidate, source in (
        (getattr(agent, "tmux_window", None), TicketSource.TMUX),
        (getattr(agent, "last_summary", None), TicketSource.PROMPT),
        (summary, TicketSource.SUMMARY),
    ):
        key = extract_ticket(candidate)
        if key:
            # A guess that names a key some *other* probe already established
            # for this same session is not really a guess any more — the two
            # independent signals agree. Promote it rather than drawing a
            # settled fact in amber forever.
            record = work_items.load(key)
            if record is not None and record.confirmed:
                return TicketMatch(key, TicketSource.PROMOTED)
            return TicketMatch(key, source)
    return None


def ticket_for_agent(
    agent: AgentState,
    summary: str | None = None,
    pr: PullRequest | None = None,
) -> str | None:
    """The key :func:`match_for_agent` found for this session, or ``None``."""
    match = match_for_agent(agent, summary, pr)
    return match.key if match is not None else None


# session_id -> promoted ticket, rebuilt on a short TTL. Without this the
# render path re-reads every work record for every session twice a second;
# with it, a repaint of 25 sessions costs one directory scan per TTL window.
_promotion_index: dict[str, str] = {}
_promotion_index_expires: float = 0.0


def _promotions(*, use_cache: bool = True) -> dict[str, str]:
    """Map of session id -> confirmed ticket, from the work records."""
    global _promotion_index, _promotion_index_expires
    if use_cache and _promotion_index_expires > time.monotonic():
        return _promotion_index
    index: dict[str, str] = {}
    for item in work_items.load_all().values():
        if not item.confirmed:
            continue
        for sid in item.session_ids:
            # A session id belongs to one piece of work. If two records claim
            # the same one, the first wins rather than flickering between
            # them on alternate repaints.
            index.setdefault(sid, item.key)
    _promotion_index = index
    _promotion_index_expires = time.monotonic() + _CWD_CACHE_TTL_SEC
    return index


def promoted_for_session(session_id: str | None) -> str | None:
    """The confirmed ticket a stored work record holds for ``session_id``.

    This is the read half of promotion. The write half is :func:`promote`,
    called by the dashboard whenever a fact-tier probe succeeds — so a key
    established once by a branch name survives that branch being deleted, the
    worktree being removed, and the session outliving both.
    """
    if not session_id:
        return None
    return _promotions().get(session_id)


def promote(match: TicketMatch | None, agent: AgentState) -> None:
    """Write a confident match down so it never has to be re-derived.

    A no-op for guesses: promoting a prose-tier reading would launder it into
    a fact, which is precisely the distinction :class:`TicketSource` exists to
    keep. Also a no-op for a match that is already a read-back of the record
    it would write.
    """
    if match is None or not match.confident or match.source is TicketSource.PROMOTED:
        return
    global _promotion_index_expires
    ctx = gitinfo.context(agent.cwd)
    work_items.record(
        match.key,
        session_id=getattr(agent, "session_id", None),
        branch=ctx.branch or "",
        worktree=ctx.toplevel or "",
        repo=Path(ctx.toplevel).name if ctx.toplevel else "",
        confirmed=True,
    )
    # The index this write invalidates is the one the very next repaint
    # reads, so expire it now rather than letting the TTL do it later.
    _promotion_index_expires = 0.0

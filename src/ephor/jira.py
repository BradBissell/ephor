"""Jira ticket extraction from session context.

Resolves a Jira ticket key (e.g. ``DR-8222``, ``ABC-12``) for a session.
:func:`ticket_for_cwd` covers the directory-only probes:

  1. The current branch name of the git repo at ``cwd`` (covers
     ``feature/DR-8222-…``, ``DR-8222``, ``brad/DR-8222/x``).
  2. The basename of ``cwd`` itself (worktrees often embed the key —
     e.g. ``~/projects/work/aim-myt/DR-8222``).
  3. Any ancestor directory of ``cwd`` (so a nested subdir within a
     DR-8222 worktree still resolves).

:func:`ticket_for_agent` is what the dashboard uses: it extends that chain
with the session-level signals a bare path can't see — the tmux window
label, the user's latest prompt, and finally the LLM summary. Deterministic
sources are preferred over the model's text, which is both the weakest
signal and a circular one (the summarizer already glues the cwd-derived key
onto its output, so trusting it first just re-reads our own guess).

All probes are best-effort. Failures return ``None``; nothing here ever
raises into a caller.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ephor.state.models import AgentState

# Matches PROJECT-NUMBER. Project is 2-10 uppercase letters (Jira's own
# default), number is 1-7 digits. Anchored on word boundaries so embedded
# variants like ``feature/DR-8222-add-foo`` still match cleanly.
_TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,7})\b")


# Standards and encodings that are Jira-shaped but never Jira keys. Without
# this, an LLM summary saying "fixed UTF-8 decoding" or "bumped to AES-256"
# would light up the ticket column with a bogus key. Only consulted through
# extract_ticket, which every prose probe goes through.
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
# dashboard forks a `git rev-parse` per session twice a second. A branch
# switch still shows up within the TTL, which is plenty for a label.
_CWD_CACHE_TTL_SEC = 20.0
_cwd_cache: dict[str, tuple[str | None, float]] = {}


def extract_ticket(text: str | None) -> str | None:
    """Return the first Jira-shaped key in ``text`` or ``None``.

    Skips well-known standards (``UTF-8``, ``SHA-256``, ...) that share the
    ``LETTERS-DIGITS`` shape, so a stray mention in prose doesn't masquerade
    as a ticket.
    """
    if not text:
        return None
    for m in _TICKET_RE.finditer(text):
        key = m.group(1)
        if key.upper() in _NOT_TICKETS:
            continue
        return key
    return None


def _current_branch(cwd: Path) -> str | None:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["/usr/bin/env", "git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    return branch or None


def ticket_for_cwd(cwd: str | Path | None, *, use_cache: bool = True) -> str | None:
    """Resolve a Jira ticket for ``cwd`` using branch + path heuristics.

    Returns the first match found, or ``None``. Safe to call with a
    missing / non-existent path — it just falls through to None.

    Answers are memoized for ``_CWD_CACHE_TTL_SEC`` because the branch probe
    forks a subprocess and the dashboard calls this on every repaint. Pass
    ``use_cache=False`` to force a fresh probe.
    """
    if cwd is None:
        return None
    key = str(cwd)
    if use_cache:
        hit = _cwd_cache.get(key)
        if hit is not None and hit[1] > time.monotonic():
            return hit[0]
    found = _ticket_for_cwd_uncached(cwd)
    _cwd_cache[key] = (found, time.monotonic() + _CWD_CACHE_TTL_SEC)
    return found


def clear_cwd_cache() -> None:
    """Drop every memoized cwd->ticket answer (used by tests and manual refresh)."""
    _cwd_cache.clear()


def _ticket_for_cwd_uncached(cwd: str | Path | None) -> str | None:
    if cwd is None:
        return None
    try:
        p = Path(cwd).expanduser()
    except (TypeError, ValueError):
        return None
    if not p.exists():
        # Even if the dir is gone, the basename may still encode a key.
        return extract_ticket(p.name) or extract_ticket(str(p))

    # 1. Branch name.
    branch = _current_branch(p)
    found = extract_ticket(branch)
    if found:
        return found

    # 2-3. Path components (most-specific first).
    parts = list(p.resolve().parts)
    for part in reversed(parts):
        found = extract_ticket(part)
        if found:
            return found
    return None


def ticket_for_agent(agent: AgentState, summary: str | None = None) -> str | None:
    """Best-effort Jira key for a dashboard session, or ``None``.

    Probe order, most trustworthy first:

      1. ``cwd`` — the git branch, then the path components
         (see :func:`ticket_for_cwd`).
      2. The tmux window label, which the start-work flow names after the
         ticket even when the session runs from a shared checkout.
      3. The session's latest user prompt ("implement DR-8222" is how most
         of these sessions begin).
      4. The LLM summary, last — it is generated text, and the summarizer
         already prefixes it with the cwd-derived key, so anything new it
         contributes is the model's own reading of the transcript.
    """
    found = ticket_for_cwd(agent.cwd)
    if found:
        return found
    for candidate in (
        getattr(agent, "tmux_window", None),
        getattr(agent, "last_summary", None),
        summary,
    ):
        found = extract_ticket(candidate)
        if found:
            return found
    return None

"""Jira ticket extraction from working-directory context.

Resolves a Jira ticket key (e.g. ``DR-8222``, ``ABC-12``) for a session by
inspecting, in order:

  1. The current branch name of the git repo at ``cwd`` (covers
     ``feature/DR-8222-…``, ``DR-8222``, ``brad/DR-8222/x``).
  2. The basename of ``cwd`` itself (worktrees often embed the key —
     e.g. ``~/projects/work/aim-myt/DR-8222``).
  3. Any ancestor directory of ``cwd`` (so a nested subdir within a
     DR-8222 worktree still resolves).

All probes are best-effort. Failures return ``None``; nothing here ever
raises into a caller.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Matches PROJECT-NUMBER. Project is 2-10 uppercase letters (Jira's own
# default), number is 1-7 digits. Anchored on word boundaries so embedded
# variants like ``feature/DR-8222-add-foo`` still match cleanly.
_TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,7})\b")


def extract_ticket(text: str | None) -> str | None:
    """Return the first Jira-shaped key in ``text`` or ``None``."""
    if not text:
        return None
    m = _TICKET_RE.search(text)
    return m.group(1) if m else None


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


def ticket_for_cwd(cwd: str | Path | None) -> str | None:
    """Resolve a Jira ticket for ``cwd`` using branch + path heuristics.

    Returns the first match found, or ``None``. Safe to call with a
    missing / non-existent path — it just falls through to None.
    """
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

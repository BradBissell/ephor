"""The unit of work a session is *for*, persisted across session lifetimes.

Everything else in ephor is keyed on a session: the hook writes a session
file, the TUI draws a session row, the reconciler buries a session when its
pid dies. That works right up until you ask the question you actually care
about at 25 sessions — *what happened to DR-8222?* — because the answer spans
several sessions, some of them already dead, and nothing on disk connects
them.

A :class:`WorkItem` is that connection. One record per ticket, holding the
ticket, where its code lives, its pull request, and every session id that has
touched it. It is written by whoever learns something (the launcher at
``ephor start``, the PR resolver when ``gh`` answers, the Jira probe when it
reads a status) and it survives every one of those sessions dying.

Two consequences worth naming:

*Promotion.* :mod:`ephor.jira` re-derives a session's key from scratch on
every repaint, and grades it: a key off a git branch is a fact, a key off an
LLM summary is a guess drawn in amber. Once any session's guess is
corroborated by a fact-tier signal, the fact is written here, and every later
render of that work reads back a settled answer instead of re-guessing. A key
is promoted once and stays promoted.

*Inversion.* Because the record is keyed by ticket rather than session, the
dashboard can group by work — several sessions attempting one ticket collapse
under one header — which is the thing a flat list of 25 rows cannot do.

Storage mirrors the session state files: one JSON file per key, 0600, written
by atomic rename. Unlike those, ephor itself is the only writer, so there is
no lock — a later write simply wins, and every field is last-writer-wins by
design (a newer PR state *should* beat an older one).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ephor.config import state_dir

# Bumped independently of the session-state schema: these files have a
# different writer, a different lifetime, and no hook contract to honour.
WORK_SCHEMA_VERSION = 1

# A Jira key is already filesystem-safe, but this store accepts whatever the
# harvester produced and a path is not the place to find out it was wrong.
_SAFE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,9}-\d{1,7}$")

# Keep the session list bounded. A long-lived ticket can accumulate dozens of
# resumes; the newest few are the ones anybody looks at, and the full history
# is in the event log (:mod:`ephor.eventlog`) anyway.
_MAX_SESSIONS = 32


def work_dir() -> Path:
    """Where per-ticket work records live (beside the session state dir)."""
    return state_dir().parent / "work"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class WorkItem:
    """One ticket's worth of work, and everything ephor knows about it."""

    key: str
    repo: str = ""  # worktree root, or owner/name once gh has told us
    branch: str = ""
    worktree: str = ""
    # Set once any fact-tier signal (branch, path, commits, PR) establishes
    # the key. A promoted key is never re-guessed.
    confirmed: bool = False
    pr_number: int | None = None
    pr_url: str = ""
    pr_state: str = ""  # OPEN | MERGED | CLOSED
    # Jira's own view of the ticket, when ephor has been given credentials.
    # Empty means "not asked" — never "no status".
    jira_status: str = ""
    jira_title: str = ""
    jira_assignee: str = ""
    session_ids: tuple[str, ...] = ()
    first_seen: str = field(default_factory=_now)
    last_seen: str = field(default_factory=_now)
    schema_version: int = WORK_SCHEMA_VERSION

    @property
    def has_pr(self) -> bool:
        return bool(self.pr_url)

    def to_json(self) -> str:
        raw: dict[str, Any] = asdict(self)
        raw["session_ids"] = list(self.session_ids)
        return json.dumps(raw, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkItem:
        """Build from on-disk JSON, tolerating fields a future version adds."""
        key = data.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("work item missing key")
        sessions = data.get("session_ids")
        return cls(
            key=key,
            repo=str(data.get("repo") or ""),
            branch=str(data.get("branch") or ""),
            worktree=str(data.get("worktree") or ""),
            confirmed=bool(data.get("confirmed")),
            pr_number=data.get("pr_number") if isinstance(data.get("pr_number"), int) else None,
            pr_url=str(data.get("pr_url") or ""),
            pr_state=str(data.get("pr_state") or ""),
            jira_status=str(data.get("jira_status") or ""),
            jira_title=str(data.get("jira_title") or ""),
            jira_assignee=str(data.get("jira_assignee") or ""),
            session_ids=tuple(str(s) for s in sessions) if isinstance(sessions, list) else (),
            first_seen=str(data.get("first_seen") or _now()),
            last_seen=str(data.get("last_seen") or _now()),
            schema_version=int(data.get("schema_version") or WORK_SCHEMA_VERSION),
        )


def _path_for(key: str) -> Path | None:
    """File backing ``key``, or None when the key is not one we will write."""
    if not key or not _SAFE_KEY.match(key):
        return None
    return work_dir() / f"{key.upper()}.json"


def load(key: str) -> WorkItem | None:
    """The stored record for ``key``, or None when absent or unreadable."""
    path = _path_for(key)
    if path is None:
        return None
    try:
        return WorkItem.from_dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def load_all() -> dict[str, WorkItem]:
    """Every stored record, keyed by ticket. Corrupt files are skipped."""
    directory = work_dir()
    if not directory.is_dir():
        return {}
    items: dict[str, WorkItem] = {}
    for path in directory.glob("*.json"):
        if path.name.startswith(".tmp"):
            continue
        try:
            item = WorkItem.from_dict(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            continue
        items[item.key] = item
    return items


def save(item: WorkItem) -> bool:
    """Write ``item`` atomically. False when the key or the disk says no."""
    path = _path_for(item.key)
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp.work.")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(item.to_json())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            return False
    except OSError:
        # A work record is a convenience layered over signals ephor can always
        # re-derive. Failing to persist it must never take down the dashboard.
        return False
    return True


def record(
    key: str,
    *,
    session_id: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    worktree: str | None = None,
    confirmed: bool | None = None,
    pr_number: int | None = None,
    pr_url: str | None = None,
    pr_state: str | None = None,
    jira_status: str | None = None,
    jira_title: str | None = None,
    jira_assignee: str | None = None,
) -> WorkItem | None:
    """Merge what we just learned about ``key`` into its record.

    Every parameter is optional and ``None`` means "nothing new to say" —
    which is what lets four different callers, each holding one piece of the
    picture, write to the same record without clobbering each other's fields.
    Returns the merged item, or None when ``key`` is not storable.

    ``confirmed`` is a ratchet: once True it is never set back to False, so a
    later guess cannot demote a key that a branch name already settled.
    """
    if not key or _path_for(key) is None:
        return None
    current = load(key) or WorkItem(key=key.upper())
    updates: dict[str, Any] = {"last_seen": _now()}
    for name, value in (
        ("repo", repo),
        ("branch", branch),
        ("worktree", worktree),
        ("pr_url", pr_url),
        ("pr_state", pr_state),
        ("jira_status", jira_status),
        ("jira_title", jira_title),
        ("jira_assignee", jira_assignee),
    ):
        if value:
            updates[name] = value
    if pr_number is not None:
        updates["pr_number"] = pr_number
    if confirmed:
        updates["confirmed"] = True
    if session_id:
        # Most-recent-first, deduped, bounded. The order is what makes
        # "the session that owns this ticket right now" a head lookup.
        seen = [session_id, *(s for s in current.session_ids if s != session_id)]
        updates["session_ids"] = tuple(seen[:_MAX_SESSIONS])
    merged = replace(current, **updates)
    save(merged)
    return merged


def forget(key: str) -> bool:
    """Delete ``key``'s record. True when a file was actually removed."""
    path = _path_for(key)
    if path is None:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def prune(max_age_days: float = 30.0) -> int:
    """Drop records untouched for ``max_age_days``. Returns how many went.

    Work records are small and their whole point is outliving sessions, so
    this is deliberately lazy — a month of silence means the ticket shipped
    and nobody is looking at it any more.
    """
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    directory = work_dir()
    if not directory.is_dir():
        return 0
    for path in directory.glob("*.json"):
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed

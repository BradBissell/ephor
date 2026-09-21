"""Reading a ticket's real state from Jira, and noticing when it has drifted.

:mod:`ephor.jira` answers "which ticket is this session working on" entirely
from local signals — branch names, paths, commit subjects. It never asks Jira
anything, which is why the dashboard can show you ``DR-8222`` without knowing
that DR-8222 was closed as a duplicate three days ago.

This module is the one place ephor talks to Jira, and it stays deliberately
small: given a key, return the status, the summary and the assignee. That is
enough for three things the board could not do before — show what the ticket
is actually *called* instead of the branch slug, colour the key by where it
sits in the workflow, and flag **drift**.

Drift is the payoff. "PR merged, ticket still In Progress" is a silent,
weekly, entirely invisible cost: nothing in git notices, nothing in Jira
notices, and the only thing that would have noticed is a human reading both
at once. ephor already reads both at once.

**Opt-in by construction.** ephor is otherwise a local tool, so every function
here returns ``None`` unless the user has explicitly configured credentials.
No configuration means no network call, not a failed one.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote

# Tickets move on a human timescale. Ten minutes is far fresher than the
# workflow it is tracking and keeps a 25-session board to a handful of
# requests per hour.
CACHE_TTL_SEC = 600.0
_TIMEOUT_SEC = 6.0

# Statuses that mean the ticket is finished. Compared case-insensitively
# against Jira's status *name*, and against its category key, which is the
# field that is actually stable across a team's custom workflow names.
_DEFAULT_DONE = frozenset({"done", "closed", "resolved", "released", "complete", "completed"})
_DONE_CATEGORY = "done"


@dataclass(frozen=True)
class Credentials:
    """Everything needed to read one Jira site."""

    base_url: str
    email: str
    token: str


@dataclass(frozen=True)
class Issue:
    """The bits of a Jira issue the dashboard cares about."""

    key: str
    status: str = ""
    status_category: str = ""
    title: str = ""
    assignee: str = ""

    @property
    def is_done(self) -> bool:
        """True when Jira considers this ticket finished.

        Prefers the status *category*, which Jira guarantees is one of
        to-do / in-progress / done however creatively a team has renamed the
        columns, and falls back to matching the status name for sites that
        do not return a category.
        """
        if self.status_category:
            return self.status_category.lower() == _DONE_CATEGORY
        return self.status.strip().lower() in done_statuses()


def done_statuses() -> frozenset[str]:
    """Status names that count as finished, lowercased.

    ``$EPHOR_JIRA_DONE_STATUSES`` replaces the default set for teams whose
    workflow ends somewhere other than a column called Done.
    """
    raw = os.environ.get("EPHOR_JIRA_DONE_STATUSES") or ""
    names = {part.strip().lower() for part in raw.split(",")}
    custom = frozenset(n for n in names if n)
    return custom or _DEFAULT_DONE


def credentials() -> Credentials | None:
    """Configured Jira credentials, or None when ephor should stay offline.

    ``EPHOR_JIRA_*`` wins over the ``JIRA_*`` names other tools already set,
    so pointing ephor at a second site does not disturb them.
    """
    base = os.environ.get("EPHOR_JIRA_URL") or os.environ.get("JIRA_URL") or ""
    email = os.environ.get("EPHOR_JIRA_EMAIL") or os.environ.get("JIRA_EMAIL") or ""
    token = os.environ.get("EPHOR_JIRA_TOKEN") or os.environ.get("JIRA_API_TOKEN") or ""
    if not (base and email and token):
        return None
    return Credentials(base_url=base.rstrip("/"), email=email, token=token)


def is_configured() -> bool:
    """True when a Jira lookup would actually be attempted."""
    return credentials() is not None


def _parse_issue(key: str, payload: object) -> Issue | None:
    if not isinstance(payload, dict):
        return None
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        return None
    status_name = ""
    category = ""
    status = fields.get("status")
    if isinstance(status, dict):
        status_name = str(status.get("name") or "")
        cat = status.get("statusCategory")
        if isinstance(cat, dict):
            category = str(cat.get("key") or "")
    assignee = ""
    who = fields.get("assignee")
    if isinstance(who, dict):
        assignee = str(who.get("displayName") or who.get("name") or "")
    return Issue(
        key=str(payload.get("key") or key),
        status=status_name,
        status_category=category,
        title=str(fields.get("summary") or ""),
        assignee=assignee,
    )


def _fetch(key: str, creds: Credentials) -> Issue | None:
    """One authenticated GET. None on any failure — this is best-effort."""
    # `fields` keeps the response to the four things we read; a Jira issue
    # document is otherwise tens of kilobytes of custom fields.
    url = f"{creds.base_url}/rest/api/3/issue/{quote(key, safe='')}?fields=summary,status,assignee"
    auth = base64.b64encode(f"{creds.email}:{creds.token}".encode()).decode()
    request = urllib.request.Request(  # noqa: S310 - scheme is the user's own configured site
        url,
        headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
    )
    if not url.lower().startswith("https://"):
        # Refuse to send a bearer credential in the clear, even to a host the
        # user named themselves.
        return None
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SEC) as response:  # noqa: S310
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    return _parse_issue(key, payload)


@dataclass
class _Entry:
    issue: Issue | None
    expires_at: float


class IssueResolver:
    """TTL cache over the Jira probe, keyed by ticket.

    :meth:`cached` is render-path safe (dict lookup, never blocks).
    :meth:`resolve` performs a network call and is only safe from a worker.
    """

    def __init__(self, ttl: float = CACHE_TTL_SEC) -> None:
        self._ttl = ttl
        self._cache: dict[str, _Entry] = {}

    def cached(self, key: str | None) -> Issue | None:
        if not key:
            return None
        entry = self._cache.get(key.upper())
        if entry is None or entry.expires_at <= time.monotonic():
            return None
        return entry.issue

    def needs_refresh(self, key: str | None) -> bool:
        """True when ``key`` has no live entry and a lookup is possible."""
        if not key or not is_configured():
            return False
        entry = self._cache.get(key.upper())
        return entry is None or entry.expires_at <= time.monotonic()

    def resolve(self, key: str | None) -> Issue | None:
        """Probe (or return a live entry) for ``key``. Blocking."""
        if not key:
            return None
        creds = credentials()
        if creds is None:
            return None
        cache_key = key.upper()
        entry = self._cache.get(cache_key)
        if entry is not None and entry.expires_at > time.monotonic():
            return entry.issue
        issue = _fetch(cache_key, creds)
        self._cache[cache_key] = _Entry(issue=issue, expires_at=time.monotonic() + self._ttl)
        return issue

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._cache.clear()
            return
        self._cache.pop(key.upper(), None)


def drift(issue: Issue | None, pr_state: str) -> str | None:
    """A one-line description of ticket/PR disagreement, or None.

    Only the two directions that actually cost something are reported. A
    ticket sitting in review while its PR is open is not drift, it is
    Tuesday.
    """
    if issue is None or not issue.status:
        return None
    state = (pr_state or "").upper()
    if state == "MERGED" and not issue.is_done:
        return f"PR merged, ticket still {issue.status}"
    if state == "OPEN" and issue.is_done:
        return f"ticket {issue.status}, PR still open"
    return None

"""``flows/snapshot.json``: where every ticket on the board was, last time we looked.

The board is drawn from a Jira search, and flows fire on *changes* to it. Both
need the same thing — the board's current state, and the state before it — so
this module owns that pair. A refresh is three steps, each a separate function
so the flows tick can run its rules in between them:

1. :func:`fetch` — one JQL search for the whole board
   (:meth:`ephor.board.BoardSettings.search_jql`). ``None`` on any failure.
2. :func:`advance` — a pure diff of that result against the previous snapshot:
   the new snapshot, and the list of status :class:`Transition` s.
3. :func:`save` — atomic rename, 0600, and always the *last* write of a tick.

Only the flows tick (:mod:`ephor.flows.tick`) composes them, under
``flows/tick.lock``, with the rules run in between. There is deliberately no
rule-less "refresh" here: saving a snapshot consumes the moves it records,
so a refresh that skipped the rules would silently swallow every move they
should have fired on. The dashboard and ``ephor flows board --refresh`` run
the tick; with no rules configured it is exactly that refresh anyway.

Rules this file keeps, because the flows built on it depend on them:

**Unknown never means unmet.** A failed search writes nothing. A snapshot
missing half the board would read, next time, as half the board having moved.

**Absence is not movement.** A ticket missing from one search is kept, with
``unseen_ticks`` bumped and ``unseen_since`` stamped, and produces no
transition — search results are eventually consistent, and a ticket can drop
out of a JQL for reasons that have nothing to do with its status (reassigned,
a filter edited). It is forgotten only once every search for
:data:`DROP_AFTER_UNSEEN_SEC` has missed it. A duration, not a count of
searches: ticks come at whatever cadence the dashboard, ``flows watch`` or a
timer happens to run, and "an hour" should mean an hour under all of them.

**``seq`` counts entries, not time.** Every time a ticket's status differs
from the previous snapshot, its ``seq`` goes up by one. ``(status_id, seq)``
therefore names one *visit* to a status: a ticket that leaves Peer Review
and comes back is a new visit with a new name, while the same visit seen on a
hundred ticks keeps one. That pair is what a flow's run is keyed on.
A ticket forgotten after :data:`DROP_AFTER_UNSEEN_SEC` leaves a small tombstone
(``dropped``: its last seq and status). If it comes back in that same status
it never moved, and it resumes its old visit — no transition, nothing fires
again. If it comes back anywhere else it continues from its old seq: the run
of its earlier visit keeps its file for a month, and a returning ticket
restarting at seq 0 would collide with it and never fire.

**The board's scope is part of what was seen.** ``scope`` fingerprints the
search itself (projects plus JQL). When it changes, the tickets the new
search brings in were not *moved* there — the window onto the board was —
and the flow tick adopts them instead of firing on them.

**When a ticket entered its status** (``entered_at``) is the tick's own
clock on the tick that saw the move. A ticket seen for the first time has no
such tick, so it borrows Jira's ``statuscategorychangedate`` and is marked
``entered_estimated``: that field moves only when the status *category*
changes, so it is the earliest the current status can have begun, not when
it did. The lanes use it to age a card; nothing fires on it.

The snapshot also carries fields only the flow tick reads (``rules_seen``,
``myself``, the Flagged field's id, and the two observations :mod:`ephor.flows.triggers` compares
from tick to tick: ``prs`` with ``pr_checked_at``, and ``sessions_seen``).
:func:`advance` carries them forward untouched — in particular it never
marks a rule as seen, so a rule the tick has not evaluated yet still starts
in adopt mode.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ephor import eventlog, jira_api
from ephor.board import BoardSettings
from ephor.flows import flows_dir, runs
from ephor.flows.runs import as_int, as_str, iso, parse_iso
from ephor.jira_api import Issue

SNAPSHOT_FILE = "snapshot.json"
TICK_LOCK_FILE = "tick.lock"
SNAPSHOT_SCHEMA_VERSION = 1

# How long every search must miss a ticket before it is forgotten: long
# enough to ride out a flaky JQL or a ticket briefly reassigned, short enough
# that the file does not accumulate every ticket you have ever owned.
DROP_AFTER_UNSEEN_SEC = 3600.0
# How long a dropped ticket's tombstone is kept. A returning ticket only
# needs its old seq while the run files of its earlier visit still exist
# (runs.PRUNE_AFTER_DAYS after they finish), plus some slack.
TOMBSTONE_KEEP_DAYS = runs.PRUNE_AFTER_DAYS + 7.0

# What the board search asks Jira for. Everything a lane or a flow rule
# reads, and nothing else — an issue document is otherwise tens of kilobytes.
# The site's "Flagged" custom field is appended when its id is known (see
# :func:`search_fields`); its id differs from one Jira site to the next.
SEARCH_FIELDS: tuple[str, ...] = ("summary", "status", "assignee", "statuscategorychangedate")


def search_fields(flagged_field: str = "") -> tuple[str, ...]:
    """:data:`SEARCH_FIELDS`, plus the Flagged field when its id is known."""
    return (*SEARCH_FIELDS, flagged_field) if flagged_field else SEARCH_FIELDS


def snapshot_path() -> Path:
    return flows_dir() / SNAPSHOT_FILE


def tick_lock_path() -> Path:
    """The non-blocking lock every snapshot writer holds (see docs/architecture.md)."""
    return flows_dir() / TICK_LOCK_FILE


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class TicketState:
    """One ticket as the last search saw it."""

    status: str = ""
    status_id: str = ""
    status_category: str = ""
    assignee_id: str = ""
    assignee: str = ""
    title: str = ""
    seq: int = 0
    unseen_ticks: int = 0
    # When the first of the searches that have since missed it ran ("" while
    # on the board). It is what DROP_AFTER_UNSEEN_SEC is measured from.
    unseen_since: str = ""
    # When the ticket entered its current status (see the module docstring),
    # and whether that is Jira's category-change date standing in for it.
    entered_at: str = ""
    entered_estimated: bool = False
    # Jira's "Flagged" (impediment) is set on the issue.
    flagged: bool = False
    # The status it was in before the move that brought it to this one ("" if
    # it appeared on the board in it) — what on_enter's ``from =`` compares,
    # and what ``ephor flows explain`` names for an on_exit rule.
    from_status: str = ""
    from_status_id: str = ""

    @property
    def status_identity(self) -> str:
        """What "the status changed" is compared on: the id, else the name.

        Ids survive a workflow renaming its columns; the name is the fallback
        for a site that does not return one.
        """
        return self.status_id or self.status.strip().lower()

    @property
    def on_board(self) -> bool:
        """True when the latest search returned this ticket."""
        return self.unseen_ticks == 0

    @property
    def is_done(self) -> bool:
        return Issue(key="", status=self.status, status_category=self.status_category).is_done

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "status_id": self.status_id,
            "status_category": self.status_category,
            "assignee_id": self.assignee_id,
            "assignee": self.assignee,
            "title": self.title,
            "seq": self.seq,
            "unseen_ticks": self.unseen_ticks,
            "unseen_since": self.unseen_since,
            "entered_at": self.entered_at,
            "entered_estimated": self.entered_estimated,
            "flagged": self.flagged,
            "from_status": self.from_status,
            "from_status_id": self.from_status_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TicketState:
        return cls(
            status=as_str(data.get("status")),
            status_id=as_str(data.get("status_id")),
            status_category=as_str(data.get("status_category")),
            assignee_id=as_str(data.get("assignee_id")),
            assignee=as_str(data.get("assignee")),
            title=as_str(data.get("title")),
            seq=as_int(data.get("seq")),
            unseen_ticks=as_int(data.get("unseen_ticks")),
            unseen_since=as_str(data.get("unseen_since")),
            entered_at=as_str(data.get("entered_at")),
            entered_estimated=data.get("entered_estimated") is True,
            flagged=data.get("flagged") is True,
            from_status=as_str(data.get("from_status")),
            from_status_id=as_str(data.get("from_status_id")),
        )

    @classmethod
    def from_issue(cls, issue: Issue, *, seq: int) -> TicketState:
        return cls(
            status=issue.status,
            status_id=issue.status_id,
            status_category=issue.status_category,
            assignee_id=issue.assignee_id,
            assignee=issue.assignee,
            title=issue.title,
            seq=seq,
            unseen_ticks=0,
            flagged=issue.flagged,
        )


CI_VALUES = frozenset({"pass", "fail", "pending"})


@dataclass(frozen=True)
class PrState:
    """One of a ticket's pull requests as the last ``gh`` lookup saw it (flow tick only)."""

    number: int
    state: str = ""  # OPEN | MERGED | CLOSED, as gh reports it
    head_sha: str = ""
    url: str = ""
    ci: str = ""  # pass | fail | pending | "" (unknown, or no checks)

    @property
    def is_open(self) -> bool:
        return self.state.upper() == "OPEN"

    @property
    def is_merged(self) -> bool:
        return self.state.upper() == "MERGED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "state": self.state,
            "head_sha": self.head_sha,
            "url": self.url,
            "ci": self.ci,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PrState | None:
        number = data.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            return None
        return cls(
            number=number,
            state=as_str(data.get("state")),
            head_sha=as_str(data.get("head_sha")),
            url=as_str(data.get("url")),
            ci=as_str(data.get("ci")) if data.get("ci") in CI_VALUES else "",
        )


def _prs_from(value: object) -> tuple[PrState, ...] | None:
    """A ticket's stored PR list, or None when it is not the shape we write.

    A lone object (an older shape) is a list of one. An empty list is a real
    observation — "no PR yet" — and is kept. A list with *any* malformed
    entry is dropped whole: a baseline missing one PR would read, next time,
    as that PR having just been opened, and no baseline at all only costs a
    re-baseline.
    """
    items = [value] if isinstance(value, dict) else value
    if not isinstance(items, list):
        return None
    found: list[PrState] = []
    for item in items:
        pr = PrState.from_dict(item) if isinstance(item, dict) else None
        if pr is None:
            return None
        found.append(pr)
    return tuple(found)


@dataclass(frozen=True)
class SessionSeen:
    """A ticket-bound agent session as the last tick saw it (flow tick only).

    ``settled`` is "IDLE, with no event for at least ``[flows] settle``".
    ``pid`` is kept so a session whose state file has since been deleted can
    still be told apart: a dead process was a death, a live one was not.
    ``idle_since`` is the session's last hook event while settled — the
    start of this idle spell, and so what names it: an ``on_session =
    "idle"`` run fires once per spell, not once per session.
    """

    ticket: str
    status: str
    pid: int = 0
    settled: bool = False
    idle_since: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "status": self.status,
            "pid": self.pid,
            "settled": self.settled,
            "idle_since": self.idle_since,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionSeen | None:
        ticket = as_str(data.get("ticket"))
        if not ticket:
            return None
        return cls(
            ticket=ticket,
            status=as_str(data.get("status")),
            pid=as_int(data.get("pid")),
            settled=data.get("settled") is True,
            idle_since=as_str(data.get("idle_since")),
        )


@dataclass(frozen=True)
class DroppedTicket:
    """A ticket forgotten after DROP_AFTER_UNSEEN: the seq and status it had, and when.

    The status is what tells a return apart from a move: a ticket that comes
    back in the status it left in never moved (the default JQL drops a Done
    ticket after a week, and any later edit brings it back), so it resumes
    its old visit. Tombstones written before the status was kept have none,
    and a return to one of those still counts as a new visit.
    """

    seq: int
    dropped_at: str
    status: str = ""
    status_id: str = ""

    @property
    def status_identity(self) -> str:
        return self.status_id or self.status.strip().lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "dropped_at": self.dropped_at,
            "status": self.status,
            "status_id": self.status_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DroppedTicket:
        return cls(
            seq=as_int(data.get("seq")),
            dropped_at=as_str(data.get("dropped_at")),
            status=as_str(data.get("status")),
            status_id=as_str(data.get("status_id")),
        )


@dataclass(frozen=True)
class Transition:
    """A ticket whose status differs from the previous snapshot."""

    key: str
    from_status: str  # "" when the ticket was not in the previous snapshot
    to_status: str
    from_status_id: str
    to_status_id: str
    seq: int  # the ticket's seq *after* this transition
    # True when the ticket was not in the previous snapshot: new to the
    # board rather than moved on it. Structural, not inferred from an empty
    # from_status — a status Jira once returned blank must not read as "new".
    appeared: bool = False

    @property
    def detail(self) -> str:
        if self.appeared:
            return f"→ {self.to_status} (new on board)"
        return f"{self.from_status} → {self.to_status}"


@dataclass(frozen=True)
class Snapshot:
    """``flows/snapshot.json``, decoded."""

    taken_at: str
    tickets: Mapping[str, TicketState] = field(default_factory=dict)
    rules_seen: tuple[str, ...] = ()
    myself: str = ""  # the configured user's Jira accountId, cached by the tick
    # jira_api.identity() of the credentials ``myself`` was fetched with. A
    # different site or email is a different "me", and is asked again.
    myself_for: str = ""
    # When on_pr rules last asked gh about the board's tickets, and what it
    # said: ticket -> every PR that names it. A ticket with an entry (even
    # an empty one) has a baseline; only a change from it fires on_pr.
    pr_checked_at: str = ""
    prs: Mapping[str, tuple[PrState, ...]] = field(default_factory=dict)
    # ticket -> the repo its ``prs`` entry was read from. A different repo
    # (a worktree appeared or went away) is a different question, and its
    # answer is a fresh baseline rather than a burst of opened/merged.
    pr_repos: Mapping[str, str] = field(default_factory=dict)
    # The site's "Flagged" custom field id ("" when it has none), and the
    # jira_api.identity() it was looked up for, like ``myself_for``.
    flagged_field: str = ""
    flagged_for: str = ""
    # session id -> what the last tick saw of it (on_session rules only).
    sessions_seen: Mapping[str, SessionSeen] = field(default_factory=dict)
    # Tickets forgotten after DROP_AFTER_UNSEEN, so a return gets a new seq.
    dropped: Mapping[str, DroppedTicket] = field(default_factory=dict)
    # board_scope() of the search that produced ``tickets`` ("" before P2).
    scope: str = ""
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    @property
    def taken(self) -> datetime | None:
        return parse_iso(self.taken_at)

    def age_sec(self, now: datetime | None = None) -> float | None:
        """Seconds since this snapshot was taken, or None when undated."""
        taken = self.taken
        if taken is None:
            return None
        return max(0.0, ((now or _now()) - taken).total_seconds())

    def on_board(self) -> dict[str, TicketState]:
        """Tickets the latest search returned — the ones a lane should draw."""
        return {key: t for key, t in self.tickets.items() if t.on_board}

    def prs_for(self, key: str) -> tuple[PrState, ...]:
        """``key``'s pull requests as last observed (empty when none, or unknown)."""
        return tuple(self.prs.get((key or "").strip().upper(), ()))

    def to_json(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "taken_at": self.taken_at,
            "rules_seen": list(self.rules_seen),
            "myself": self.myself,
            "myself_for": self.myself_for,
            "tickets": {key: t.to_dict() for key, t in sorted(self.tickets.items())},
            "pr_checked_at": self.pr_checked_at,
            "prs": {key: [pr.to_dict() for pr in found] for key, found in sorted(self.prs.items())},
            "pr_repos": dict(sorted(self.pr_repos.items())),
            "flagged_field": self.flagged_field,
            "flagged_for": self.flagged_for,
            "sessions_seen": {
                sid: seen.to_dict() for sid, seen in sorted(self.sessions_seen.items())
            },
            "dropped": {key: d.to_dict() for key, d in sorted(self.dropped.items())},
            "scope": self.scope,
        }
        return json.dumps(payload, sort_keys=True, indent=1)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Snapshot:
        """Decode, skipping any entry that is not the shape we write."""
        raw_tickets = data.get("tickets")
        tickets: dict[str, TicketState] = {}
        if isinstance(raw_tickets, dict):
            for key, value in raw_tickets.items():
                if isinstance(key, str) and key and isinstance(value, dict):
                    tickets[key] = TicketState.from_dict(value)
        raw_prs = data.get("prs")
        prs: dict[str, tuple[PrState, ...]] = {}
        if isinstance(raw_prs, dict):
            for key, value in raw_prs.items():
                found = _prs_from(value)
                if isinstance(key, str) and key and found is not None:
                    prs[key] = found
        raw_repos = data.get("pr_repos")
        pr_repos: dict[str, str] = {}
        if isinstance(raw_repos, dict):
            for key, value in raw_repos.items():
                if isinstance(key, str) and key and isinstance(value, str):
                    pr_repos[key] = value
        raw_sessions = data.get("sessions_seen")
        sessions: dict[str, SessionSeen] = {}
        if isinstance(raw_sessions, dict):
            for sid, value in raw_sessions.items():
                seen = SessionSeen.from_dict(value) if isinstance(value, dict) else None
                if isinstance(sid, str) and sid and seen is not None:
                    sessions[sid] = seen
        raw_dropped = data.get("dropped")
        dropped: dict[str, DroppedTicket] = {}
        if isinstance(raw_dropped, dict):
            for key, value in raw_dropped.items():
                if isinstance(key, str) and key and isinstance(value, dict):
                    dropped[key] = DroppedTicket.from_dict(value)
        rules = data.get("rules_seen")
        return cls(
            taken_at=as_str(data.get("taken_at")),
            tickets=tickets,
            rules_seen=tuple(r for r in rules if isinstance(r, str))
            if isinstance(rules, list)
            else (),
            myself=as_str(data.get("myself")),
            myself_for=as_str(data.get("myself_for")),
            pr_checked_at=as_str(data.get("pr_checked_at")),
            prs=prs,
            pr_repos=pr_repos,
            flagged_field=as_str(data.get("flagged_field")),
            flagged_for=as_str(data.get("flagged_for")),
            sessions_seen=sessions,
            dropped=dropped,
            scope=as_str(data.get("scope")),
            schema_version=as_int(data.get("schema_version")) or SNAPSHOT_SCHEMA_VERSION,
        )


# --- persistence ------------------------------------------------------------


def load(path: Path | None = None) -> Snapshot | None:
    """The stored snapshot, or None when there is none or it cannot be read.

    An unreadable snapshot is treated exactly like a missing one. For the
    lanes that means "no tickets yet"; for the flow tick it means adopt mode
    — record the board and fire nothing — which is the safe reading of "we
    do not know what the board looked like before".
    """
    target = path or snapshot_path()
    try:
        data = json.loads(target.read_text())
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    return Snapshot.from_dict(data)


def save(snapshot: Snapshot, path: Path | None = None) -> bool:
    """Write ``snapshot`` by atomic rename, 0600. False when the disk says no."""
    target = path or snapshot_path()
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp.snapshot.")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(snapshot.to_json())
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            return False
    except OSError:
        return False
    return True


# --- the three steps --------------------------------------------------------


Search = Callable[..., list[Issue] | None]


def board_scope(board: BoardSettings) -> str:
    """A short fingerprint of what the board search asks for (projects plus JQL)."""
    return hashlib.sha256(board.search_jql().encode()).hexdigest()[:12]


def fetch(
    board: BoardSettings, *, search: Search | None = None, flagged_field: str = ""
) -> list[Issue] | None:
    """Every ticket on the board, or None when the search failed. Blocking."""
    if not board.projects:
        return None
    run = search or jira_api.search
    return run(board.search_jql(), search_fields(flagged_field))


def advance(
    previous: Snapshot | None,
    issues: Iterable[Issue],
    *,
    now: datetime | None = None,
    seq_floor: Mapping[str, int] | None = None,
) -> tuple[Snapshot, list[Transition]]:
    """The snapshot after ``issues``, and how it differs from ``previous``.

    Pure: reads nothing, writes nothing. With no previous snapshot there is
    nothing to compare against, so no transitions are reported — the first
    snapshot is a baseline, not a board-wide burst of moves. ``seq_floor``
    (ticket -> lowest seq to use) seeds that baseline, so a snapshot rebuilt
    after snapshot.json was lost does not reuse the seqs of runs still on
    disk; it is ignored when there is a previous snapshot.
    """
    moment = now or _now()
    stamp = iso(moment)
    before: Mapping[str, TicketState] = previous.tickets if previous is not None else {}
    dropped: dict[str, DroppedTicket] = dict(previous.dropped) if previous is not None else {}
    floor: Mapping[str, int] = (seq_floor or {}) if previous is None else {}
    tickets: dict[str, TicketState] = {}
    transitions: list[Transition] = []
    seen: set[str] = set()

    # A ticket seen for the first time (or carried over from a snapshot
    # written before entered_at existed) borrows Jira's category-change date.
    def estimated(issue: Issue) -> dict[str, Any]:
        return {"entered_at": issue.status_changed_at, "entered_estimated": True}

    for issue in issues:
        key = issue.key.strip().upper()
        if not key or key in seen:
            continue
        fresh = TicketState.from_issue(issue, seq=0)
        if not fresh.status_identity:
            # A ticket returned with no status at all. Unknown never means
            # unmet: treat it exactly like a ticket the search missed — the
            # old state is kept with unseen_ticks bumped, and a ticket new
            # to the board waits until it has a status to be placed by.
            continue
        seen.add(key)
        old = before.get(key)
        if old is None:
            # A ticket that was here before and was forgotten continues its
            # count: its earlier visit's run may still be on disk.
            tomb = dropped.pop(key, None)
            if (
                tomb is not None
                and tomb.status_identity
                and tomb.status_identity == fresh.status_identity
            ):
                # Back in the status it was dropped in: the same visit, so no
                # transition, and nothing fires a second time for it.
                tickets[key] = replace(fresh, seq=tomb.seq, **estimated(issue))
                continue
            seq = tomb.seq + 1 if tomb is not None else floor.get(key, 0)
            tickets[key] = replace(fresh, seq=seq, **estimated(issue))
            if previous is not None:
                transitions.append(
                    Transition(
                        key=key,
                        from_status="",
                        to_status=fresh.status,
                        from_status_id="",
                        to_status_id=fresh.status_id,
                        seq=seq,
                        appeared=True,
                    )
                )
            continue
        if fresh.status_identity != old.status_identity:
            seq = old.seq + 1
            tickets[key] = replace(
                fresh,
                seq=seq,
                entered_at=stamp,
                entered_estimated=False,
                from_status=old.status,
                from_status_id=old.status_id,
            )
            transitions.append(
                Transition(
                    key=key,
                    from_status=old.status,
                    to_status=fresh.status,
                    from_status_id=old.status_id,
                    to_status_id=fresh.status_id,
                    seq=seq,
                )
            )
        else:
            entered = (
                {"entered_at": old.entered_at, "entered_estimated": old.entered_estimated}
                if old.entered_at
                else estimated(issue)
            )
            tickets[key] = replace(
                fresh,
                seq=old.seq,
                from_status=old.from_status,
                from_status_id=old.from_status_id,
                **entered,
            )

    for key, old in before.items():
        if key in seen:
            continue
        # The clock starts at the first search that missed it. A snapshot
        # written before unseen_since existed starts it now: late, never early.
        since = old.unseen_since or stamp
        missing = parse_iso(since)
        gone_for = (moment - missing).total_seconds() if missing is not None else 0.0
        if gone_for <= DROP_AFTER_UNSEEN_SEC:
            tickets[key] = replace(old, unseen_ticks=old.unseen_ticks + 1, unseen_since=since)
        else:
            dropped[key] = DroppedTicket(
                seq=old.seq, dropped_at=stamp, status=old.status, status_id=old.status_id
            )

    keep = TOMBSTONE_KEEP_DAYS * 86400.0
    for key, tomb in list(dropped.items()):
        when = parse_iso(tomb.dropped_at)
        if when is None or (moment - when).total_seconds() > keep:
            del dropped[key]

    base = previous or Snapshot(taken_at="")
    snapshot = replace(base, taken_at=stamp, tickets=tickets, dropped=dropped)
    return snapshot, transitions


def log_transitions(transitions: Iterable[Transition]) -> None:
    """One ``TICKET_TRANSITIONED`` event per move, for ``ephor log <KEY>``."""
    for move in transitions:
        eventlog.append(
            eventlog.EventKind.TICKET_TRANSITIONED,
            ticket=move.key,
            detail=move.detail,
            from_status=move.from_status,
            to_status=move.to_status,
            status_id=move.to_status_id,
            seq=move.seq,
        )


# --- the outcome of a tick, as the status lanes read it -----------------------


class RefreshStatus(StrEnum):
    OK = "ok"
    LOCKED = "locked"  # another tick holds tick.lock; it will write the snapshot
    NO_CONFIG = "no_config"  # no board.toml
    BOARD_INVALID = "board_invalid"  # [board] has errors; the search would be wrong
    JIRA_OFF = "jira_off"  # no Jira credentials
    SEARCH_FAILED = "search_failed"  # Jira did not answer; nothing was written
    WRITE_FAILED = "write_failed"


@dataclass(frozen=True)
class RefreshResult:
    status: RefreshStatus
    snapshot: Snapshot | None = None
    transitions: tuple[Transition, ...] = ()
    # What went wrong, in more words than the status ("HTTP 401: …", "rate
    # limited until 12:04"). Replaces the stock message when it is set.
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is RefreshStatus.OK

    @property
    def message(self) -> str:
        if self.detail and self.status is not RefreshStatus.OK:
            return self.detail
        if self.status is RefreshStatus.OK:
            count = len(self.snapshot.on_board()) if self.snapshot else 0
            moved = len(self.transitions)
            return f"{count} ticket(s) on the board, {moved} moved"
        if self.status is RefreshStatus.JIRA_OFF:
            missing = jira_api.missing_credentials()
            return f"Jira is not configured — missing {missing}" if missing else "Jira is off"
        return {
            RefreshStatus.LOCKED: "another tick is running — it will update the snapshot",
            RefreshStatus.NO_CONFIG: "no board.toml",
            RefreshStatus.BOARD_INVALID: "board.toml [board] has errors",
            RefreshStatus.SEARCH_FAILED: "Jira search failed — snapshot left as it was",
            RefreshStatus.WRITE_FAILED: "could not write the snapshot",
        }[self.status]


def baseline_seq_floor(previous: Snapshot | None) -> dict[str, int]:
    """For a snapshot built from nothing: each ticket's next seq, from its run files."""
    if previous is not None:
        return {}
    return runs.next_seq_by_ticket()

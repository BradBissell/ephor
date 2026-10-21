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

:func:`refresh` composes them under ``flows/tick.lock``. It is exactly a
tick with no rules — and so it must not be used where rules exist: the moves
it records would never be offered to them. The dashboard and ``ephor flows
board --refresh`` run the real tick (:mod:`ephor.flows.tick`) instead.

Rules this file keeps, because the flows built on it depend on them:

**Unknown never means unmet.** A failed search writes nothing. A snapshot
missing half the board would read, next time, as half the board having moved.

**Absence is not movement.** A ticket missing from one search is kept, with
``unseen_ticks`` bumped, and produces no transition — search results are
eventually consistent, and a ticket can drop out of a JQL for reasons that
have nothing to do with its status (reassigned, a filter edited). It is
forgotten only after :data:`DROP_AFTER_UNSEEN` searches in a row miss it.

**``seq`` counts entries, not time.** Every time a ticket's status differs
from the previous snapshot, its ``seq`` goes up by one. ``(status_id, seq)``
therefore names one *visit* to a status: a ticket that leaves Peer Review
and comes back is a new visit with a new name, while the same visit seen on a
hundred ticks keeps one. That pair is what a flow's run is keyed on.
A ticket forgotten after :data:`DROP_AFTER_UNSEEN` leaves a small tombstone
(``dropped``: its last seq and status). If it comes back in that same status
it never moved, and it resumes its old visit — no transition, nothing fires
again. If it comes back anywhere else it continues from its old seq: the run
of its earlier visit keeps its file for a month, and a returning ticket
restarting at seq 0 would collide with it and never fire.

**The board's scope is part of what was seen.** ``scope`` fingerprints the
search itself (projects plus JQL). When it changes, the tickets the new
search brings in were not *moved* there — the window onto the board was —
and the flow tick adopts them instead of firing on them.

The snapshot also carries fields only the flow tick reads (``rules_seen``,
``myself``, and the two observations :mod:`ephor.flows.triggers` compares
from tick to tick: ``prs`` with ``pr_checked_at``, and ``sessions_seen``).
A rule-less refresh carries them forward untouched — in particular it never
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
from ephor.board import BoardConfig, BoardSettings
from ephor.filelock import locked
from ephor.flows import flows_dir, runs
from ephor.jira_api import Issue

SNAPSHOT_FILE = "snapshot.json"
TICK_LOCK_FILE = "tick.lock"
SNAPSHOT_SCHEMA_VERSION = 1

# How many searches in a row may miss a ticket before it is forgotten. At the
# TUI's one-minute cadence that is an hour: long enough to ride out a flaky
# JQL or a ticket briefly reassigned, short enough that the file does not
# accumulate every ticket you have ever owned.
DROP_AFTER_UNSEEN = 60
# How long a dropped ticket's tombstone is kept. A returning ticket only
# needs its old seq while the run files of its earlier visit still exist
# (runs.PRUNE_AFTER_DAYS after they finish), plus some slack.
TOMBSTONE_KEEP_DAYS = runs.PRUNE_AFTER_DAYS + 7.0

# What the board search asks Jira for. Everything a lane or a flow rule
# reads, and nothing else — an issue document is otherwise tens of kilobytes.
SEARCH_FIELDS: tuple[str, ...] = ("summary", "status", "assignee")


def snapshot_path() -> Path:
    return flows_dir() / SNAPSHOT_FILE


def tick_lock_path() -> Path:
    """The non-blocking lock every snapshot writer holds (see docs/architecture.md)."""
    return flows_dir() / TICK_LOCK_FILE


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _parse_iso(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TicketState:
        return cls(
            status=_str(data.get("status")),
            status_id=_str(data.get("status_id")),
            status_category=_str(data.get("status_category")),
            assignee_id=_str(data.get("assignee_id")),
            assignee=_str(data.get("assignee")),
            title=_str(data.get("title")),
            seq=_int(data.get("seq")),
            unseen_ticks=_int(data.get("unseen_ticks")),
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
        )


@dataclass(frozen=True)
class PrState:
    """One of a ticket's pull requests as the last ``gh`` lookup saw it (flow tick only)."""

    number: int
    state: str = ""  # OPEN | MERGED | CLOSED, as gh reports it
    head_sha: str = ""
    url: str = ""

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
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PrState | None:
        number = data.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            return None
        return cls(
            number=number,
            state=_str(data.get("state")),
            head_sha=_str(data.get("head_sha")),
            url=_str(data.get("url")),
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
    """

    ticket: str
    status: str
    pid: int = 0
    settled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "status": self.status,
            "pid": self.pid,
            "settled": self.settled,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionSeen | None:
        ticket = _str(data.get("ticket"))
        if not ticket:
            return None
        return cls(
            ticket=ticket,
            status=_str(data.get("status")),
            pid=_int(data.get("pid")),
            settled=data.get("settled") is True,
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
            seq=_int(data.get("seq")),
            dropped_at=_str(data.get("dropped_at")),
            status=_str(data.get("status")),
            status_id=_str(data.get("status_id")),
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
    # When on_pr rules last asked gh about the board's tickets, and what it
    # said: ticket -> every PR that names it. A ticket with an entry (even
    # an empty one) has a baseline; only a change from it fires on_pr.
    pr_checked_at: str = ""
    prs: Mapping[str, tuple[PrState, ...]] = field(default_factory=dict)
    # session id -> what the last tick saw of it (on_session rules only).
    sessions_seen: Mapping[str, SessionSeen] = field(default_factory=dict)
    # Tickets forgotten after DROP_AFTER_UNSEEN, so a return gets a new seq.
    dropped: Mapping[str, DroppedTicket] = field(default_factory=dict)
    # board_scope() of the search that produced ``tickets`` ("" before P2).
    scope: str = ""
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    @property
    def taken(self) -> datetime | None:
        return _parse_iso(self.taken_at)

    def age_sec(self, now: datetime | None = None) -> float | None:
        """Seconds since this snapshot was taken, or None when undated."""
        taken = self.taken
        if taken is None:
            return None
        return max(0.0, ((now or _now()) - taken).total_seconds())

    def on_board(self) -> dict[str, TicketState]:
        """Tickets the latest search returned — the ones a lane should draw."""
        return {key: t for key, t in self.tickets.items() if t.on_board}

    def to_json(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "taken_at": self.taken_at,
            "rules_seen": list(self.rules_seen),
            "myself": self.myself,
            "tickets": {key: t.to_dict() for key, t in sorted(self.tickets.items())},
            "pr_checked_at": self.pr_checked_at,
            "prs": {key: [pr.to_dict() for pr in found] for key, found in sorted(self.prs.items())},
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
            taken_at=_str(data.get("taken_at")),
            tickets=tickets,
            rules_seen=tuple(r for r in rules if isinstance(r, str))
            if isinstance(rules, list)
            else (),
            myself=_str(data.get("myself")),
            pr_checked_at=_str(data.get("pr_checked_at")),
            prs=prs,
            sessions_seen=sessions,
            dropped=dropped,
            scope=_str(data.get("scope")),
            schema_version=_int(data.get("schema_version")) or SNAPSHOT_SCHEMA_VERSION,
        )


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


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


def fetch(board: BoardSettings, *, search: Search | None = None) -> list[Issue] | None:
    """Every ticket on the board, or None when the search failed. Blocking."""
    if not board.projects:
        return None
    run = search or jira_api.search
    return run(board.search_jql(), SEARCH_FIELDS)


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
    stamp = _iso(moment)
    before: Mapping[str, TicketState] = previous.tickets if previous is not None else {}
    dropped: dict[str, DroppedTicket] = dict(previous.dropped) if previous is not None else {}
    floor: Mapping[str, int] = (seq_floor or {}) if previous is None else {}
    tickets: dict[str, TicketState] = {}
    transitions: list[Transition] = []
    seen: set[str] = set()

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
                tickets[key] = replace(fresh, seq=tomb.seq)
                continue
            seq = tomb.seq + 1 if tomb is not None else floor.get(key, 0)
            tickets[key] = replace(fresh, seq=seq)
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
            tickets[key] = replace(fresh, seq=seq)
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
            tickets[key] = replace(fresh, seq=old.seq)

    for key, old in before.items():
        if key in seen:
            continue
        unseen = old.unseen_ticks + 1
        if unseen <= DROP_AFTER_UNSEEN:
            tickets[key] = replace(old, unseen_ticks=unseen)
        else:
            dropped[key] = DroppedTicket(
                seq=old.seq, dropped_at=stamp, status=old.status, status_id=old.status_id
            )

    keep = TOMBSTONE_KEEP_DAYS * 86400.0
    for key, tomb in list(dropped.items()):
        when = _parse_iso(tomb.dropped_at)
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


# --- the rule-less tick ------------------------------------------------------


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

    @property
    def ok(self) -> bool:
        return self.status is RefreshStatus.OK

    @property
    def message(self) -> str:
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


def refresh(
    config: BoardConfig | None,
    *,
    dry_run: bool = False,
    search: Search | None = None,
    now: datetime | None = None,
) -> RefreshResult:
    """Search, diff and persist the board, under ``tick.lock``. Blocking.

    The lock is taken without waiting: a second refresh (another TUI, the
    tick from ``ephor flows watch``) that finds it held returns ``LOCKED``
    at once, because the holder is about to write the same answer. A
    dry run takes no lock and writes nothing — not the snapshot, not an event.
    """
    problem = _precondition(config)
    if problem is not None or config is None:
        return RefreshResult(problem or RefreshStatus.NO_CONFIG)
    if dry_run:
        return refresh_locked(config, dry_run=True, search=search, now=now)
    with locked(tick_lock_path(), timeout=0) as acquired:
        if not acquired:
            return RefreshResult(RefreshStatus.LOCKED, snapshot=load())
        return refresh_locked(config, dry_run=False, search=search, now=now)


def refresh_locked(
    config: BoardConfig,
    *,
    dry_run: bool = False,
    search: Search | None = None,
    now: datetime | None = None,
) -> RefreshResult:
    """:func:`refresh` for a caller already holding ``tick.lock``."""
    problem = _precondition(config)
    if problem is not None:
        return RefreshResult(problem)
    previous = load()
    issues = fetch(config.board, search=search)
    if issues is None:
        return RefreshResult(RefreshStatus.SEARCH_FAILED, snapshot=previous)
    snapshot, transitions = advance(
        previous, issues, now=now, seq_floor=baseline_seq_floor(previous)
    )
    snapshot = replace(snapshot, scope=board_scope(config.board))
    if dry_run:
        return RefreshResult(RefreshStatus.OK, snapshot=snapshot, transitions=tuple(transitions))
    log_transitions(transitions)
    # Written last: everything above can be repeated harmlessly by the next
    # tick if this one dies before here.
    if not save(snapshot):
        return RefreshResult(RefreshStatus.WRITE_FAILED, snapshot=previous)
    return RefreshResult(RefreshStatus.OK, snapshot=snapshot, transitions=tuple(transitions))


def baseline_seq_floor(previous: Snapshot | None) -> dict[str, int]:
    """For a snapshot built from nothing: each ticket's next seq, from its run files."""
    if previous is not None:
        return {}
    return runs.next_seq_by_ticket()


def _precondition(config: BoardConfig | None) -> RefreshStatus | None:
    if config is None:
        return RefreshStatus.NO_CONFIG
    if not config.lanes_ok:
        return RefreshStatus.BOARD_INVALID
    if not jira_api.is_configured():
        return RefreshStatus.JIRA_OFF
    return None

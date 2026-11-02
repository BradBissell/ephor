"""``ephor flows tick``: look at the board once, and act on what moved.

A tick is a board refresh (:mod:`ephor.flows.snapshot`: fetch, advance, save) with
the flow rules run in the middle of it::

    take tick.lock → search Jira → diff → create runs → tend old runs
                   → dispatch → write the snapshot (last)

Everything before the snapshot write can be repeated harmlessly. A run's
file name is its dedupe (:mod:`ephor.flows.runs`), so a tick that dies after
creating runs and before saving the snapshot just tries to create the same
runs again next time, and fails to.

**Unknown never means unmet.** A failed search, a ``/myself`` that did not
answer, a pre-launch re-read that timed out, a tmux server that would not
list its panes: each of these leaves things exactly as they were, and the
next tick asks again. None of them is ever read as "the ticket moved" or "the
run finished".

**Adopt before acting.** With no previous snapshot, nothing is known to have
*moved* — tickets are just where they are — so nothing fires. The same goes
for a rule the snapshot has not seen before (its fingerprint is not in
``rules_seen``): it starts by adopting the board as it stands, rather than
firing on every ticket already sitting in its status. After a gap longer
than ``max_event_age`` (the laptop was asleep), moves are still noticed, but
every run they create waits for a human (``stale``), whatever the rule's mode.
And when the board's *scope* changes (a project added to ``[board]``, the JQL
edited), the tickets the new search brings in did not move — the window onto
the board did — so on that tick no rule fires on a ticket merely appearing.

**Guards, in order,** before anything is launched: the pause flag; for a
``command =`` flow its own hourly cap (``max_commands_per_hour``), and
nothing else; for a flow that starts an agent, the agent hourly cap, the
per-ticket breaker, the agent queue and the launch guard (an agent is
already working on this ticket — one idle past ``settle`` has finished with
it — or one was launched there in the last ten minutes, or its work record
names a live session, or this same tick already launched one there). Only a
launch that opened a window counts toward the caps. Then the ticket is re-read
from Jira, uncached: a search is eventually consistent, and an agent must
not start on a status the ticket has already left (a re-read with no status
is unknown, and blocks). A guard that says "not now" parks the run as
``blocked``; it is tried again on every tick for as long as the event that
fired it is still the ticket's latest (a failed launch after an exponential
backoff, :func:`retry_delay`) — but not for ever: a run held longer
than ``confirm_ttl`` (from its approval, or from its creation for an auto
run) expires, so a yes given days ago, or an event that arrived during a
long pause, does not act today.

**The agent queue.** ``max_active_agents`` is a number of agent slots, and a
slot is a session still at work: one idle past ``settle`` frees its slot
without anybody closing its window. A run that finds every slot taken is
*queued* (blocked with :data:`runs.QUEUED_KEY`) rather than merely blocked:
runs dispatch first approved, first launched, so a slot that comes free goes
to the run that has waited longest, and a queued run ages out after the longer
``queue_ttl`` instead of ``confirm_ttl``.

**A run launches the flow that made it.** Each run records what its rule
ran (kind, template, provider, worktree). If ``board.toml`` now says
something else, the run is expired instead of launched, and a rule that
needs a confirmation (every ``start =``, and ``mode = "confirm"``) only
launches a run somebody approved — an auto ``command =`` run can never be
edited into an unconfirmed agent.

**A retry looks before it launches.** A run with an attempt behind it and no
recorded window may still have one: the tick that opened it died, could not
write ``launched``, or tmux reported a failure after opening the window.
Before trying again, the tick folds in an exit file that attempt left, or
adopts its window (found by the run id in the pane's start command); only
when tmux confirms there is none does it open another. An agent run is
never relaunched on that alone: if a claimed ``start =`` run's earlier
attempt left no window, the event log decides whether that agent ran
(``done``) or nobody can tell (``stuck``).

**Coalescing.** A tick that finds the snapshot younger than
:data:`COALESCE_SEC` returns before it asks Jira anything, unless forced or
a run was approved since; a Jira 429 parks every tick until its
``Retry-After`` runs out (``flows/backoff.json``).

**Triggers.** ``on_enter`` and ``on_exit`` read the snapshot diff, narrowed
by a rule's ``from =`` and ``jql =``. ``on_pr`` and ``on_session`` read
observations of their own — one ``gh`` listing per repo, spaced
``pr_interval`` apart, and the session state files —
which :mod:`ephor.flows.triggers` compares with the previous snapshot's, so
all four share adopt mode, the stale gap, the guards and the run files. A
status run stays current while its visit to the status does; an ``on_pr
= "opened"`` run while its PR is open; an ``on_session = "idle"`` run while
its session stays idle; ``merged`` and ``died`` are final and stay current.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from ephor import eventlog, github, jira_api, work_items
from ephor.board import Assignee, BoardConfig, FlowRule, Mode, Trigger
from ephor.constants import AgentStatus
from ephor.filelock import locked
from ephor.flows import flows_dir, printable, runners, runs, triggers
from ephor.flows import snapshot as board_snapshot
from ephor.flows.runs import MAX_ATTEMPTS, Run, RunState
from ephor.flows.snapshot import (
    PrState,
    RefreshResult,
    RefreshStatus,
    SessionSeen,
    Snapshot,
    TicketState,
    Transition,
)
from ephor.flows.triggers import TriggerEvent
from ephor.github import PullRequest
from ephor.jira_api import Issue
from ephor.state.models import AgentState
from ephor.work_items import WorkItem

# The launch guard's memory: a session launched on a ticket this recently
# counts as "someone is already on this ticket".
LAUNCH_GUARD_SEC = 10 * 60.0
_HOUR = 3600.0
_STATUS_TRIGGERS = frozenset({Trigger.ON_ENTER, Trigger.ON_EXIT})


def _now() -> datetime:
    return datetime.now(UTC)


# --- dependencies -------------------------------------------------------------------
#
# Every call that leaves the process goes through TickDeps, so a test can
# run the whole algorithm against fakes. The defaults look their targets up
# at call time, which keeps monkeypatching a module attribute effective too.


def _search(jql: str, fields: tuple[str, ...]) -> list[Issue] | None:
    # The Flagged field is the one custom field the board asks for.
    flagged = next((f for f in fields if f.startswith("customfield_")), "")
    return jira_api.search(jql, fields, flagged_field=flagged)


def _search_error() -> jira_api.RequestError | None:
    return jira_api.last_error()


def _flagged_field() -> str | None:
    return jira_api.flagged_field_id()


def _myself() -> str | None:
    return jira_api.myself()


def _jira_identity() -> str:
    return jira_api.identity()


def _fetch_fresh(key: str) -> Issue | None:
    return jira_api.fetch_fresh(key)


def _sessions() -> list[AgentState]:
    from ephor.state.manager import StateManager

    return StateManager().scan()


def _ticket_of(agent: AgentState) -> str:
    """The session's ticket by fact only (see :func:`ephor.jira.confident_ticket_for_agent`)."""
    from ephor.jira import confident_ticket_for_agent

    return confident_ticket_for_agent(agent)


def _is_launch(event: eventlog.Event) -> bool:
    """True for a SESSION_STARTED written by a launch, not by a dashboard seeing a session.

    ``launcher.start``, ``launcher.resume`` and the flow runners all record
    what they started (a provider, a worktree, a run id). The dashboard's
    first sight of a session carries none of those, and is tagged
    ``observed`` besides — a dashboard restart starts nothing.
    """
    extra = event.extra or {}
    if extra.get("observed"):
        return False
    return bool(extra.get("run_id") or extra.get("worktree") or extra.get("provider"))


def _recent_starts(since_sec: float) -> dict[str, str]:
    """ticket -> time of its newest launched SESSION_STARTED within ``since_sec``."""
    found: dict[str, str] = {}
    for event in eventlog.read(since_sec=since_sec):
        if event.kind is eventlog.EventKind.SESSION_STARTED and event.ticket and _is_launch(event):
            found[event.ticket.upper()] = event.at
    return found


_EVIDENCE_KINDS = frozenset(
    {eventlog.EventKind.SESSION_STARTED, eventlog.EventKind.FLOW_DISPATCHED}
)


def _launch_evidence(run_id: str, since_sec: float) -> bool:
    """True when the event log says an agent was started for ``run_id``."""
    for event in eventlog.read(since_sec=since_sec):
        if event.kind in _EVIDENCE_KINDS and (event.extra or {}).get("run_id") == run_id:
            return True
    return False


def _work_item(key: str) -> WorkItem | None:
    return work_items.load(key)


def _live_panes() -> runners.LivePanes | None:
    return runners.live_panes()


def _find_prs(repo: Path, key: str) -> list[PullRequest] | None:
    return github.find_prs_for_ticket(repo, key)


def _list_prs(repo: Path) -> github.RepoPrs | None:
    return github.list_repo_prs(repo)


def _pid_alive(pid: int) -> bool:
    from ephor.state.manager import _is_pid_alive

    return _is_pid_alive(pid)


@dataclass(frozen=True)
class TickDeps:
    search: Callable[[str, tuple[str, ...]], list[Issue] | None] = _search
    myself: Callable[[], str | None] = _myself
    jira_identity: Callable[[], str] = _jira_identity
    fetch_fresh: Callable[[str], Issue | None] = _fetch_fresh
    sessions: Callable[[], list[AgentState]] = _sessions
    ticket_of: Callable[[AgentState], str] = _ticket_of
    recent_starts: Callable[[float], dict[str, str]] = _recent_starts
    launch_evidence: Callable[[str, float], bool] = _launch_evidence
    work_item: Callable[[str], WorkItem | None] = _work_item
    live_panes: Callable[[], runners.LivePanes | None] = _live_panes
    find_prs: Callable[[Path, str], list[PullRequest] | None] = _find_prs
    list_prs: Callable[[Path], github.RepoPrs | None] = _list_prs
    pid_alive: Callable[[int], bool] = _pid_alive
    # Why the last search failed (HTTP status, Jira's words, Retry-After).
    search_error: Callable[[], jira_api.RequestError | None] = _search_error
    # The site's Flagged field id: "" none, None unknown (asked again next tick).
    flagged_field: Callable[[], str | None] = _flagged_field
    launchers: runners.Launchers = field(default_factory=runners.Launchers)


# --- result -----------------------------------------------------------------------------


class TickStatus(StrEnum):
    OK = "ok"
    # The snapshot is younger than COALESCE_SEC: nothing was asked of Jira,
    # and the previous snapshot stands. Counts as ok.
    SKIPPED = "skipped"
    # Jira said 429 with a Retry-After that has not run out: not asked again.
    BACKOFF = "backoff"
    LOCKED = "locked"
    NO_CONFIG = "no_config"
    BOARD_INVALID = "board_invalid"
    JIRA_OFF = "jira_off"
    SEARCH_FAILED = "search_failed"
    MYSELF_FAILED = "myself_failed"
    WRITE_FAILED = "write_failed"


_REFRESH_STATUS = {
    TickStatus.OK: RefreshStatus.OK,
    TickStatus.SKIPPED: RefreshStatus.OK,
    TickStatus.BACKOFF: RefreshStatus.SEARCH_FAILED,
    TickStatus.LOCKED: RefreshStatus.LOCKED,
    TickStatus.NO_CONFIG: RefreshStatus.NO_CONFIG,
    TickStatus.BOARD_INVALID: RefreshStatus.BOARD_INVALID,
    TickStatus.JIRA_OFF: RefreshStatus.JIRA_OFF,
    TickStatus.SEARCH_FAILED: RefreshStatus.SEARCH_FAILED,
    TickStatus.MYSELF_FAILED: RefreshStatus.SEARCH_FAILED,
    TickStatus.WRITE_FAILED: RefreshStatus.WRITE_FAILED,
}


@dataclass(frozen=True)
class Action:
    """One thing the tick did (or, in a dry run, would do) to one run."""

    run_id: str
    verb: str  # created | dispatched | blocked | expired | done | failed | stuck | waiting
    detail: str = ""


@dataclass(frozen=True)
class TickResult:
    status: TickStatus
    snapshot: Snapshot | None = None
    transitions: tuple[Transition, ...] = ()
    # PR and session events observed this tick (whether or not a rule took them).
    events: tuple[TriggerEvent, ...] = ()
    actions: tuple[Action, ...] = ()
    adopted: tuple[str, ...] = ()  # rule names that adopted the board this tick
    # Why nothing was dispatched, when nothing could be ("" when dispatch ran).
    dispatch_note: str = ""
    dry_run: bool = False
    # What went wrong, in Jira's words (SEARCH_FAILED, BACKOFF), or why the
    # tick was skipped.
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (TickStatus.OK, TickStatus.SKIPPED)

    @property
    def refresh(self) -> RefreshResult:
        """The same outcome in the shape the status lanes read."""
        return RefreshResult(
            _REFRESH_STATUS[self.status],
            snapshot=self.snapshot,
            transitions=self.transitions,
            detail=self.detail if self.status is not TickStatus.SKIPPED else "",
        )

    @property
    def message(self) -> str:
        if self.status is TickStatus.MYSELF_FAILED:
            return 'Jira /myself did not answer — assignee = "me" rules cannot be matched yet'
        if self.status is TickStatus.SKIPPED:
            return self.detail
        return self.refresh.message


# --- the run ledger (real or dry) ------------------------------------------------------


class _Ledger:
    """The runs, read once per tick; writes go to disk unless this is a dry run."""

    def __init__(self, *, dry_run: bool, now: datetime) -> None:
        self.dry_run = dry_run
        self.now = now
        self.runs: dict[str, Run] = {r.run_id: r for r in runs.list_runs()}
        self.actions: list[Action] = []

    def note(self, run_id: str, verb: str, detail: str = "") -> None:
        self.actions.append(Action(run_id, verb, detail))

    def create(self, run: Run) -> bool:
        if run.run_id in self.runs:
            return False
        if self.dry_run:
            if runs.exists(run.run_id):
                return False
        elif not runs.create(run):
            return False
        self.runs[run.run_id] = run
        return True

    def update(self, run: Run, *, expect: Iterable[RunState], **changes: Any) -> Run | None:
        allowed = frozenset(expect)
        if self.dry_run:
            if run.state not in allowed:
                return None
            updated = replace(run, updated_at=runs.iso(self.now), **changes)
        else:
            maybe = runs.update(run.run_id, expect=allowed, now=self.now, **changes)
            if maybe is None:
                # Someone else moved it (an approve, a deny). Re-read, so the
                # rest of this tick works from what is actually on disk.
                fresh = runs.load(run.run_id)
                if fresh is not None:
                    self.runs[run.run_id] = fresh
                return None
            updated = maybe
        self.runs[run.run_id] = updated
        return updated

    def event(self, kind: eventlog.EventKind, run: Run, reason: str = "") -> None:
        if not self.dry_run:
            runs.log_event(kind, run, reason)


# The FLOW_* event a run's end writes, by the state it ended in.
_END_EVENT = {
    RunState.EXPIRED: eventlog.EventKind.FLOW_EXPIRED,
    RunState.STUCK: eventlog.EventKind.FLOW_STUCK,
    RunState.DENIED: eventlog.EventKind.FLOW_CANCELLED,
}


def _end_event(state: RunState) -> eventlog.EventKind:
    return _END_EVENT.get(state, eventlog.EventKind.FLOW_FINISHED)


# --- entry points -------------------------------------------------------------------


# A tick that finds the snapshot younger than this does nothing (unless
# forced): three processes ticking once a minute each — the dashboard, a
# `flows watch`, a timer — would otherwise ask Jira three times a minute.
# Under the TUI's sixty seconds, so one ticker on its own is never skipped.
COALESCE_SEC = 45.0
BACKOFF_FILE = "backoff.json"
# A 429 without a usable Retry-After still backs off, for this long.
DEFAULT_BACKOFF_SEC = 60.0
_MAX_BACKOFF_SEC = 3600.0


def backoff_path() -> Path:
    return flows_dir() / BACKOFF_FILE


def backoff_until(now: datetime | None = None) -> tuple[datetime, str] | None:
    """When a rate limit Jira asked for runs out, and why; None when there is none in force."""
    try:
        data = json.loads(backoff_path().read_text())
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    until = runs.parse_iso(runs.as_str(data.get("until")))
    if until is None or until <= (now or _now()):
        return None
    return until, runs.as_str(data.get("reason"))


def _write_backoff(seconds: float, reason: str, now: datetime) -> None:
    until = now + timedelta(seconds=min(max(seconds, 1.0), _MAX_BACKOFF_SEC))
    runs.write_atomic(backoff_path(), json.dumps({"until": runs.iso(until), "reason": reason}))


def _clear_backoff() -> None:
    with contextlib.suppress(OSError):
        backoff_path().unlink(missing_ok=True)


def run(
    config: BoardConfig | None,
    *,
    dry_run: bool = False,
    force: bool = False,
    deps: TickDeps | None = None,
    now: datetime | None = None,
) -> TickResult:
    """One tick, under ``tick.lock`` (non-blocking). Blocking otherwise.

    A dry run takes no lock and writes nothing: no snapshot, no run file, no
    event, no window. It reads everything a real tick would, including the
    pre-launch Jira re-check, and reports what it would have done.

    A real tick is **skipped** (``SKIPPED``, which is ok) when the snapshot
    is younger than :data:`COALESCE_SEC` — unless ``force``, or a run was
    approved since that snapshot, which is what the dashboard's "approve,
    then tick" relies on. Nothing is read from Jira for a skipped tick. Nor
    for one inside a Retry-After that Jira gave (``BACKOFF``), forced or
    not: that is the server asking.
    """
    problem = _precondition(config)
    if problem is not None or config is None:
        return TickResult(problem or TickStatus.NO_CONFIG, dry_run=dry_run)
    tools = deps or TickDeps()
    moment = now or _now()
    if dry_run:
        return _tick_locked(config, dry_run=True, force=True, deps=tools, now=moment)
    with locked(board_snapshot.tick_lock_path(), timeout=0) as acquired:
        if not acquired:
            return TickResult(TickStatus.LOCKED, snapshot=board_snapshot.load())
        return _tick_locked(config, dry_run=False, force=force, deps=tools, now=moment)


def _coalesce(previous: Snapshot | None, now: datetime) -> str:
    """Why this tick can be skipped ("" when it cannot): a fresh snapshot, nothing approved since."""
    if previous is None:
        return ""
    age = previous.age_sec(now)
    taken = previous.taken
    if age is None or taken is None or age >= COALESCE_SEC or taken > now:
        return ""
    for found in runs.list_runs():
        if found.state is RunState.CLAIMED and found.attempts == 0:
            approved = runs.parse_iso(found.approved_at or found.updated_at)
            if approved is None or approved >= taken:
                return ""
    return f"skipped: the snapshot is {int(age)}s old (ticks closer than {COALESCE_SEC:g}s coalesce; --force to tick anyway)"


def _precondition(config: BoardConfig | None) -> TickStatus | None:
    if config is None:
        return TickStatus.NO_CONFIG
    if not config.lanes_ok:
        return TickStatus.BOARD_INVALID
    if not jira_api.is_configured():
        return TickStatus.JIRA_OFF
    return None


def _tick_locked(
    config: BoardConfig, *, dry_run: bool, force: bool, deps: TickDeps, now: datetime
) -> TickResult:
    previous = board_snapshot.load()
    held = backoff_until(now)
    if held is not None:
        until, why = held
        local = until.astimezone().strftime("%H:%M:%S")
        return TickResult(
            TickStatus.BACKOFF,
            snapshot=previous,
            dry_run=dry_run,
            detail=f"Jira asked ephor to back off ({why or 'HTTP 429'}) — next search after {local}",
        )
    skip = "" if force else _coalesce(previous, now)
    if skip:
        return TickResult(TickStatus.SKIPPED, snapshot=previous, dry_run=dry_run, detail=skip)

    who = deps.jira_identity()
    flagged_field, flagged_for = _flagged(previous, deps, who)
    issues = board_snapshot.fetch(config.board, search=deps.search, flagged_field=flagged_field)
    if issues is None:
        return _search_failed(previous, deps, dry_run=dry_run, now=now)
    if not dry_run:
        _clear_backoff()
    snap, transitions = board_snapshot.advance(
        previous, issues, now=now, seq_floor=board_snapshot.baseline_seq_floor(previous)
    )
    snap = replace(snap, flagged_field=flagged_field, flagged_for=flagged_for)
    scope = board_snapshot.board_scope(config.board)
    # A wider (or different) search brings tickets into view that did not
    # move: adopt them, as a new rule adopts the board it first sees.
    scope_changed = previous is not None and previous.scope != scope
    snap = replace(snap, scope=scope)

    notes: list[str] = []
    rules: list[FlowRule] = []
    if not config.dispatch_ok:
        notes.append(f"board.toml has {len(config.errors)} error(s) — no flow runs")
    else:
        rules = list(config.rules)
        if scope_changed and any(r.trigger in _STATUS_TRIGGERS for r in rules):
            notes.append("board scope changed — tickets new to the search were adopted")

    # The cached accountId is only "me" for the credentials it was fetched
    # with: after a switch of site or user, ask again rather than match
    # assignee = "me" rules against somebody else's id.
    myself = snap.myself if snap.myself_for == who else ""
    if not myself and any(r.assignee is Assignee.ME for r in rules):
        myself = deps.myself() or ""
        if not myself:
            # Unknown never means unmet: write nothing, so the moves this
            # tick saw are still moves when the next tick can match them.
            return TickResult(TickStatus.MYSELF_FAILED, snapshot=previous, dry_run=dry_run)
    snap = replace(snap, myself=myself, myself_for=who if myself else "")

    context = _Context(deps, settle=config.flows.settle, now=now)
    snap, events = _observe(config, snap, rules, context, now)

    ledger = _Ledger(dry_run=dry_run, now=now)
    adopted: list[str] = []
    # A rule's jql filter that could not be checked holds this tick's
    # snapshot back: its moves are then still moves next tick, and fire (or
    # not) once the filter can be asked. Runs other rules made this tick are
    # claimed by file, so seeing their events again creates nothing twice.
    hold_snapshot = False
    if config.dispatch_ok:
        stale = _stale_gap(previous, config, now)
        seen = set(previous.rules_seen) if previous is not None else set()
        for rule in rules:
            if previous is None or rule.fingerprint not in seen:
                adopted.append(rule.name)
                continue
            moves: list[Transition] = []
            fired: list[TriggerEvent] = []
            if rule.trigger in _STATUS_TRIGGERS:
                for move in transitions:
                    if scope_changed and move.appeared:
                        continue
                    record = snap.tickets.get(move.key)
                    if record is not None and _matches(rule, move, record, myself):
                        moves.append(move)
            else:
                for event in events:
                    record = snap.tickets.get(event.key)
                    if record is not None and _event_matches(rule, event, record, myself):
                        fired.append(event)
            candidates = {m.key for m in moves} | {e.key for e in fired}
            if rule.jql and candidates:
                passing, problem = jql_filter(rule, candidates, deps)
                if passing is None:
                    notes.append(problem)
                    hold_snapshot = hold_snapshot or not problem.endswith(_JQL_REJECTED_TAIL)
                    continue
                moves = [m for m in moves if m.key in passing]
                fired = [e for e in fired if e.key in passing]
            for move in moves:
                _create_run(ledger, rule, move, snap.tickets[move.key], stale=stale)
            for event in fired:
                _create_event_run(ledger, rule, event, snap.tickets[event.key], stale=stale)
        snap = replace(snap, rules_seen=tuple(sorted({r.fingerprint for r in rules})))

    # A session observation that is not running (no on_session rule in
    # effect, e.g. while board.toml has errors) is unknown, not "not idle".
    watching_sessions = any(r.trigger is Trigger.ON_SESSION for r in rules)
    _tend(ledger, config, snap, context, watching_sessions=watching_sessions)
    if config.dispatch_ok:
        if runs.is_paused():
            notes.insert(0, "paused (ephor flows resume)")
        _dispatch(ledger, config, snap, context, watching_sessions=watching_sessions)
    dispatch_note = "; ".join(notes)

    if not dry_run and hold_snapshot:
        # Everything above is safe to repeat, which is what holding the
        # snapshot back asks of the next tick.
        runs.prune(now)
        return TickResult(
            TickStatus.OK,
            snapshot=previous,
            transitions=tuple(transitions),
            events=tuple(events),
            actions=tuple(ledger.actions),
            adopted=tuple(adopted),
            dispatch_note="; ".join([*notes, "snapshot held back: retried next tick"]),
            dry_run=dry_run,
        )
    if not dry_run:
        board_snapshot.log_transitions(transitions)
        runs.prune(now)
        # Written last: everything above is safe to repeat if this tick dies.
        if not board_snapshot.save(snap):
            return TickResult(
                TickStatus.WRITE_FAILED,
                snapshot=previous,
                transitions=tuple(transitions),
                actions=tuple(ledger.actions),
            )
    return TickResult(
        TickStatus.OK,
        snapshot=snap,
        transitions=tuple(transitions),
        events=tuple(events),
        actions=tuple(ledger.actions),
        adopted=tuple(adopted),
        dispatch_note=dispatch_note,
        dry_run=dry_run,
    )


def _flagged(previous: Snapshot | None, deps: TickDeps, who: str) -> tuple[str, str]:
    """The Flagged field id to ask the search for, and the identity it is cached for.

    Looked up once per Jira identity and kept in the snapshot, like
    ``myself``. Fails soft: a lookup that does not answer leaves the field
    out of this search (no ticket reads as flagged) and asks again next tick.
    """
    if previous is not None and previous.flagged_for and previous.flagged_for == who:
        return previous.flagged_field, previous.flagged_for
    found: str | None = None
    with contextlib.suppress(Exception):
        found = deps.flagged_field()
    if found is None:
        return "", ""
    return found, who


def _search_failed(
    previous: Snapshot | None, deps: TickDeps, *, dry_run: bool, now: datetime
) -> TickResult:
    """A failed board search: say why, in Jira's words, and honour a Retry-After."""
    error: jira_api.RequestError | None = None
    with contextlib.suppress(Exception):
        error = deps.search_error()
    detail = ""
    if error is not None:
        detail = f"Jira search failed ({error.summary}) — snapshot left as it was"
        if error.status == 429 and not dry_run:
            _write_backoff(
                error.retry_after if error.retry_after is not None else DEFAULT_BACKOFF_SEC,
                error.summary,
                now,
            )
    return TickResult(TickStatus.SEARCH_FAILED, snapshot=previous, dry_run=dry_run, detail=detail)


_JQL_REJECTED_TAIL = "(Jira rejected it; fix the flow's jql)"
_JQL_FIELDS: tuple[str, ...] = ("status",)


def jql_filter(rule: FlowRule, keys: Iterable[str], deps: TickDeps) -> tuple[set[str] | None, str]:
    """Which of ``keys`` also meet ``rule.jql``: one search for all of them.

    Returns (keys that pass, "") — or (None, why) when the search did not
    answer, which is unknown: nothing fires on it. A query Jira *rejected*
    (HTTP 400) will not get better by asking again, so its reason ends in
    :data:`_JQL_REJECTED_TAIL` and the tick does not hold its snapshot for it.
    """
    wanted = sorted({k.strip().upper() for k in keys if k.strip()})
    if not wanted:
        return set(), ""
    query = f"key in ({', '.join(wanted)}) AND ({rule.jql})"
    found: list[Issue] | None = None
    with contextlib.suppress(Exception):
        found = deps.search(query, _JQL_FIELDS)
    if found is None:
        error: jira_api.RequestError | None = None
        with contextlib.suppress(Exception):
            error = deps.search_error()
        why = f" ({error.summary})" if error is not None else ""
        if error is not None and error.status == 400:
            return None, f"flow {rule.name}: jql filter{why} {_JQL_REJECTED_TAIL}"
        return None, f"flow {rule.name}: jql filter did not answer{why}"
    return {i.key.strip().upper() for i in found}, ""


# --- PR and session observations ------------------------------------------------------


def _observe(
    config: BoardConfig,
    snap: Snapshot,
    rules: list[FlowRule],
    context: _Context,
    now: datetime,
) -> tuple[Snapshot, list[TriggerEvent]]:
    """Refresh the snapshot's PR and session observations; the events between them.

    Each observation is kept only while a rule reads it. With no ``on_pr``
    (or ``on_session``) rule in effect — none configured, or a config with
    errors — it is cleared, so a rule added later starts from a fresh
    baseline instead of firing on everything that changed while nobody was
    looking. That is the same thing that happens to status moves while a
    config is broken: they are seen, and fire nothing.
    """
    events: list[TriggerEvent] = []
    pr_rules = [r for r in rules if r.trigger is Trigger.ON_PR]
    if not pr_rules:
        snap = replace(snap, prs={}, pr_repos={}, pr_checked_at="")
    elif triggers.pr_lookup_due(snap.pr_checked_at, config.flows.pr_interval, now):
        keys = triggers.pr_tickets(snap, pr_rules, snap.myself)
        observed = triggers.observe_prs(
            snap.prs,
            keys,
            tickets=snap.tickets,
            repo_for=lambda key: pr_repo_for(config, pr_rules, key, context.deps.work_item(key)),
            list_prs=context.deps.list_prs,
            find=context.deps.find_prs,
            previous_repos=snap.pr_repos,
        )
        snap = replace(snap, prs=observed.prs, pr_repos=observed.repos, pr_checked_at=runs.iso(now))
        events.extend(observed.events)
    else:
        snap = replace(
            snap,
            prs={k: v for k, v in snap.prs.items() if k in snap.tickets},
            pr_repos={k: v for k, v in snap.pr_repos.items() if k in snap.tickets},
        )

    if not any(r.trigger is Trigger.ON_SESSION for r in rules):
        snap = replace(snap, sessions_seen={})
    else:
        agents = context.all_sessions()
        if agents is not None:
            seen, found = triggers.observe_sessions(
                snap.sessions_seen,
                agents,
                ticket_of=context.deps.ticket_of,
                pid_alive=context.deps.pid_alive,
                settle=config.flows.settle,
                now=now,
            )
            snap = replace(snap, sessions_seen=seen)
            events.extend(found)
    return snap, events


def pr_repo_for(
    config: BoardConfig, pr_rules: Iterable[FlowRule], key: str, work: WorkItem | None
) -> Path | None:
    """The repo ``key``'s PRs are looked up in: its own, else the first on_pr rule's ``repo =``."""
    repo, _ = config.resolve_repo(key, work)
    if repo is not None:
        return repo
    for rule in pr_rules:
        if rule.repo:
            found, _ = config.repo_for_rule(rule, key, work)
            if found is not None:
                return found
    return None


# --- matching and creating ------------------------------------------------------------


def _stale_gap(previous: Snapshot | None, config: BoardConfig, now: datetime) -> bool:
    """True when the previous snapshot is older than ``max_event_age`` (or undated)."""
    if previous is None:
        return False
    age = previous.age_sec(now)
    return age is None or age > config.flows.max_event_age


def _matches(rule: FlowRule, move: Transition, record: TicketState, myself: str) -> bool:
    """True when ``move`` is the event ``rule`` fires on.

    A ticket that *appears* on the board already in a status counts as
    entering it: with the default JQL that is a ticket just assigned to you,
    which is exactly when a review of it becomes yours to start.
    """
    if rule.assignee is Assignee.ME and (not myself or record.assignee_id != myself):
        return False
    if rule.trigger is Trigger.ON_ENTER:
        # ``from =``: where it came from. A ticket new to the board came
        # from nowhere, which no from list names.
        if rule.from_statuses and (move.appeared or not rule.matches_from(move.from_status)):
            return False
        return rule.matches_status(move.to_status)
    if rule.trigger is Trigger.ON_EXIT:
        # A ticket new to the board left nothing.
        return not move.appeared and rule.matches_status(move.from_status)
    return False


def _event_matches(rule: FlowRule, event: TriggerEvent, record: TicketState, myself: str) -> bool:
    """True when the PR or session ``event`` is the one ``rule`` fires on.

    The ticket must be on the board — a ticket the last search missed is
    never fired on, whatever its PR or session did.
    """
    if rule.trigger is not event.trigger or rule.value != event.value:
        return False
    if not record.on_board:
        return False
    return triggers.assignee_ok(rule, record, myself)


def _generation(rule: FlowRule, move: Transition) -> str:
    if rule.trigger is Trigger.ON_EXIT:
        # The status left, numbered by the move that left it: each exit is
        # its own event even when the ticket keeps coming back.
        return runs.status_generation(move.from_status_id, move.from_status, move.seq)
    return runs.status_generation(move.to_status_id, move.to_status, move.seq)


def _create_run(
    ledger: _Ledger, rule: FlowRule, move: Transition, record: TicketState, *, stale: bool
) -> None:
    generation = _generation(rule, move)
    run_id = runs.make_run_id(move.key, rule.name, generation)
    if not runs.valid_run_id(run_id):
        return
    # A stale run always waits for a human; otherwise the rule's mode decides.
    confirm = stale or rule.mode is Mode.CONFIRM
    state = RunState.AWAITING_CONFIRM if confirm else RunState.CLAIMED
    stamp = runs.iso(ledger.now)
    candidate = Run(
        run_id=run_id,
        flow=rule.name,
        ticket=move.key,
        trigger=str(rule.trigger),
        generation=generation,
        state=state,
        created_at=stamp,
        updated_at=stamp,
        stale=stale,
        reason="stale: created after a gap in ticks" if stale else "",
        kind="start" if rule.launches_agent else "command",
        action=rule.action_fingerprint,
        title=record.title,
        from_status=move.from_status,
        to_status=move.to_status,
        from_status_id=move.from_status_id,
        status_id=move.to_status_id,
        seq=move.seq,
    )
    if ledger.create(candidate):
        ledger.note(run_id, "created", f"{state}{' (stale)' if stale else ''} · {move.detail}")
        ledger.event(eventlog.EventKind.FLOW_CREATED, candidate, f"created: {move.detail}")


def _create_event_run(
    ledger: _Ledger, rule: FlowRule, event: TriggerEvent, record: TicketState, *, stale: bool
) -> None:
    run_id = runs.make_run_id(event.key, rule.name, event.generation)
    if not runs.valid_run_id(run_id):
        return
    confirm = stale or rule.mode is Mode.CONFIRM
    state = RunState.AWAITING_CONFIRM if confirm else RunState.CLAIMED
    stamp = runs.iso(ledger.now)
    pr = event.pr
    candidate = Run(
        run_id=run_id,
        flow=rule.name,
        ticket=event.key,
        trigger=str(rule.trigger),
        generation=event.generation,
        state=state,
        created_at=stamp,
        updated_at=stamp,
        stale=stale,
        reason="stale: created after a gap in ticks" if stale else "",
        kind="start" if rule.launches_agent else "command",
        action=rule.action_fingerprint,
        title=record.title,
        # The ticket's status when the event fired: what {status} renders as.
        to_status=record.status,
        status_id=record.status_id,
        seq=record.seq,
        event=event.value,
        pr_number=pr.number if pr is not None else 0,
        pr_url=pr.url if pr is not None else "",
        head_sha=pr.head_sha if pr is not None else "",
        session_id=event.session_id,
        idle_since=event.idle_since,
    )
    if ledger.create(candidate):
        ledger.note(run_id, "created", f"{state}{' (stale)' if stale else ''} · {event.detail}")
        ledger.event(eventlog.EventKind.FLOW_CREATED, candidate, f"created: {event.detail}")


def generation_current(
    run: Run,
    tickets: Mapping[str, TicketState],
    *,
    prs: Mapping[str, tuple[PrState, ...]] | None = None,
    sessions: Mapping[str, SessionSeen] | None = None,
) -> bool:
    """True while the event that fired ``run`` still holds.

    For a status trigger, while it is still the ticket's latest move. For
    ``on_pr = "opened"``, while the PR is open (``prs`` None, or a PR the
    last lookup did not see, is unknown and counts as open); for
    ``on_session = "idle"``, while the session stays idle (``sessions``
    None is unknown). ``merged`` and ``died`` cannot be undone. A ticket
    that has left the snapshot altogether ends every run on it.
    """
    record = tickets.get(run.ticket)
    if record is None:
        return False
    if run.trigger == str(Trigger.ON_PR):
        if run.event == triggers.PR_OPENED and prs is not None:
            return triggers.pr_still_open(run.ticket, run.pr_number, prs)
        return True
    if run.trigger == str(Trigger.ON_SESSION):
        if run.event == triggers.SESSION_IDLE and sessions is not None:
            return triggers.session_still_idle(run.session_id, sessions, run.idle_since)
        return True
    if record.seq != run.seq:
        return False
    if run.trigger == str(Trigger.ON_ENTER):
        return record.status_identity == (run.status_id or run.to_status.strip().lower())
    return True


def _current(run: Run, snap: Snapshot, *, watching_sessions: bool) -> bool:
    return generation_current(
        run,
        snap.tickets,
        prs=snap.prs,
        sessions=snap.sessions_seen if watching_sessions else None,
    )


# --- tending runs that already exist ---------------------------------------------------


def _tend(
    ledger: _Ledger,
    config: BoardConfig,
    snap: Snapshot,
    context: _Context,
    *,
    watching_sessions: bool,
) -> None:
    """Expire and finish runs, before anything new is dispatched."""
    for run in sorted(ledger.runs.values(), key=lambda r: (r.created_at, r.run_id)):
        if run.state is RunState.AWAITING_CONFIRM:
            _tend_awaiting(ledger, config, snap, run, watching_sessions=watching_sessions)
        elif run.state in (RunState.CLAIMED, RunState.BLOCKED):
            if not _current(run, snap, watching_sessions=watching_sessions):
                _cancel(ledger, run, RunState.EXPIRED, _moved_reason(run, snap))
            elif _held_too_long(run, config, ledger.now):
                why = QUEUED_TOO_LONG if runs.is_queued(run) else HELD_TOO_LONG
                _cancel(ledger, run, RunState.EXPIRED, why)
        elif run.state is RunState.LAUNCHED:
            _tend_launched(ledger, run, context.panes())


HELD_TOO_LONG = "held past confirm_ttl"
QUEUED_TOO_LONG = "queued past queue_ttl"


def _held_too_long(run: Run, config: BoardConfig, now: datetime) -> bool:
    """True when a run waiting to launch has waited longer than ``confirm_ttl``.

    A guard can hold a run for days — a session that never quite leaves the
    ticket, a long pause — and the event that fired it, or the yes it was
    given, is not a reason to act days later. Age counts from the approval
    for a confirmed run, from creation for an auto one. A claimed run whose
    launch is already under way is left to the retry logic, which knows how
    to look for its window.
    """
    if run.state is RunState.CLAIMED and run.attempts > 0:
        return False
    age = runs.age_sec(run.approved_at or run.rearmed_at or run.created_at, now)
    # A queued run is waiting its turn for an agent slot, not on a guard
    # that may never clear, so it gets the longer of the two.
    ttl = config.flows.confirm_ttl
    if runs.is_queued(run):
        ttl = max(ttl, config.flows.queue_ttl)
    return age is not None and age > ttl


FLOW_CHANGED = "flow changed since this run was created"


def _rule_changed(run: Run, rule: FlowRule, *, approved_needed: bool) -> str:
    """Why ``rule`` is no longer the flow that created ``run``, or "".

    A run launches what its rule says *now*, so a run made by one rule must
    not go out as another: an auto ``command =`` run edited into ``start =``
    would be an agent nobody approved, and an approval was for the flow that
    was shown. ``approved_needed`` adds the dispatch-time half: a rule that
    now wants a confirmation (any ``start =``, any ``mode = "confirm"``)
    only launches a run somebody approved.
    """
    kind = "start" if rule.launches_agent else "command"
    if run.kind and run.kind != kind:
        return FLOW_CHANGED
    if run.action and run.action != rule.action_fingerprint:
        return FLOW_CHANGED
    needs_yes = rule.launches_agent or rule.mode is Mode.CONFIRM
    if approved_needed and needs_yes and not run.approved_at:
        return FLOW_CHANGED
    return ""


def _moved_reason(run: Run, snap: Snapshot) -> str:
    record = snap.tickets.get(run.ticket)
    if record is None:
        return "ticket left the board"
    if run.trigger == str(Trigger.ON_PR):
        return f"PR #{run.pr_number} is no longer open"
    if run.trigger == str(Trigger.ON_SESSION):
        return f"session {run.session_id[:8]} is no longer idle"
    return f"ticket moved on (now {record.status or '?'})"


def _tend_awaiting(
    ledger: _Ledger, config: BoardConfig, snap: Snapshot, run: Run, *, watching_sessions: bool
) -> None:
    age = runs.age_sec(run.rearmed_at or run.created_at, ledger.now)
    if age is not None and age > config.flows.confirm_ttl:
        _cancel(ledger, run, RunState.EXPIRED, "not confirmed within confirm_ttl")
    elif not _current(run, snap, watching_sessions=watching_sessions):
        _cancel(ledger, run, RunState.EXPIRED, _moved_reason(run, snap))
    elif config.dispatch_ok:
        rule = config.rule(run.flow)
        if rule is None:
            _cancel(ledger, run, RunState.EXPIRED, "flow no longer in board.toml")
        elif changed := _rule_changed(run, rule, approved_needed=False):
            # Approving it now would approve something nobody was shown.
            _cancel(ledger, run, RunState.EXPIRED, changed)


def _cancel(ledger: _Ledger, run: Run, state: RunState, reason: str) -> None:
    updated = ledger.update(run, expect={run.state}, state=state, reason=reason)
    if updated is not None:
        ledger.note(run.run_id, str(state), reason)
        ledger.event(_end_event(state), updated, reason)


def _tend_launched(ledger: _Ledger, run: Run, panes: runners.LivePanes | None) -> None:
    code = runs.read_exit(run.run_id) if run.kind == "command" else None
    if code is not None:
        state = RunState.DONE if code == 0 else RunState.FAILED
        reason = f"exited {code}"
    elif panes is not None and run.pane and not panes.has(run.pane, run.tmux_server):
        if run.kind == "command":
            state, reason = RunState.FAILED, "window closed without an exit status"
        else:
            state, reason = RunState.DONE, "agent window closed"
    else:
        return
    updated = ledger.update(
        run, expect={RunState.LAUNCHED}, state=state, reason=reason, exit_code=code
    )
    if updated is not None:
        ledger.note(run.run_id, str(state), reason)
        ledger.event(eventlog.EventKind.FLOW_FINISHED, updated, reason)


# --- dispatch ----------------------------------------------------------------------------


@dataclass
class _Context:
    """What the guards read, gathered at most once per tick."""

    deps: TickDeps
    # [flows] settle: a session idle longer than this is not "on" its
    # ticket for the launch guard (see _launch_guard).
    settle: float = 0.0
    now: datetime | None = None
    # Tickets a start= run was launched on during this tick (or, in a dry
    # run, would have been). The cached session scan and event-log read
    # predate those launches, so the agent cap and the launch guard count
    # them from here.
    launched: set[str] = field(default_factory=set)
    _all_sessions: list[AgentState] | None = None
    _all_sessions_read: bool = False
    _sessions: list[AgentState] | None = None
    _starts: dict[str, str] | None = None
    # One `tmux list-panes -a` per tick answers every pane question: is a
    # launched run's pane alive, did an unrecorded attempt leave a window,
    # which server a new pane is on.
    _panes: runners.LivePanes | None = None
    _panes_read: bool = False
    _panes_reread: bool = False

    def all_sessions(self) -> list[AgentState] | None:
        """Every session state file, DEAD ones included; None when the scan failed."""
        if not self._all_sessions_read:
            self._all_sessions_read = True
            try:
                self._all_sessions = list(self.deps.sessions())
            except OSError:
                self._all_sessions = None
        return self._all_sessions

    def live_sessions(self) -> list[AgentState]:
        if self._sessions is None:
            self._sessions = [
                a for a in self.all_sessions() or [] if a.status is not AgentStatus.DEAD
            ]
        return self._sessions

    def working_sessions(self) -> list[AgentState]:
        """Live sessions, less those settled-idle past ``settle`` — the launch guard's view.

        An agent that opened its PR and has sat at its prompt since is done
        with the ticket for every practical purpose; holding a review behind
        it until somebody closes the window is what made the PR hand-off
        wait for ``confirm_ttl``. One still working, or quiet for less than
        ``settle``, still counts.
        """
        if self.now is None or self.settle <= 0:
            return self.live_sessions()
        moment = self.now
        return [a for a in self.live_sessions() if not triggers.is_settled(a, self.settle, moment)]

    def recent_starts(self) -> dict[str, str]:
        if self._starts is None:
            self._starts = self.deps.recent_starts(LAUNCH_GUARD_SEC)
        return self._starts

    def panes(self) -> runners.LivePanes | None:
        """This tick's tmux listing; None when tmux did not answer."""
        if not self._panes_read:
            self._panes, self._panes_read = self.deps.live_panes(), True
        return self._panes

    def flow_panes(self) -> Mapping[str, str] | None:
        """run id -> pane of every live flow window; None when tmux did not answer."""
        live = self.panes()
        return None if live is None else live.flows

    def server_of(self, pane: str) -> str:
        """The tmux server ``pane`` lives on (see :class:`runners.LivePanes`), or "" if unknown.

        A pane this tick just opened is not in the listing taken before it,
        but it was opened on the same socket, so it is on the server that
        listing names. Only when there was no server to name — this launch
        started one — is tmux asked again, once; a pane still not found
        records "" (unknown), which :meth:`runners.LivePanes.has` accepts.
        """
        if not pane:
            return ""
        live = self.panes()
        if live is not None and pane in live.panes:
            return live.server
        if live is not None and live.server:
            return live.server
        if live is not None and not self._panes_reread:
            self._panes_reread = True
            self._panes = live = self.deps.live_panes()
            if live is not None and pane in live.panes:
                return live.server
        return ""


def _launches_within(
    ledger: _Ledger,
    seconds: float,
    *,
    exclude: str,
    ticket: str | None = None,
    agents: bool | None = None,
) -> int:
    """Runs (other than ``exclude``) that opened a window within ``seconds``.

    ``launched_at`` is stamped only when a window opened, so an attempt that
    failed (tmux down) costs nothing. A run counts once however many
    attempts it took; the run being dispatched is left out. ``agents``
    picks start= runs (True) or command= runs (False): the two kinds have
    separate budgets.
    """
    count = 0
    for run in ledger.runs.values():
        if run.run_id == exclude or (ticket is not None and run.ticket != ticket):
            continue
        if agents is not None and (run.kind == "start") != agents:
            continue
        age = runs.age_sec(run.launched_at, ledger.now)
        if age is not None and age < seconds:
            count += 1
    return count


def _dispatch(
    ledger: _Ledger,
    config: BoardConfig,
    snap: Snapshot,
    context: _Context,
    *,
    watching_sessions: bool,
) -> None:
    ready = [r for r in ledger.runs.values() if r.state in (RunState.CLAIMED, RunState.BLOCKED)]
    # Queue order — first approved, first launched — so a slot that comes
    # free goes to the run that has waited longest for it.
    for run in sorted(ready, key=runs.queue_key):
        current = ledger.runs.get(run.run_id, run)
        if current.state not in (RunState.CLAIMED, RunState.BLOCKED):
            continue
        _dispatch_one(ledger, config, snap, context, current, watching_sessions=watching_sessions)


def _dispatch_one(
    ledger: _Ledger,
    config: BoardConfig,
    snap: Snapshot,
    context: _Context,
    run: Run,
    *,
    watching_sessions: bool,
) -> None:
    rule = config.rule(run.flow)
    if rule is None:
        _cancel(ledger, run, RunState.EXPIRED, "flow no longer in board.toml")
        return
    changed = _rule_changed(run, rule, approved_needed=True)
    if changed:
        _cancel(ledger, run, RunState.EXPIRED, changed)
        return
    # _tend has already expired runs whose event is over, but a run can
    # reach here without passing it: an approve that lands while the tick is
    # expiring that very run is re-read as claimed. Every launch path is
    # gated on currency here, whatever order the tick did things in.
    if not _current(run, snap, watching_sessions=watching_sessions):
        _cancel(ledger, run, RunState.EXPIRED, _moved_reason(run, snap))
        return

    if run.state is RunState.CLAIMED and run.attempts > 0:
        # A launch began and never reported a window: the tick that started
        # it died, or tmux did. Give it time to show up, then try again.
        waited = runs.age_sec(run.attempt_at or run.launched_at or run.updated_at, ledger.now)
        if waited is not None and waited < runs.CLAIM_TIMEOUT_SEC:
            ledger.note(run.run_id, "waiting", "a launch is in progress")
            return
    if run.state is RunState.BLOCKED and run.retry_at:
        # A failed launch backs off before it is tried again (see retry_delay).
        due = runs.parse_iso(run.retry_at)
        if due is not None and due > ledger.now:
            left = int((due - ledger.now).total_seconds())
            ledger.note(run.run_id, "waiting", f"retry in {left}s after: {run.reason}")
            return
    if _unrecorded_attempt(run):
        if _recover_attempt(ledger, context, run):
            return
        if run.kind == "start" and run.state is RunState.CLAIMED:
            # An agent attempt that never recorded how it went, and no
            # window left to show for it. Never open a second agent on one
            # approval on the strength of "probably did not start".
            _settle_unaccounted_agent(ledger, config, context, run)
            return
    if run.attempts >= MAX_ATTEMPTS:
        _finish(ledger, run, RunState.STUCK, f"{run.attempts} launch attempts without a window")
        return

    key, reason = _guard(ledger, config, context, run, rule)
    if reason:
        _block(ledger, run, reason, key=key)
        return

    repo, how = config.repo_for_rule(rule, run.ticket, context.deps.work_item(run.ticket))
    if rule.launches_agent and repo is None:
        _block(ledger, run, how, key="no_repo")
        return

    fresh = context.deps.fetch_fresh(run.ticket)
    if fresh is None:
        _block(ledger, run, "Jira re-check did not answer")
        return
    if fresh.key.strip().upper() != run.ticket.upper():
        _block(ledger, run, f"Jira re-check returned {fresh.key or 'no ticket'}, not {run.ticket}")
        return
    if not (fresh.status_id or fresh.status.strip()):
        # A partial answer, not a status: unknown never means unmet, so it
        # can neither launch the run nor end it.
        _block(ledger, run, "Jira re-check returned no status")
        return
    moved = _recheck(run, rule, fresh, snap.myself)
    if moved:
        _cancel(ledger, run, RunState.EXPIRED, moved)
        return

    _launch(ledger, config, snap, context, run, rule, repo=repo, repo_how=how)


# A failed launch waits RETRY_BASE_SEC before its second attempt, twice
# that before its third, and so on, up to RETRY_MAX_SEC — so a tmux server
# that is down for a quarter of an hour does not use up MAX_ATTEMPTS in the
# first two minutes of it.
RETRY_BASE_SEC = 60.0
RETRY_MAX_SEC = 30 * 60.0


def retry_delay(attempts: int) -> float:
    """Seconds to wait after the ``attempts``-th failed launch before the next."""
    return float(min(RETRY_BASE_SEC * 2.0 ** max(0, attempts - 1), RETRY_MAX_SEC))


# Why a retry is parked when tmux cannot say whether the last attempt's
# window exists.
_TMUX_UNKNOWN = "tmux did not answer — cannot tell whether the last attempt is running"
UNACCOUNTED = "earlier attempt unaccounted for"


def _unrecorded_attempt(run: Run) -> bool:
    """True when an earlier attempt may have opened a window nobody recorded.

    Any run with an attempt behind it that has not reached ``launched``. A
    claimed one counted its attempt before opening the window and never
    wrote ``launched``; a blocked one may follow a launch tmux *reported*
    as failed — a ``new-window`` that timed out, or printed no ids — after
    the window had in fact opened. Either way, look before launching again.
    """
    return run.attempts > 0 and run.state in (RunState.CLAIMED, RunState.BLOCKED)


def _settle_unaccounted_agent(
    ledger: _Ledger, config: BoardConfig, context: _Context, run: Run
) -> None:
    """End a start= run whose earlier attempt left no window and no record.

    If the event log shows that attempt starting an agent, the agent ran
    and its window has since closed: the run is done. If it does not, the
    attempt may have died before tmux was asked, or after the window opened
    but before anything was written — there is no telling which, so the run
    is stuck rather than relaunched. A human can start the flow again.
    """
    if context.deps.launch_evidence(run.run_id, max(config.flows.confirm_ttl, _HOUR)):
        _finish(
            ledger, run, RunState.DONE, "agent window closed (an attempt that was never recorded)"
        )
    else:
        _finish(ledger, run, RunState.STUCK, UNACCOUNTED)


def _recover_attempt(ledger: _Ledger, context: _Context, run: Run) -> bool:
    """Settle what an unrecorded earlier attempt did. True when the run must not relaunch."""
    code = runs.read_exit(run.run_id) if run.kind == "command" else None
    if code is not None:
        # It ran to completion: one execution per event, so record it.
        state = RunState.DONE if code == 0 else RunState.FAILED
        reason = f"exited {code} (an earlier attempt that was never recorded)"
        updated = ledger.update(run, expect={run.state}, state=state, reason=reason, exit_code=code)
        if updated is not None:
            ledger.note(run.run_id, str(state), reason)
            ledger.event(eventlog.EventKind.FLOW_FINISHED, updated, reason)
        return True
    panes = context.flow_panes()
    if panes is None:
        # Unknown never means unmet: a window may be running this very run.
        _block(ledger, run, _TMUX_UNKNOWN)
        return True
    pane = panes.get(run.run_id)
    if pane is None:
        return False
    reason = "adopted window from an earlier attempt"
    payload = runs.payload_path(run.run_id)
    adopted = ledger.update(
        run,
        expect={run.state},
        state=RunState.LAUNCHED,
        window=runners.window_name(run.ticket, run.flow),
        launched_at=run.launched_at or run.attempt_at or runs.iso(ledger.now),
        pane=pane,
        tmux_server=context.server_of(pane),
        payload_path=str(payload) if payload.exists() else run.payload_path,
        reason=reason,
    )
    if adopted is not None:
        if run.kind == "start":
            context.launched.add(run.ticket)
        ledger.note(run.run_id, "dispatched", f"{pane} ({reason})")
        ledger.event(eventlog.EventKind.FLOW_DISPATCHED, adopted, reason)
    return True


_CLEAR = ("", "")


def _guard(
    ledger: _Ledger, config: BoardConfig, context: _Context, run: Run, rule: FlowRule
) -> tuple[str, str]:
    """The first guard that says "not now", as (key, reason); ("", "") when none does.

    The key names the guard without the numbers in its reason (counts and
    ages change every tick), so a run parked on one guard is written once.
    """
    settings = config.flows
    if runs.is_paused():
        return "paused", "paused"
    if not rule.launches_agent:
        # command= flows have their own budget; the agent caps are for agents.
        commands = _launches_within(ledger, _HOUR, exclude=run.run_id, agents=False)
        if commands >= settings.max_commands_per_hour:
            return (
                "max_commands_per_hour",
                f"max_commands_per_hour: {commands} command launches in the last hour",
            )
        return _CLEAR
    hourly = _launches_within(ledger, _HOUR, exclude=run.run_id, agents=True)
    if hourly >= settings.max_runs_per_hour:
        return "max_runs_per_hour", f"max_runs_per_hour: {hourly} agent launches in the last hour"
    per_ticket = _launches_within(ledger, _HOUR, exclude=run.run_id, ticket=run.ticket, agents=True)
    if per_ticket >= settings.ticket_breaker:
        return (
            "ticket_breaker",
            f"ticket_breaker: {per_ticket} agent launches for {run.ticket} in the last hour",
        )
    if context.all_sessions() is None:
        # Unknown never means unmet: a scan that failed is not zero agents.
        return "scan_failed", "session scan failed — cannot count live agents"
    # The agent queue. A slot is a session still at work: one idle past
    # ``settle`` has finished (opened its PR, answered, gone quiet) and frees
    # its slot without anybody closing its window. The scan is from before
    # this tick launched anything, so the agents it has started since take a
    # slot on top of it — which is what keeps the rest of the queue waiting
    # once the head of it has taken the slot that came free.
    active = len(context.working_sessions()) + len(context.launched)
    if active >= settings.max_active_agents:
        return (
            runs.QUEUED_KEY,
            f"queued: {active}/{settings.max_active_agents} agent slots busy",
        )
    return _launch_guard(ledger, context, run.ticket)


def _ago(raw: str, now: datetime) -> str:
    age = runs.age_sec(raw, now)
    return "just now" if age is None or age < 60 else f"{int(age // 60)}m ago"


def _launch_guard(ledger: _Ledger, context: _Context, ticket: str) -> tuple[str, str]:
    """Why an agent is (or may already be) on ``ticket``, as (key, reason); or ("", "")."""
    if ticket in context.launched:
        return (
            "launch_guard:this_tick",
            f"launch guard: an agent was launched on {ticket} this tick",
        )
    for agent in context.working_sessions():
        bound = (agent.ticket or "").upper() or context.deps.ticket_of(agent)
        if bound == ticket:
            return (
                f"launch_guard:session:{agent.session_id}",
                f"launch guard: session {agent.session_id[:8]} is live on {ticket}",
            )
    started = context.recent_starts().get(ticket)
    if started:
        return (
            "launch_guard:started",
            f"launch guard: a session was started on {ticket} {_ago(started, ledger.now)}",
        )
    # Not the work record's ``last_seen``: every passive write bumps it (the
    # dashboard promoting a key, a Jira status folded in on repaint), so it
    # says "somebody looked at this ticket", not "somebody started work on
    # it". A start is what the launched SESSION_STARTED above records.
    item = context.deps.work_item(ticket)
    if item is not None:
        live_ids = {a.session_id for a in context.working_sessions()}
        if any(sid in live_ids for sid in item.session_ids):
            return (
                "launch_guard:recorded_session",
                f"launch guard: a live session is recorded on {ticket}",
            )
    return _CLEAR


def _recheck(run: Run, rule: FlowRule, fresh: Issue, myself: str) -> str:
    """Why the uncached ticket no longer fits the run, or ""."""
    fresh_identity = fresh.status_id or fresh.status.strip().lower()
    if rule.trigger is Trigger.ON_ENTER:
        wanted = run.status_id or run.to_status.strip().lower()
        if fresh_identity != wanted:
            return f"ticket is now {fresh.status or '?'}, not {run.to_status}"
    elif rule.trigger is Trigger.ON_EXIT:
        left = run.from_status_id or run.from_status.strip().lower()
        if fresh_identity == left:
            return f"ticket is back in {run.from_status}"
    if rule.assignee is Assignee.ME and fresh.assignee_id != myself:
        return "ticket is no longer assigned to you"
    return ""


def _block(ledger: _Ledger, run: Run, reason: str, *, key: str = "", retry_at: str = "") -> None:
    """Park ``run`` as blocked. Once per guard: a re-block for the same key writes nothing.

    ``key`` defaults to the reason itself, which suits reasons that never
    change. The tick's own note carries the fresh reason either way.
    ``retry_at`` (a failed launch's backoff) always writes: it moves.
    """
    key = key or reason
    if (
        not retry_at
        and run.state is RunState.BLOCKED
        and (run.block_key == key or (not run.block_key and run.reason == reason))
    ):
        ledger.note(run.run_id, "blocked", reason)
        return
    updated = ledger.update(
        run,
        expect={RunState.CLAIMED, RunState.BLOCKED},
        state=RunState.BLOCKED,
        reason=reason,
        block_key=key,
        retry_at=retry_at,
    )
    if updated is not None:
        ledger.note(run.run_id, "blocked", reason)
        ledger.event(eventlog.EventKind.FLOW_BLOCKED, updated, reason)


def _finish(ledger: _Ledger, run: Run, state: RunState, reason: str) -> None:
    updated = ledger.update(
        run, expect={RunState.CLAIMED, RunState.BLOCKED}, state=state, reason=reason
    )
    if updated is not None:
        ledger.note(run.run_id, str(state), reason)
        ledger.event(_end_event(state), updated, reason)


def payload_for(run: Run, snap: Snapshot, repo: Path | None) -> dict[str, Any]:
    """What a flow is told about the event that fired it."""
    record = snap.tickets.get(run.ticket)
    return {
        "run_id": run.run_id,
        "flow": run.flow,
        "ticket": run.ticket,
        "title": run.title,
        "trigger": run.trigger,
        "from_status": run.from_status,
        "to_status": run.to_status,
        "status_id": run.status_id,
        "assignee": record.assignee if record is not None else "",
        "assignee_id": record.assignee_id if record is not None else "",
        "event": run.event,
        "pr": {
            "number": run.pr_number,
            "url": run.pr_url,
            "head_sha": run.head_sha,
            "state": _pr_state_of(run, snap),
        }
        if run.pr_number
        else None,
        "session_id": run.session_id,
        "repo_path": str(repo) if repo is not None else "",
    }


def _pr_state_of(run: Run, snap: Snapshot) -> str:
    for pr in snap.prs.get(run.ticket, ()):
        if pr.number == run.pr_number:
            return pr.state
    return ""


def _launch(
    ledger: _Ledger,
    config: BoardConfig,
    snap: Snapshot,
    context: _Context,
    run: Run,
    rule: FlowRule,
    *,
    repo: Path | None,
    repo_how: str,
) -> None:
    where = f" in {repo}" if repo is not None else ""
    if ledger.dry_run:
        # Record the would-be launch in the in-memory ledger (a dry ledger
        # writes nothing), so the caps later runs in this dry tick check
        # count it, exactly as a real tick's would.
        ledger.update(
            run,
            expect={RunState.CLAIMED, RunState.BLOCKED},
            attempts=run.attempts + 1,
            attempt_at=runs.iso(ledger.now),
            launched_at=runs.iso(ledger.now),
        )
        if rule.launches_agent:
            context.launched.add(run.ticket)
        ledger.note(
            run.run_id, "dispatch", f"would open {runners.window_name(run.ticket, run.flow)}{where}"
        )
        return
    # Count the attempt *before* opening the window. If this process dies
    # between here and the LAUNCHED write, the next tick sees a claimed run
    # with attempts > 0 and waits CLAIM_TIMEOUT_SEC (from attempt_at) rather
    # than launching a second window straight away. launched_at — what the
    # hourly caps count — waits until a window has actually opened.
    claimed = ledger.update(
        run,
        expect={RunState.CLAIMED, RunState.BLOCKED},
        state=RunState.CLAIMED,
        attempts=run.attempts + 1,
        attempt_at=runs.iso(ledger.now),
        retry_at="",
        reason="launching",
    )
    if claimed is None:
        return
    # This attempt must be judged by its own exit status, never by one an
    # earlier attempt left behind. Safe to clear only now: _dispatch_one has
    # already folded in any exit status an earlier attempt recorded.
    runs.clear_exit(claimed.run_id)
    payload = runs.write_payload(claimed.run_id, payload_for(claimed, snap, repo))
    if payload is None:
        _block(ledger, claimed, "could not write the payload file")
        return
    outcome = runners.launch(
        claimed,
        rule,
        repo=repo,
        repo_is_worktree=repo_how == "worktree",
        payload=payload,
        settings=config.flows,
        launchers=context.deps.launchers,
    )
    if outcome.fatal:
        _finish(ledger, claimed, RunState.FAILED, outcome.message)
        return
    if not outcome.ok:
        if claimed.attempts >= MAX_ATTEMPTS:
            _finish(ledger, claimed, RunState.STUCK, f"launch failed: {outcome.message}")
        else:
            delay = retry_delay(claimed.attempts)
            _block(
                ledger,
                claimed,
                f"launch failed: {outcome.message}",
                key="launch_failed",
                retry_at=runs.iso(ledger.now + timedelta(seconds=delay)),
            )
        return
    if rule.launches_agent:
        # Counted whether or not the LAUNCHED write below lands: the agent
        # is running either way.
        context.launched.add(run.ticket)
    launched = ledger.update(
        claimed,
        expect={RunState.CLAIMED},
        state=RunState.LAUNCHED,
        window=outcome.window,
        pane=outcome.pane,
        tmux_server=context.server_of(outcome.pane),
        payload_path=str(payload),
        launched_at=runs.iso(ledger.now),
        reason=f"launched{where}",
    )
    if launched is not None:
        ledger.note(run.run_id, "dispatched", f"{outcome.window} {outcome.pane}{where}".strip())
        ledger.event(eventlog.EventKind.FLOW_DISPATCHED, launched, f"launched{where}")


# --- explain: why a rule would, or would not, fire on a ticket ---------------------------


@dataclass(frozen=True)
class Explanation:
    """One rule, judged against one ticket as the last snapshot has it.

    ``gate`` is the first check that failed ("" when none did), in the order
    the tick applies them; ``detail`` says what it saw. ``passed`` lists the
    checks before it, and ``notes`` what would shape a run without stopping
    it (a stale gap, a PR lookup not due yet).
    """

    flow: str
    gate: str
    detail: str
    passed: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def fires(self) -> bool:
        return not self.gate


def explain(
    config: BoardConfig,
    key: str,
    flow: str | None = None,
    *,
    deps: TickDeps | None = None,
    now: datetime | None = None,
) -> tuple[list[Explanation], str]:
    """For each rule (or just ``flow``), would it fire on ``key`` now — and if not, why not.

    Read-only: no lock, no snapshot write, no run file, no window. It reads
    the last snapshot rather than searching the board, and applies the same
    functions the tick does — the assignee filter, the status match, the run
    file names, the jql filter (one search), the guards and the repo lookup
    — so its answer is the tick's, not a second opinion. Returns the
    explanations and a problem that stopped them all ("" when none did).
    """
    tools = deps or TickDeps()
    moment = now or _now()
    ticket = (key or "").strip().upper()
    if not config.lanes_ok:
        return [], "board.toml [board] has errors — `ephor flows check`"
    rules = [r for r in config.rules if flow is None or r.name == flow]
    if flow is not None and not rules:
        return [], f"no flow named {flow!r} in board.toml"
    if not rules:
        return [], "board.toml has no [[flow]] rules"
    snap = board_snapshot.load()
    if snap is None:
        return [], "no snapshot yet — run a tick first (`ephor flows tick`)"
    ledger = _Ledger(dry_run=True, now=moment)
    context = _Context(tools, settle=config.flows.settle, now=moment)
    found = [_explain_rule(config, snap, ledger, context, rule, ticket, moment) for rule in rules]
    return found, ""


def _explain_rule(
    config: BoardConfig,
    snap: Snapshot,
    ledger: _Ledger,
    context: _Context,
    rule: FlowRule,
    key: str,
    now: datetime,
) -> Explanation:
    passed: list[str] = []
    notes: list[str] = []

    def stop(gate: str, detail: str) -> Explanation:
        return Explanation(rule.name, gate, detail, tuple(passed), tuple(notes))

    if not config.dispatch_ok:
        return stop("config", f"board.toml has {len(config.errors)} error(s) — no flow runs")
    record = snap.tickets.get(key)
    if record is None or not record.on_board:
        where = "is not in the snapshot" if record is None else "was missing from the last search"
        return stop("not on board", f"{key} {where}")
    passed.append("on board")
    if rule.fingerprint not in snap.rules_seen:
        return stop(
            "adopting",
            "the rule is new (or retargeted): the next tick records the board as it stands, "
            "and it fires on the move after that",
        )
    passed.append("rule adopted")
    if not triggers.assignee_ok(rule, record, snap.myself):
        who = printable_name(record.assignee) or "nobody"
        why = "your Jira accountId is not cached yet" if not snap.myself else f"assigned to {who}"
        return stop("assignee", f'assignee = "me", but {key} is {why}')
    passed.append("assignee")

    event = _explain_event(config, snap, context, rule, key, record, now, notes)
    if isinstance(event, Explanation):
        return replace(event, passed=tuple(passed), notes=tuple(notes))
    generation, what = event
    passed.append(what)

    run_id = runs.make_run_id(key, rule.name, generation)
    existing = ledger.runs.get(run_id)
    if existing is not None and existing.state not in (RunState.CLAIMED, RunState.BLOCKED):
        reason = f": {existing.reason}" if existing.reason else ""
        return stop("run exists", f"{run_id} is {existing.state}{reason}")
    if existing is None:
        notes.append(
            f"no run exists for this event ({run_id}): it happened before the rule could fire "
            "on it (adopted, or filtered out at the time) — the next such event fires it"
        )
    if rule.jql:
        passing, problem = jql_filter(rule, [key], context.deps)
        if passing is None:
            return stop("jql", problem)
        if key not in passing:
            return stop("jql", f"{key} does not match the flow's jql: {rule.jql}")
        passed.append("jql")
    if _stale_gap(snap, config, now):
        notes.append(
            "the snapshot is older than max_event_age: a run the next tick creates is stale "
            "and waits for your confirmation"
        )

    probe = existing or Run(
        run_id=run_id,
        flow=rule.name,
        ticket=key,
        trigger=str(rule.trigger),
        generation=generation,
        state=RunState.CLAIMED,
        created_at=runs.iso(now),
        updated_at=runs.iso(now),
        kind="start" if rule.launches_agent else "command",
    )
    guard_key, reason = _guard(ledger, config, context, probe, rule)
    if reason:
        gate = (
            "paused"
            if guard_key == "paused"
            else "caps"
            if "launch_guard" not in guard_key
            else "launch guard"
        )
        return stop(gate, reason)
    passed.append("guards")
    repo, how = config.repo_for_rule(rule, key, context.deps.work_item(key))
    if rule.launches_agent and repo is None:
        return stop("no repo", how)
    passed.append(f"repo ({how}: {repo})" if repo is not None else "repo ($HOME)")
    if rule.mode is Mode.CONFIRM or rule.launches_agent:
        notes.append("mode = confirm: a run waits for `ephor flows approve` before it launches")
    if existing is not None:
        return Explanation(
            rule.name,
            "",
            f"{run_id} is {existing.state} and would launch now",
            tuple(passed),
            tuple(notes),
        )
    return Explanation(
        rule.name, "", "matches, and nothing would hold the launch", tuple(passed), tuple(notes)
    )


def printable_name(text: str) -> str:
    return printable(text, 40)


def _explain_event(
    config: BoardConfig,
    snap: Snapshot,
    context: _Context,
    rule: FlowRule,
    key: str,
    record: TicketState,
    now: datetime,
    notes: list[str],
) -> tuple[str, str] | Explanation:
    """The generation the rule would fire on for ``key`` now, and the check that found it."""

    def stop(gate: str, detail: str) -> Explanation:
        return Explanation(rule.name, gate, detail)

    if rule.trigger is Trigger.ON_ENTER:
        if not rule.matches_status(record.status):
            return stop("status match", f"{key} is in {record.status or '?'}, not {rule.value}")
        if rule.from_statuses and not rule.matches_from(record.from_status):
            came = record.from_status or "off the board"
            return stop(
                "status match", f"{key} came from {came}; from = {list(rule.from_statuses)}"
            )
        gen = runs.status_generation(record.status_id, record.status, record.seq)
        return gen, f"status match ({record.status}, visit {record.seq})"
    if rule.trigger is Trigger.ON_EXIT:
        if not record.from_status or not rule.matches_status(record.from_status):
            last = (
                f"{record.from_status} → {record.status}" if record.from_status else record.status
            )
            return stop(
                "status match", f"{key}'s last move was {last}; it has not left {rule.value}"
            )
        gen = runs.status_generation(record.from_status_id, record.from_status, record.seq)
        return gen, f"status match (left {record.from_status})"
    if rule.trigger is Trigger.ON_PR:
        repo = pr_repo_for(config, [rule], key, context.deps.work_item(key))
        if repo is None:
            return stop("no repo", f"{key} has no worktree and no [repos] (or rule repo) entry")
        due = triggers.pr_lookup_due(snap.pr_checked_at, config.flows.pr_interval, now)
        if not due:
            checked = runs.parse_iso(snap.pr_checked_at)
            wait = config.flows.pr_interval - ((now - checked).total_seconds() if checked else 0.0)
            notes.append(f"next PR lookup in {max(0, int(wait))}s")
        if key not in snap.prs:
            gate = "PR lookup not due" if not due else "no event"
            return stop(gate, f"{key} has no PR observation yet (the first lookup is a baseline)")
        wanted = [
            pr
            for pr in snap.prs_for(key)
            if (pr.is_open if rule.value == triggers.PR_OPENED else pr.is_merged)
        ]
        if not wanted:
            return stop("no event", f"no {rule.value} PR seen for {key} at the last lookup")
        pr = wanted[-1]
        return runs.pr_generation(pr.number), f"PR #{pr.number} {rule.value}"
    seen = [(sid, s) for sid, s in snap.sessions_seen.items() if s.ticket == key]
    if rule.value == triggers.SESSION_IDLE:
        idle = [(sid, s) for sid, s in seen if s.settled]
        if not idle:
            return stop("no event", f"no session on {key} has been idle for settle")
        sid, spell = idle[-1]
        gen = (
            runs.idle_generation(sid, spell.idle_since)
            if spell.idle_since
            else runs.session_generation(sid)
        )
        return gen, f"session {sid[:8]} idle"
    return stop("no event", f"no session on {key} has died since it was last seen alive")

"""``ephor flows tick``: look at the board once, and act on what moved.

A tick is the rule-less snapshot refresh (:mod:`ephor.flows.snapshot`) with
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

**Guards, in order,** before anything is launched: the pause flag, the
hourly cap across all flows, the per-ticket breaker, and — for flows that
start an agent — the cap on live agents and the launch guard (an agent is
already on this ticket, or one was started in the last ten minutes, or its
work record was touched in the last ten minutes, or this same tick already
launched one there). Then the ticket is re-read
from Jira, uncached: a search is eventually consistent, and an agent must
not start on a status the ticket has already left (a re-read with no status
is unknown, and blocks). A guard that says "not now" parks the run as
``blocked``; it is tried again on every tick for as long as the event that
fired it is still the ticket's latest — but not for ever: a run held longer
than ``confirm_ttl`` (from its approval, or from its creation for an auto
run) expires, so a yes given days ago, or an event that arrived during a
long pause, does not act today.

**A run launches the flow that made it.** Each run records what its rule
ran (kind, template, provider, worktree). If ``board.toml`` now says
something else, the run is superseded instead of launched, and a rule that
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

**Triggers.** ``on_enter`` and ``on_exit`` read the snapshot diff.
``on_pr`` and ``on_session`` read observations of their own — ``gh``'s PRs
per ticket, spaced ``pr_interval`` apart, and the session state files —
which :mod:`ephor.flows.triggers` compares with the previous snapshot's, so
all four share adopt mode, the stale gap, the guards and the run files. A
status run stays current while its visit to the status does; an ``on_pr
= "opened"`` run while its PR is open; an ``on_session = "idle"`` run while
its session stays idle; ``merged`` and ``died`` are final and stay current.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ephor import eventlog, github, jira_api, work_items
from ephor.board import Assignee, BoardConfig, FlowRule, Mode, Trigger
from ephor.constants import AgentStatus
from ephor.filelock import locked
from ephor.flows import runners, runs, triggers
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

# The launch guard's memory: a session started, or a work record touched,
# this recently counts as "someone is already on this ticket".
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
    return jira_api.search(jql, fields)


def _myself() -> str | None:
    return jira_api.myself()


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


def _flow_panes() -> dict[str, str] | None:
    return runners.flow_panes()


def _find_prs(repo: Path, key: str) -> list[PullRequest] | None:
    return github.find_prs_for_ticket(repo, key)


def _pid_alive(pid: int) -> bool:
    from ephor.state.manager import _is_pid_alive

    return _is_pid_alive(pid)


@dataclass(frozen=True)
class TickDeps:
    search: Callable[[str, tuple[str, ...]], list[Issue] | None] = _search
    myself: Callable[[], str | None] = _myself
    fetch_fresh: Callable[[str], Issue | None] = _fetch_fresh
    sessions: Callable[[], list[AgentState]] = _sessions
    ticket_of: Callable[[AgentState], str] = _ticket_of
    recent_starts: Callable[[float], dict[str, str]] = _recent_starts
    launch_evidence: Callable[[str, float], bool] = _launch_evidence
    work_item: Callable[[str], WorkItem | None] = _work_item
    live_panes: Callable[[], runners.LivePanes | None] = _live_panes
    flow_panes: Callable[[], dict[str, str] | None] = _flow_panes
    find_prs: Callable[[Path, str], list[PullRequest] | None] = _find_prs
    pid_alive: Callable[[int], bool] = _pid_alive
    launchers: runners.Launchers = field(default_factory=runners.Launchers)


# --- result -----------------------------------------------------------------------------


class TickStatus(StrEnum):
    OK = "ok"
    LOCKED = "locked"
    NO_CONFIG = "no_config"
    BOARD_INVALID = "board_invalid"
    JIRA_OFF = "jira_off"
    SEARCH_FAILED = "search_failed"
    MYSELF_FAILED = "myself_failed"
    WRITE_FAILED = "write_failed"


_REFRESH_STATUS = {
    TickStatus.OK: RefreshStatus.OK,
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
    verb: str  # created | dispatched | blocked | expired | superseded | done | failed | stuck | waiting
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

    @property
    def ok(self) -> bool:
        return self.status is TickStatus.OK

    @property
    def refresh(self) -> RefreshResult:
        """The same outcome in the shape the status lanes read."""
        return RefreshResult(
            _REFRESH_STATUS[self.status], snapshot=self.snapshot, transitions=self.transitions
        )

    @property
    def message(self) -> str:
        if self.status is TickStatus.MYSELF_FAILED:
            return 'Jira /myself did not answer — assignee = "me" rules cannot be matched yet'
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


# --- entry points -------------------------------------------------------------------


def run(
    config: BoardConfig | None,
    *,
    dry_run: bool = False,
    deps: TickDeps | None = None,
    now: datetime | None = None,
) -> TickResult:
    """One tick, under ``tick.lock`` (non-blocking). Blocking otherwise.

    A dry run takes no lock and writes nothing: no snapshot, no run file, no
    event, no window. It reads everything a real tick would, including the
    pre-launch Jira re-check, and reports what it would have done.
    """
    problem = _precondition(config)
    if problem is not None or config is None:
        return TickResult(problem or TickStatus.NO_CONFIG, dry_run=dry_run)
    tools = deps or TickDeps()
    moment = now or _now()
    if dry_run:
        return _tick_locked(config, dry_run=True, deps=tools, now=moment)
    with locked(board_snapshot.tick_lock_path(), timeout=0) as acquired:
        if not acquired:
            return TickResult(TickStatus.LOCKED, snapshot=board_snapshot.load())
        return _tick_locked(config, dry_run=False, deps=tools, now=moment)


def _precondition(config: BoardConfig | None) -> TickStatus | None:
    if config is None:
        return TickStatus.NO_CONFIG
    if not config.lanes_ok:
        return TickStatus.BOARD_INVALID
    if not jira_api.is_configured():
        return TickStatus.JIRA_OFF
    return None


def _tick_locked(
    config: BoardConfig, *, dry_run: bool, deps: TickDeps, now: datetime
) -> TickResult:
    previous = board_snapshot.load()
    issues = board_snapshot.fetch(config.board, search=deps.search)
    if issues is None:
        return TickResult(TickStatus.SEARCH_FAILED, snapshot=previous, dry_run=dry_run)
    snap, transitions = board_snapshot.advance(
        previous, issues, now=now, seq_floor=board_snapshot.baseline_seq_floor(previous)
    )
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

    myself = snap.myself
    if not myself and any(r.assignee is Assignee.ME for r in rules):
        myself = deps.myself() or ""
        if not myself:
            # Unknown never means unmet: write nothing, so the moves this
            # tick saw are still moves when the next tick can match them.
            return TickResult(TickStatus.MYSELF_FAILED, snapshot=previous, dry_run=dry_run)
    snap = replace(snap, myself=myself)

    context = _Context(deps)
    snap, events = _observe(config, snap, rules, context, now)

    ledger = _Ledger(dry_run=dry_run, now=now)
    adopted: list[str] = []
    if config.dispatch_ok:
        stale = _stale_gap(previous, config, now)
        seen = set(previous.rules_seen) if previous is not None else set()
        for rule in rules:
            if previous is None or rule.fingerprint not in seen:
                adopted.append(rule.name)
                continue
            if rule.trigger in _STATUS_TRIGGERS:
                for move in transitions:
                    if scope_changed and move.appeared:
                        continue
                    record = snap.tickets.get(move.key)
                    if record is not None and _matches(rule, move, record, myself):
                        _create_run(ledger, rule, move, record, stale=stale)
                continue
            for event in events:
                record = snap.tickets.get(event.key)
                if record is not None and _event_matches(rule, event, record, myself):
                    _create_event_run(ledger, rule, event, record, stale=stale)
        snap = replace(snap, rules_seen=tuple(sorted({r.fingerprint for r in rules})))

    # A session observation that is not running (no on_session rule in
    # effect, e.g. while board.toml has errors) is unknown, not "not idle".
    watching_sessions = any(r.trigger is Trigger.ON_SESSION for r in rules)
    _tend(ledger, config, snap, deps, watching_sessions=watching_sessions)
    if config.dispatch_ok:
        if runs.is_paused():
            notes.insert(0, "paused (ephor flows resume)")
        _dispatch(ledger, config, snap, context, watching_sessions=watching_sessions)
    dispatch_note = "; ".join(notes)

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
        snap = replace(snap, prs={}, pr_checked_at="")
    elif triggers.pr_lookup_due(snap.pr_checked_at, config.flows.pr_interval, now):
        keys = triggers.pr_tickets(snap, pr_rules, snap.myself)
        prs, found = triggers.observe_prs(
            snap.prs,
            keys,
            tickets=snap.tickets,
            repo_for=lambda key: config.resolve_repo(key, context.deps.work_item(key))[0],
            find=context.deps.find_prs,
        )
        snap = replace(snap, prs=prs, pr_checked_at=runs.iso(now))
        events.extend(found)
    else:
        snap = replace(snap, prs={k: v for k, v in snap.prs.items() if k in snap.tickets})

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
    )
    if ledger.create(candidate):
        ledger.note(run_id, "created", f"{state}{' (stale)' if stale else ''} · {event.detail}")


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
            return triggers.session_still_idle(run.session_id, sessions)
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
    deps: TickDeps,
    *,
    watching_sessions: bool,
) -> None:
    """Expire, supersede and finish runs, before anything new is dispatched."""
    panes: runners.LivePanes | None = None
    panes_read = False
    for run in sorted(ledger.runs.values(), key=lambda r: (r.created_at, r.run_id)):
        if run.state is RunState.AWAITING_CONFIRM:
            _tend_awaiting(ledger, config, snap, run, watching_sessions=watching_sessions)
        elif run.state in (RunState.CLAIMED, RunState.BLOCKED):
            if not _current(run, snap, watching_sessions=watching_sessions):
                _cancel(ledger, run, RunState.SUPERSEDED, _moved_reason(run, snap))
            elif _held_too_long(run, config, ledger.now):
                _cancel(ledger, run, RunState.EXPIRED, HELD_TOO_LONG)
        elif run.state is RunState.LAUNCHED:
            if not panes_read:
                panes, panes_read = deps.live_panes(), True
            _tend_launched(ledger, run, panes)


HELD_TOO_LONG = "held past confirm_ttl"


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
    age = runs.age_sec(run.approved_at or run.created_at, now)
    return age is not None and age > config.flows.confirm_ttl


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
    age = runs.age_sec(run.created_at, ledger.now)
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
        ledger.event(eventlog.EventKind.FLOW_CANCELLED, updated, reason)


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
    # Tickets a start= run was launched on during this tick (or, in a dry
    # run, would have been). The cached session scan and event-log read
    # predate those launches, so the agent cap and the launch guard count
    # them from here.
    launched: set[str] = field(default_factory=set)
    _all_sessions: list[AgentState] | None = None
    _all_sessions_read: bool = False
    _sessions: list[AgentState] | None = None
    _starts: dict[str, str] | None = None
    _flow_panes: dict[str, str] | None = None
    _flow_panes_read: bool = False

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

    def recent_starts(self) -> dict[str, str]:
        if self._starts is None:
            self._starts = self.deps.recent_starts(LAUNCH_GUARD_SEC)
        return self._starts

    def flow_panes(self) -> dict[str, str] | None:
        """run id -> pane of every live flow window; None when tmux did not answer."""
        if not self._flow_panes_read:
            self._flow_panes, self._flow_panes_read = self.deps.flow_panes(), True
        return self._flow_panes


def _launches_within(
    ledger: _Ledger, seconds: float, *, exclude: str, ticket: str | None = None
) -> int:
    """Runs (other than ``exclude``) whose launch began within ``seconds``.

    A run counts once however many attempts it took; the run being
    dispatched is left out, so its own failed attempt cannot hold it back.
    """
    count = 0
    for run in ledger.runs.values():
        if run.run_id == exclude or (ticket is not None and run.ticket != ticket):
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
    for run in sorted(ready, key=lambda r: (r.created_at, r.run_id)):
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
        _cancel(ledger, run, RunState.SUPERSEDED, "flow no longer in board.toml")
        return
    changed = _rule_changed(run, rule, approved_needed=True)
    if changed:
        _cancel(ledger, run, RunState.SUPERSEDED, changed)
        return
    # _tend has already superseded runs whose event is over, but a run can
    # reach here without passing it: an approve that lands while the tick is
    # expiring that very run is re-read as claimed. Every launch path is
    # gated on currency here, whatever order the tick did things in.
    if not _current(run, snap, watching_sessions=watching_sessions):
        _cancel(ledger, run, RunState.SUPERSEDED, _moved_reason(run, snap))
        return

    if run.state is RunState.CLAIMED and run.attempts > 0:
        # A launch began and never reported a window: the tick that started
        # it died, or tmux did. Give it time to show up, then try again.
        waited = runs.age_sec(run.launched_at or run.updated_at, ledger.now)
        if waited is not None and waited < runs.CLAIM_TIMEOUT_SEC:
            ledger.note(run.run_id, "waiting", "a launch is in progress")
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

    repo, how = config.resolve_repo(run.ticket, context.deps.work_item(run.ticket))
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
        _cancel(ledger, run, RunState.SUPERSEDED, moved)
        return

    _launch(ledger, config, snap, context, run, rule, repo=repo, repo_how=how)


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
        pane=pane,
        tmux_server=_server_of(context, pane),
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
    hourly = _launches_within(ledger, _HOUR, exclude=run.run_id)
    if hourly >= settings.max_runs_per_hour:
        return "max_runs_per_hour", f"max_runs_per_hour: {hourly} launches in the last hour"
    per_ticket = _launches_within(ledger, _HOUR, exclude=run.run_id, ticket=run.ticket)
    if per_ticket >= settings.ticket_breaker:
        return (
            "ticket_breaker",
            f"ticket_breaker: {per_ticket} launches for {run.ticket} in the last hour",
        )
    if not rule.launches_agent:
        return _CLEAR
    if context.all_sessions() is None:
        # Unknown never means unmet: a scan that failed is not zero agents.
        return "scan_failed", "session scan failed — cannot count live agents"
    # The session scan is from before this tick launched anything, so the
    # agents it has started since count on top of it.
    active = len(context.live_sessions()) + len(context.launched)
    if active >= settings.max_active_agents:
        return "max_active_agents", f"max_active_agents: {active} sessions running"
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
    for agent in context.live_sessions():
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
    item = context.deps.work_item(ticket)
    if item is not None:
        if (age := runs.age_sec(item.last_seen, ledger.now)) is not None and (
            age < LAUNCH_GUARD_SEC
        ):
            return (
                "launch_guard:work_record",
                f"launch guard: {ticket}'s work record was updated {_ago(item.last_seen, ledger.now)}",
            )
        live_ids = {a.session_id for a in context.live_sessions()}
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


def _block(ledger: _Ledger, run: Run, reason: str, *, key: str = "") -> None:
    """Park ``run`` as blocked. Once per guard: a re-block for the same key writes nothing.

    ``key`` defaults to the reason itself, which suits reasons that never
    change. The tick's own note carries the fresh reason either way.
    """
    key = key or reason
    if run.state is RunState.BLOCKED and (
        run.block_key == key or (not run.block_key and run.reason == reason)
    ):
        ledger.note(run.run_id, "blocked", reason)
        return
    updated = ledger.update(
        run,
        expect={RunState.CLAIMED, RunState.BLOCKED},
        state=RunState.BLOCKED,
        reason=reason,
        block_key=key,
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
        ledger.event(eventlog.EventKind.FLOW_FINISHED, updated, reason)


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


def _server_of(context: _Context, pane: str) -> str:
    """The tmux server ``pane`` lives on (see :class:`runners.LivePanes`), or "" if unknown."""
    if not pane:
        return ""
    live = context.deps.live_panes()
    return live.server if live is not None and pane in live.panes else ""


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
    # with attempts > 0 and waits CLAIM_TIMEOUT_SEC rather than launching
    # a second window straight away.
    claimed = ledger.update(
        run,
        expect={RunState.CLAIMED, RunState.BLOCKED},
        state=RunState.CLAIMED,
        attempts=run.attempts + 1,
        launched_at=runs.iso(ledger.now),
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
            _block(ledger, claimed, f"launch failed: {outcome.message}")
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
        tmux_server=_server_of(context, outcome.pane),
        payload_path=str(payload),
        reason=f"launched{where}",
    )
    if launched is not None:
        ledger.note(run.run_id, "dispatched", f"{outcome.window} {outcome.pane}{where}".strip())
        ledger.event(eventlog.EventKind.FLOW_DISPATCHED, launched, f"launched{where}")

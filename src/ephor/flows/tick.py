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
not start on a status the ticket has already left. A guard that says "not
now" parks the run as ``blocked``; it is tried again on every tick for as
long as the event that fired it is still the ticket's latest.

**A retry looks before it launches.** A claimed run with an attempt behind
it and no recorded window may still have one: the tick that opened it died,
or could not write ``launched``. Before trying again, the tick folds in an
exit file that attempt left, or adopts its window (found by the run id in
the pane's start command); only when tmux confirms there is none does it
open another.

Triggers: ``on_enter`` and ``on_exit`` are evaluated here. ``on_pr`` and
``on_session`` rules are validated but not yet evaluated — they are not
marked as seen either, so they will adopt the board when they are.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ephor import eventlog, jira_api, work_items
from ephor.board import Assignee, BoardConfig, FlowRule, Mode, Trigger
from ephor.constants import AgentStatus
from ephor.filelock import locked
from ephor.flows import runners, runs
from ephor.flows import snapshot as board_snapshot
from ephor.flows.runs import MAX_ATTEMPTS, Run, RunState
from ephor.flows.snapshot import (
    RefreshResult,
    RefreshStatus,
    Snapshot,
    TicketState,
    Transition,
)
from ephor.jira_api import Issue
from ephor.state.models import AgentState
from ephor.work_items import WorkItem

# The launch guard's memory: a session started, or a work record touched,
# this recently counts as "someone is already on this ticket".
LAUNCH_GUARD_SEC = 10 * 60.0
_HOUR = 3600.0
# Rules this tick evaluates. The others are accepted by the config but wait
# for their own phase; see the module docstring.
EVALUATED_TRIGGERS = frozenset({Trigger.ON_ENTER, Trigger.ON_EXIT})


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
    from ephor.jira import ticket_for_agent

    return (ticket_for_agent(agent) or "").upper()


def _recent_starts(since_sec: float) -> dict[str, str]:
    """ticket -> time of its newest SESSION_STARTED within ``since_sec``."""
    found: dict[str, str] = {}
    for event in eventlog.read(since_sec=since_sec):
        if event.kind is eventlog.EventKind.SESSION_STARTED and event.ticket:
            found[event.ticket.upper()] = event.at
    return found


def _work_item(key: str) -> WorkItem | None:
    return work_items.load(key)


def _live_panes() -> set[str] | None:
    return runners.live_panes()


def _flow_panes() -> dict[str, str] | None:
    return runners.flow_panes()


@dataclass(frozen=True)
class TickDeps:
    search: Callable[[str, tuple[str, ...]], list[Issue] | None] = _search
    myself: Callable[[], str | None] = _myself
    fetch_fresh: Callable[[str], Issue | None] = _fetch_fresh
    sessions: Callable[[], list[AgentState]] = _sessions
    ticket_of: Callable[[AgentState], str] = _ticket_of
    recent_starts: Callable[[float], dict[str, str]] = _recent_starts
    work_item: Callable[[str], WorkItem | None] = _work_item
    live_panes: Callable[[], set[str] | None] = _live_panes
    flow_panes: Callable[[], dict[str, str] | None] = _flow_panes
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
        rules = [r for r in config.rules if r.trigger in EVALUATED_TRIGGERS]
        if scope_changed and rules:
            notes.append("board scope changed — tickets new to the search were adopted")

    myself = snap.myself
    if not myself and any(r.assignee is Assignee.ME for r in rules):
        myself = deps.myself() or ""
        if not myself:
            # Unknown never means unmet: write nothing, so the moves this
            # tick saw are still moves when the next tick can match them.
            return TickResult(TickStatus.MYSELF_FAILED, snapshot=previous, dry_run=dry_run)
    snap = replace(snap, myself=myself)

    ledger = _Ledger(dry_run=dry_run, now=now)
    adopted: list[str] = []
    if config.dispatch_ok:
        stale = _stale_gap(previous, config, now)
        seen = set(previous.rules_seen) if previous is not None else set()
        for rule in rules:
            if previous is None or rule.fingerprint not in seen:
                adopted.append(rule.name)
                continue
            for move in transitions:
                if scope_changed and move.appeared:
                    continue
                record = snap.tickets.get(move.key)
                if record is not None and _matches(rule, move, record, myself):
                    _create_run(ledger, rule, move, record, stale=stale)
        snap = replace(snap, rules_seen=tuple(sorted({r.fingerprint for r in rules})))

    _tend(ledger, config, snap, deps)
    if config.dispatch_ok:
        if runs.is_paused():
            notes.insert(0, "paused (ephor flows resume)")
        _dispatch(ledger, config, snap, deps)
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
        actions=tuple(ledger.actions),
        adopted=tuple(adopted),
        dispatch_note=dispatch_note,
        dry_run=dry_run,
    )


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
        title=record.title,
        from_status=move.from_status,
        to_status=move.to_status,
        from_status_id=move.from_status_id,
        status_id=move.to_status_id,
        seq=move.seq,
    )
    if ledger.create(candidate):
        ledger.note(run_id, "created", f"{state}{' (stale)' if stale else ''} · {move.detail}")


def generation_current(run: Run, tickets: Mapping[str, TicketState]) -> bool:
    """True while the event that fired ``run`` is still the ticket's latest."""
    record = tickets.get(run.ticket)
    if record is None:
        return False
    if record.seq != run.seq:
        return False
    if run.trigger == str(Trigger.ON_ENTER):
        return record.status_identity == (run.status_id or run.to_status.strip().lower())
    return True


# --- tending runs that already exist ---------------------------------------------------


def _tend(ledger: _Ledger, config: BoardConfig, snap: Snapshot, deps: TickDeps) -> None:
    """Expire, supersede and finish runs, before anything new is dispatched."""
    panes: set[str] | None = None
    panes_read = False
    for run in sorted(ledger.runs.values(), key=lambda r: (r.created_at, r.run_id)):
        if run.state is RunState.AWAITING_CONFIRM:
            _tend_awaiting(ledger, config, snap, run)
        elif run.state in (RunState.CLAIMED, RunState.BLOCKED):
            if not generation_current(run, snap.tickets):
                _cancel(ledger, run, RunState.SUPERSEDED, _moved_reason(run, snap))
        elif run.state is RunState.LAUNCHED:
            if not panes_read:
                panes, panes_read = deps.live_panes(), True
            _tend_launched(ledger, run, panes)


def _moved_reason(run: Run, snap: Snapshot) -> str:
    record = snap.tickets.get(run.ticket)
    if record is None:
        return "ticket left the board"
    return f"ticket moved on (now {record.status or '?'})"


def _tend_awaiting(ledger: _Ledger, config: BoardConfig, snap: Snapshot, run: Run) -> None:
    age = runs.age_sec(run.created_at, ledger.now)
    if age is not None and age > config.flows.confirm_ttl:
        _cancel(ledger, run, RunState.EXPIRED, "not confirmed within confirm_ttl")
    elif not generation_current(run, snap.tickets):
        _cancel(ledger, run, RunState.EXPIRED, _moved_reason(run, snap))
    elif config.dispatch_ok and config.rule(run.flow) is None:
        _cancel(ledger, run, RunState.EXPIRED, "flow no longer in board.toml")


def _cancel(ledger: _Ledger, run: Run, state: RunState, reason: str) -> None:
    updated = ledger.update(run, expect={run.state}, state=state, reason=reason)
    if updated is not None:
        ledger.note(run.run_id, str(state), reason)
        ledger.event(eventlog.EventKind.FLOW_CANCELLED, updated, reason)


def _tend_launched(ledger: _Ledger, run: Run, panes: set[str] | None) -> None:
    code = runs.read_exit(run.run_id) if run.kind == "command" else None
    if code is not None:
        state = RunState.DONE if code == 0 else RunState.FAILED
        reason = f"exited {code}"
    elif panes is not None and run.pane and run.pane not in panes:
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
    _sessions: list[AgentState] | None = None
    _starts: dict[str, str] | None = None
    _flow_panes: dict[str, str] | None = None
    _flow_panes_read: bool = False

    def live_sessions(self) -> list[AgentState]:
        if self._sessions is None:
            try:
                self._sessions = [
                    a for a in self.deps.sessions() if a.status is not AgentStatus.DEAD
                ]
            except OSError:
                self._sessions = []
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


def _dispatch(ledger: _Ledger, config: BoardConfig, snap: Snapshot, deps: TickDeps) -> None:
    context = _Context(deps)
    ready = [r for r in ledger.runs.values() if r.state in (RunState.CLAIMED, RunState.BLOCKED)]
    for run in sorted(ready, key=lambda r: (r.created_at, r.run_id)):
        current = ledger.runs.get(run.run_id, run)
        if current.state not in (RunState.CLAIMED, RunState.BLOCKED):
            continue
        _dispatch_one(ledger, config, snap, context, current)


def _dispatch_one(
    ledger: _Ledger, config: BoardConfig, snap: Snapshot, context: _Context, run: Run
) -> None:
    rule = config.rule(run.flow)
    if rule is None:
        _cancel(ledger, run, RunState.SUPERSEDED, "flow no longer in board.toml")
        return

    if run.state is RunState.CLAIMED and run.attempts > 0:
        # A launch began and never reported a window: the tick that started
        # it died, or tmux did. Give it time to show up, then try again.
        waited = runs.age_sec(run.launched_at or run.updated_at, ledger.now)
        if waited is not None and waited < runs.CLAIM_TIMEOUT_SEC:
            ledger.note(run.run_id, "waiting", "a launch is in progress")
            return
    if _unrecorded_attempt(run) and _recover_attempt(ledger, context, run):
        return
    if run.attempts >= MAX_ATTEMPTS:
        _finish(ledger, run, RunState.STUCK, f"{run.attempts} launch attempts without a window")
        return

    reason = _guard(ledger, config, context, run, rule)
    if reason:
        _block(ledger, run, reason)
        return

    repo, how = config.resolve_repo(run.ticket, context.deps.work_item(run.ticket))
    if rule.launches_agent and repo is None:
        _block(ledger, run, how)
        return

    fresh = context.deps.fetch_fresh(run.ticket)
    if fresh is None:
        _block(ledger, run, "Jira re-check did not answer")
        return
    moved = _recheck(run, rule, fresh, snap.myself)
    if moved:
        _cancel(ledger, run, RunState.SUPERSEDED, moved)
        return

    _launch(ledger, config, snap, context, run, rule, repo=repo, repo_how=how)


# Why a retry is parked when tmux cannot say whether the last attempt's
# window exists. A run blocked for this reason is checked again, like a
# claimed one, before it may be relaunched.
_TMUX_UNKNOWN = "tmux did not answer — cannot tell whether the last attempt is running"


def _unrecorded_attempt(run: Run) -> bool:
    """True when an earlier attempt may have opened a window nobody recorded.

    A claimed run with attempts > 0 is one: the attempt was counted before
    its window was opened, and ``launched`` was never written. A blocked run
    is one only when it was parked because tmux could not be asked — every
    other block after an attempt follows a launch known to have failed.
    """
    if run.attempts <= 0:
        return False
    if run.state is RunState.CLAIMED:
        return True
    return run.state is RunState.BLOCKED and run.reason == _TMUX_UNKNOWN


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
        payload_path=str(payload) if payload.exists() else run.payload_path,
        reason=reason,
    )
    if adopted is not None:
        if run.kind == "start":
            context.launched.add(run.ticket)
        ledger.note(run.run_id, "dispatched", f"{pane} ({reason})")
        ledger.event(eventlog.EventKind.FLOW_DISPATCHED, adopted, reason)
    return True


def _guard(
    ledger: _Ledger, config: BoardConfig, context: _Context, run: Run, rule: FlowRule
) -> str:
    """The first guard that says "not now", or ""."""
    settings = config.flows
    if runs.is_paused():
        return "paused"
    hourly = _launches_within(ledger, _HOUR, exclude=run.run_id)
    if hourly >= settings.max_runs_per_hour:
        return f"max_runs_per_hour: {hourly} launches in the last hour"
    per_ticket = _launches_within(ledger, _HOUR, exclude=run.run_id, ticket=run.ticket)
    if per_ticket >= settings.ticket_breaker:
        return f"ticket_breaker: {per_ticket} launches for {run.ticket} in the last hour"
    if not rule.launches_agent:
        return ""
    # The session scan is from before this tick launched anything, so the
    # agents it has started since count on top of it.
    active = len(context.live_sessions()) + len(context.launched)
    if active >= settings.max_active_agents:
        return f"max_active_agents: {active} sessions running"
    return _launch_guard(ledger, context, run.ticket)


def _launch_guard(ledger: _Ledger, context: _Context, ticket: str) -> str:
    """Why an agent is (or may already be) on ``ticket``, or ""."""
    if ticket in context.launched:
        return f"launch guard: an agent was launched on {ticket} this tick"
    for agent in context.live_sessions():
        bound = (agent.ticket or "").upper() or context.deps.ticket_of(agent)
        if bound == ticket:
            return f"launch guard: session {agent.session_id[:8]} is live on {ticket}"
    started = context.recent_starts().get(ticket)
    if started:
        return f"launch guard: a session was started on {ticket} at {started}"
    item = context.deps.work_item(ticket)
    if item is not None:
        age = runs.age_sec(item.last_seen, ledger.now)
        if age is not None and age < LAUNCH_GUARD_SEC:
            return f"launch guard: {ticket}'s work record was updated {int(age // 60)}m ago"
        live_ids = {a.session_id for a in context.live_sessions()}
        if any(sid in live_ids for sid in item.session_ids):
            return f"launch guard: a live session is recorded on {ticket}"
    return ""


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


def _block(ledger: _Ledger, run: Run, reason: str) -> None:
    if run.state is RunState.BLOCKED and run.reason == reason:
        ledger.note(run.run_id, "blocked", reason)
        return
    updated = ledger.update(
        run, expect={RunState.CLAIMED, RunState.BLOCKED}, state=RunState.BLOCKED, reason=reason
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
        "pr": None,
        "session_id": "",
        "repo_path": str(repo) if repo is not None else "",
    }


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
    # earlier attempt left behind.
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
        payload_path=str(payload),
        reason=f"launched{where}",
    )
    if launched is not None:
        ledger.note(run.run_id, "dispatched", f"{outcome.window} {outcome.pane}{where}".strip())
        ledger.event(eventlog.EventKind.FLOW_DISPATCHED, launched, f"launched{where}")

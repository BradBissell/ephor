"""``on_pr`` and ``on_session``: the triggers that watch something other than a status.

A status trigger comes free with the board search: one JQL answers where
every ticket is, and the snapshot diff says what moved. These two triggers
need an observation of their own. Each is kept in the snapshot beside the
tickets, so the next tick can compare against it — the same "diff two
snapshots" model, applied to pull requests and to agent sessions — and each
event is named by what it is about (``pr<number>``, the session id), so a
rule fires on it once.

**on_pr** asks ``gh`` (:func:`ephor.github.find_prs_for_ticket`) which pull
requests name each ticket, in the ticket's own worktree or its project's
``[repos]`` checkout:

* ``opened`` — a PR is open now that the ticket's previous observation did
  not have open (new, or reopened);
* ``merged`` — a PR is merged now that was not merged before.

A ``gh`` call costs a second, so the lookups are spaced ``[flows]
pr_interval`` apart (``pr_checked_at``), and a ticket in a Done status is
asked about only while one of its PRs was last seen open — the merge still
to come. A ticket's first observation is a **baseline** and fires nothing:
its PRs were already there when ephor started looking. The opened/merged
pair is keyed on the PR *number*; a head-sha generation is reserved for a
later ``on_pr = "pushed"``.

**on_session** reads the session state files ephor already keeps
(:class:`ephor.state.manager.StateManager`), for sessions bound to a board
ticket:

* ``died`` — a session last seen alive is now DEAD (its agent process has
  exited), or its state file is gone and the process it recorded is no
  longer running. A session you kill from the dashboard counts: it died.
  An older session id of a process that is still running (``--resume``
  leaves one behind) did not die, and never fires.
* ``idle`` — a session has been IDLE, or waiting for your input, with no
  further hook event, for at least ``[flows] settle``. "Waiting for input"
  matters because Claude Code sends an idle_prompt Notification about a
  minute after a turn ends, which the hook records as WAITING_ANSWER with a
  question notification: a quiet Claude session is almost never plain IDLE.
  A session waiting on a permission prompt is blocked, not idle. It fires on
  the tick that sees it cross that line, and only for a session the previous
  tick saw on the near side of it: a session first seen already settled is
  a baseline, like a PR.

**Unknown never means unmet.** A ``gh`` that did not answer, a ticket with no
repo, a state directory that could not be read: each leaves that part of the
previous observation exactly as it was, and nothing fires from it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ephor.board import Assignee, FlowRule, Trigger
from ephor.constants import AgentStatus
from ephor.flows import runs, ticket_sort_key
from ephor.flows.snapshot import PrState, SessionSeen, Snapshot, TicketState
from ephor.github import PullRequest
from ephor.state.models import AgentState

PR_OPENED = "opened"
PR_MERGED = "merged"
SESSION_DIED = "died"
SESSION_IDLE = "idle"

FindPrs = Callable[[Path, str], list[PullRequest] | None]
RepoFor = Callable[[str], Path | None]
TicketOf = Callable[[AgentState], str]
PidAlive = Callable[[int], bool]


@dataclass(frozen=True)
class TriggerEvent:
    """One PR or session event, before any rule has looked at it."""

    key: str
    trigger: Trigger
    value: str  # opened | merged | died | idle
    generation: str  # pr<number> | session token
    detail: str
    pr: PrState | None = None
    session_id: str = ""


def assignee_ok(rule: FlowRule, record: TicketState, myself: str) -> bool:
    """The rule's assignee filter, as the status triggers apply it."""
    if rule.assignee is not Assignee.ME:
        return True
    return bool(myself) and record.assignee_id == myself


# --- pull requests -----------------------------------------------------------------


def pr_lookup_due(checked_at: str, interval: float, now: datetime) -> bool:
    """True when the last PR lookup was at least ``interval`` seconds ago.

    A missing or unreadable stamp is due. So is one in the future — a clock
    that jumped back must not silence on_pr until it catches up.
    """
    last = runs.parse_iso(checked_at)
    if last is None or last > now:
        return True
    return (now - last).total_seconds() >= interval


def pr_tickets(snap: Snapshot, rules: Iterable[FlowRule], myself: str) -> list[str]:
    """Board tickets an ``on_pr`` rule could fire on, in key order.

    On the board (the last search returned it), passing at least one rule's
    assignee filter, and — for a ticket whose status is in a Done category —
    with a PR last seen open: a week of finished tickets would otherwise
    cost a ``gh`` call each, every few minutes, to learn nothing.
    """
    wanted = [r for r in rules if r.trigger is Trigger.ON_PR]
    keys: list[str] = []
    for key, record in snap.tickets.items():
        if not record.on_board:
            continue
        if not any(assignee_ok(rule, record, myself) for rule in wanted):
            continue
        if record.is_done and not any(pr.is_open for pr in snap.prs.get(key, ())):
            continue
        keys.append(key)
    return sorted(keys, key=ticket_sort_key)


def _pr_state(pr: PullRequest) -> PrState:
    return PrState(number=pr.number, state=pr.state.upper(), head_sha=pr.head_sha, url=pr.url)


def observe_prs(
    previous: Mapping[str, tuple[PrState, ...]],
    keys: Iterable[str],
    *,
    tickets: Mapping[str, TicketState],
    repo_for: RepoFor,
    find: FindPrs,
) -> tuple[dict[str, tuple[PrState, ...]], list[TriggerEvent]]:
    """Look up ``keys``' PRs: the new observation, and the events it holds.

    Entries for tickets not looked up this round are dropped, so a ticket
    that returns (reassigned back, say) starts from a fresh baseline rather
    than firing for whatever happened while ephor was not looking. A ticket
    that was looked up but has no repo, or whose lookup did not answer,
    keeps its entry as it was: unknown is not unmet.
    """
    wanted = list(keys)
    keep = set(wanted)
    observed: dict[str, tuple[PrState, ...]] = {
        key: found for key, found in previous.items() if key in keep and key in tickets
    }
    events: list[TriggerEvent] = []
    for key in wanted:
        repo = repo_for(key)
        if repo is None:
            continue
        found: list[PullRequest] | None = None
        with contextlib.suppress(Exception):
            found = find(repo, key)
        if found is None:
            continue
        by_number = {pr.number: _pr_state(pr) for pr in found if pr.number > 0}
        now_prs = tuple(by_number[n] for n in sorted(by_number))
        observed[key] = now_prs
        before = previous.get(key)
        if before is not None:
            events.extend(pr_events(key, before, now_prs))
    return observed, events


def pr_events(key: str, before: Iterable[PrState], after: Iterable[PrState]) -> list[TriggerEvent]:
    """What changed between two observations of one ticket's PRs."""
    old = {pr.number: pr for pr in before}
    events: list[TriggerEvent] = []
    for pr in after:
        prior = old.get(pr.number)
        if pr.is_open and (prior is None or not prior.is_open):
            value = PR_OPENED
        elif pr.is_merged and (prior is None or not prior.is_merged):
            value = PR_MERGED
        else:
            continue
        events.append(
            TriggerEvent(
                key=key,
                trigger=Trigger.ON_PR,
                value=value,
                generation=runs.pr_generation(pr.number),
                detail=f"PR #{pr.number} {value}",
                pr=pr,
            )
        )
    return events


def pr_still_open(run_ticket: str, number: int, prs: Mapping[str, tuple[PrState, ...]]) -> bool:
    """False only when the last lookup saw PR ``number`` and it was no longer open."""
    for pr in prs.get(run_ticket, ()):
        if pr.number == number:
            return pr.is_open
    return True


# --- sessions ------------------------------------------------------------------------


def is_settled(agent: AgentState, settle: float, now: datetime) -> bool:
    """IDLE or waiting for input, with no hook event for at least ``settle`` seconds.

    A session's ``last_event_time`` moves with every hook event, so "no
    event since" is "continuously idle since". Claude's idle_prompt
    Notification lands about a minute after Stop, turning IDLE into
    WAITING_ANSWER (question) and moving the stamp once — so the settle is
    late by at most that minute, and needs no stamp of its own. A session
    waiting on a permission prompt is not idle. An unreadable stamp, or one
    in the future, is not settled.
    """
    if agent.status is AgentStatus.IDLE:
        pass
    elif agent.status is AgentStatus.WAITING_ANSWER and (
        agent.notification is None or agent.notification.type == "question"
    ):
        pass  # Claude's idle_prompt Notification (~60s after Stop) lands here
    else:
        return False
    moment = runs.parse_iso(agent.last_event_time)
    if moment is None or moment > now:
        return False
    return (now - moment).total_seconds() >= settle


def observe_sessions(
    previous: Mapping[str, SessionSeen],
    agents: Iterable[AgentState],
    *,
    ticket_of: TicketOf,
    pid_alive: PidAlive,
    settle: float,
    now: datetime,
) -> tuple[dict[str, SessionSeen], list[TriggerEvent]]:
    """Every ticket-bound session as seen now, and the died/idle events since last time."""
    seen: dict[str, SessionSeen] = {}
    events: list[TriggerEvent] = []
    dead = str(AgentStatus.DEAD)
    for agent in agents:
        sid = agent.session_id
        if not sid or sid in seen:
            continue
        old = previous.get(sid)
        ticket = (agent.ticket or "").strip().upper()
        if not ticket:
            with contextlib.suppress(Exception):
                ticket = (ticket_of(agent) or "").strip().upper()
        if not ticket and old is not None:
            ticket = old.ticket
        if not ticket:
            continue
        pid = agent.agent_pid if isinstance(agent.agent_pid, int) and agent.agent_pid > 0 else 0
        is_dead = agent.status is AgentStatus.DEAD
        settled = not is_dead and is_settled(agent, settle, now)
        seen[sid] = SessionSeen(ticket=ticket, status=str(agent.status), pid=pid, settled=settled)
        if old is None:
            continue  # a baseline: whatever it is doing, it was doing it before we looked
        if is_dead and old.status != dead:
            # The scan also marks an older session id of a still-running
            # process DEAD (resume residue). That process did not die.
            if pid and pid_alive(pid):
                continue
            events.append(_session_event(old.ticket or ticket, sid, SESSION_DIED))
        elif settled and not old.settled:
            events.append(_session_event(ticket, sid, SESSION_IDLE))
    for sid, old in previous.items():
        if sid in seen or old.status == dead:
            continue
        # The state file is gone. The reconciler removes a dead process's file
        # within a minute, so this is usually a death we did not see as DEAD;
        # with no pid recorded, or a pid still running, it is unknown.
        if old.pid and not pid_alive(old.pid):
            events.append(_session_event(old.ticket, sid, SESSION_DIED))
    return seen, events


def _session_event(ticket: str, session_id: str, value: str) -> TriggerEvent:
    return TriggerEvent(
        key=ticket,
        trigger=Trigger.ON_SESSION,
        value=value,
        generation=runs.session_generation(session_id),
        detail=f"session {session_id[:8]} {value}",
        session_id=session_id,
    )


def session_still_idle(session_id: str, sessions: Mapping[str, SessionSeen]) -> bool:
    """True while the last tick saw ``session_id`` settled-idle."""
    seen = sessions.get(session_id)
    return seen is not None and seen.settled

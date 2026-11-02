"""``on_pr`` and ``on_session``: the triggers that watch something other than a status.

A status trigger comes free with the board search: one JQL answers where
every ticket is, and the snapshot diff says what moved. These two triggers
need an observation of their own. Each is kept in the snapshot beside the
tickets, so the next tick can compare against it — the same "diff two
snapshots" model, applied to pull requests and to agent sessions — and each
event is named by what it is about (``pr<number>``, the session id), so a
rule fires on it once.

**on_pr** lists each repo's newest pull requests with one ``gh pr list``
(:func:`ephor.github.list_repo_prs`) — the repo being the ticket's own
worktree or its project's ``[repos]`` checkout — and matches the board's
tickets against that list locally:

* ``opened`` — a PR is open now that the ticket's previous observation did
  not have open. Its run is keyed on the PR number, so it fires once per
  PR: a PR closed and reopened later does not fire again;
* ``merged`` — a PR is merged now that the previous observation had
  **open**. A PR never seen open — one that was already merged the first
  time ephor saw it — is history, not an event.

A ``gh`` call costs a second, so the lookups are spaced ``[flows]
pr_interval`` apart (``pr_checked_at``), and a ticket in a Done status is
asked about only while one of its PRs was last seen open — the merge still
to come. A ticket's first observation is a **baseline** and fires nothing:
its PRs were already there when ephor started looking.

**The question includes the repo.** Each ticket's observation records the
repo it was read from (``pr_repos``). A ticket whose repo changes — its
worktree appeared, or was removed and the ``[repos]`` checkout answers
instead — is asked a different question, and the answer is a fresh
baseline, silently. Without that, every PR the new repo happens to hold
would read as just opened, or just merged, and ``mode = "auto"`` flows on
them would run unattended.

A repo listing that fills its limit has older PRs it did not return. A
ticket whose previously-seen PR has aged off it is asked about on its own
(:func:`ephor.github.find_prs_for_ticket`), and a PR that search turns up
for the first time is baselined rather than fired on: a fuzzy search
starting to match an old PR is not that PR opening. The opened/merged
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
  a baseline, like a PR. Its generation is the session *and* the start of
  the idle spell (the last hook event before it), so a session that works,
  goes quiet, works again and goes quiet again fires once per quiet spell.

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
from ephor.github import PullRequest, RepoPrs, ci_word
from ephor.state.models import AgentState

PR_OPENED = "opened"
PR_MERGED = "merged"
SESSION_DIED = "died"
SESSION_IDLE = "idle"

FindPrs = Callable[[Path, str], list[PullRequest] | None]
ListPrs = Callable[[Path], RepoPrs | None]
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
    idle_since: str = ""  # on_session = "idle": the spell's start


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
    return PrState(
        number=pr.number,
        state=pr.state.upper(),
        head_sha=pr.head_sha,
        url=pr.url,
        ci=ci_word(pr.ci),
    )


@dataclass(frozen=True)
class PrObservation:
    """One round of PR lookups: ticket -> PRs, ticket -> the repo they came from, and events."""

    prs: dict[str, tuple[PrState, ...]]
    repos: dict[str, str]
    events: list[TriggerEvent]


def observe_prs(
    previous: Mapping[str, tuple[PrState, ...]],
    keys: Iterable[str],
    *,
    tickets: Mapping[str, TicketState],
    repo_for: RepoFor,
    list_prs: ListPrs,
    find: FindPrs,
    previous_repos: Mapping[str, str] | None = None,
) -> PrObservation:
    """Look up ``keys``' PRs: the new observation, and the events it holds.

    One ``list_prs`` per distinct repo, matched per ticket locally. Entries
    for tickets not looked up this round are dropped, so a ticket that
    returns (reassigned back, say) starts from a fresh baseline rather than
    firing for whatever happened while ephor was not looking. A ticket that
    was looked up but has no repo, or whose repo's listing did not answer,
    keeps its entry (and its repo) as it was: unknown is not unmet. A
    ticket whose repo is not the one its previous entry was read from is
    re-baselined: the new answer is stored and nothing fires from it.
    """
    wanted = list(keys)
    keep = set(wanted)
    was_in = previous_repos or {}
    observed: dict[str, tuple[PrState, ...]] = {
        key: found for key, found in previous.items() if key in keep and key in tickets
    }
    repos: dict[str, str] = {key: was_in[key] for key in observed if key in was_in}
    events: list[TriggerEvent] = []
    groups: dict[str, tuple[Path, list[str]]] = {}
    for key in wanted:
        repo = None
        with contextlib.suppress(Exception):
            repo = repo_for(key)
        if repo is None:
            continue
        groups.setdefault(str(repo), (repo, []))[1].append(key)
    for where, (repo, group) in groups.items():
        listing: RepoPrs | None = None
        with contextlib.suppress(Exception):
            listing = list_prs(repo)
        if listing is None:
            continue
        for key in group:
            before = previous.get(key)
            found: list[PullRequest] | None = listing.naming(key)
            searched = False
            if not listing.complete and before:
                listed = {pr.number for pr in found or ()}
                if any(pr.number not in listed for pr in before):
                    # Aged off a full listing: ask about this ticket alone.
                    found, searched = None, True
                    with contextlib.suppress(Exception):
                        found = find(repo, key)
            if found is None:
                continue
            by_number = {pr.number: _pr_state(pr) for pr in found if pr.number > 0}
            now_prs = tuple(by_number[n] for n in sorted(by_number))
            observed[key] = now_prs
            repos[key] = where
            if before is not None and was_in.get(key) == where:
                events.extend(pr_events(key, before, now_prs, new_may_open=not searched))
    return PrObservation(prs=observed, repos=repos, events=events)


def pr_events(
    key: str,
    before: Iterable[PrState],
    after: Iterable[PrState],
    *,
    new_may_open: bool = True,
) -> list[TriggerEvent]:
    """What changed between two observations of one ticket's PRs, from the same repo.

    * a PR not in ``before`` fires ``opened`` only if it is open (and only
      when ``new_may_open``: a per-ticket search that newly matches an old
      PR is not that PR opening) — never ``merged``: a PR first seen merged
      was merged before ephor was looking;
    * a PR seen before fires ``opened`` when it is open and was not, and
      ``merged`` only when it was last seen open.
    """
    old = {pr.number: pr for pr in before}
    events: list[TriggerEvent] = []
    for pr in after:
        prior = old.get(pr.number)
        if prior is None:
            if not (pr.is_open and new_may_open):
                continue
            value = PR_OPENED
        elif pr.is_open and not prior.is_open:
            value = PR_OPENED
        elif pr.is_merged and prior.is_open:
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
        # While settled, no hook event has moved last_event_time: it is the
        # moment this idle spell began, and the same on every tick of it.
        idle_since = agent.last_event_time if settled else ""
        seen[sid] = SessionSeen(
            ticket=ticket,
            status=str(agent.status),
            pid=pid,
            settled=settled,
            idle_since=idle_since,
        )
        if old is None:
            continue  # a baseline: whatever it is doing, it was doing it before we looked
        if is_dead and old.status != dead:
            # The scan also marks an older session id of a still-running
            # process DEAD (resume residue). That process did not die.
            if pid and pid_alive(pid):
                continue
            events.append(_session_event(old.ticket or ticket, sid, SESSION_DIED))
        elif settled and (
            not old.settled or (bool(old.idle_since) and old.idle_since != idle_since)
        ):
            # A new spell: it was busy last tick, or it woke and settled again
            # in between (a settle shorter than the tick spacing).
            events.append(_session_event(ticket, sid, SESSION_IDLE, idle_since=idle_since))
    for sid, old in previous.items():
        if sid in seen or old.status == dead:
            continue
        # The state file is gone. The reconciler removes a dead process's file
        # within a minute, so this is usually a death we did not see as DEAD;
        # with no pid recorded, or a pid still running, it is unknown.
        if old.pid and not pid_alive(old.pid):
            events.append(_session_event(old.ticket, sid, SESSION_DIED))
    return seen, events


def _session_event(
    ticket: str, session_id: str, value: str, *, idle_since: str = ""
) -> TriggerEvent:
    generation = runs.session_generation(session_id)
    if idle_since:
        generation = runs.idle_generation(session_id, idle_since)
    return TriggerEvent(
        key=ticket,
        trigger=Trigger.ON_SESSION,
        value=value,
        generation=generation,
        detail=f"session {session_id[:8]} {value}",
        session_id=session_id,
        idle_since=idle_since,
    )


def session_still_idle(
    session_id: str, sessions: Mapping[str, SessionSeen], idle_since: str = ""
) -> bool:
    """True while the last tick saw ``session_id`` settled-idle — in the same spell, if named."""
    seen = sessions.get(session_id)
    if seen is None or not seen.settled:
        return False
    return not idle_since or not seen.idle_since or seen.idle_since == idle_since

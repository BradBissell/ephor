"""``ephor.flows.triggers``: the PR and session observations, without a tick around them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ephor import board
from ephor.board import Trigger
from ephor.flows import runs, triggers
from ephor.flows.snapshot import PrState, SessionSeen, Snapshot, TicketState
from ephor.github import PullRequest
from ephor.state.models import AgentState, Notification

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _gh(number: int, state: str = "OPEN") -> PullRequest:
    return PullRequest(number=number, url=f"u{number}", state=state, head_sha=f"sha{number}")


# --- generations -------------------------------------------------------------------


def test_generations_are_filename_safe_run_id_parts() -> None:
    assert runs.pr_generation(12) == "pr12"
    uuid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    assert runs.session_generation(uuid) == "0f1e2d3c-4b5"
    odd = runs.session_generation("a/b c:d.e")
    assert odd.startswith("s") and len(odd) == 12
    assert odd == runs.session_generation("a/b c:d.e")
    assert runs.session_generation("") != runs.session_generation("x")
    for generation in ("pr12", runs.session_generation(uuid), odd):
        assert runs.valid_run_id(runs.make_run_id("DR-1", "flow", generation))


# --- pull requests ------------------------------------------------------------------


def test_pr_lookup_is_due_after_the_interval_or_with_a_bad_stamp() -> None:
    stamp = runs.iso(T0)
    assert not triggers.pr_lookup_due(stamp, 300, T0 + timedelta(seconds=299))
    assert triggers.pr_lookup_due(stamp, 300, T0 + timedelta(seconds=300))
    assert triggers.pr_lookup_due("", 300, T0)
    assert triggers.pr_lookup_due("garbage", 300, T0)
    # A clock that jumped back does not silence on_pr until it catches up.
    assert triggers.pr_lookup_due(runs.iso(T0 + timedelta(hours=1)), 300, T0)


def test_pr_events_name_new_opens_reopens_and_merges() -> None:
    before = (PrState(1, "OPEN"), PrState(2, "CLOSED"), PrState(3, "OPEN"), PrState(4, "MERGED"))
    after = (
        PrState(1, "OPEN"),  # unchanged
        PrState(2, "OPEN"),  # reopened
        PrState(3, "MERGED"),  # merged
        PrState(4, "MERGED"),  # still merged
        PrState(5, "OPEN"),  # new
        PrState(6, "MERGED"),  # opened and merged between two lookups
        PrState(7, "CLOSED"),  # opened and abandoned between two lookups
    )
    found = [(e.generation, e.value) for e in triggers.pr_events("DR-1", before, after)]
    assert found == [("pr2", "opened"), ("pr3", "merged"), ("pr5", "opened"), ("pr6", "merged")]
    assert all(e.trigger is Trigger.ON_PR for e in triggers.pr_events("DR-1", before, after))


def test_observe_prs_baselines_keeps_unknowns_and_drops_departed_tickets(tmp_path: Path) -> None:
    tickets = {k: TicketState(status="In Progress") for k in ("DR-1", "DR-2", "DR-3", "DR-4")}
    previous = {
        "DR-1": (PrState(1, "OPEN"),),
        "DR-2": (PrState(2, "OPEN"),),
        "DR-9": (PrState(9, "OPEN"),),  # no longer in the snapshot
    }
    answers: dict[str, list[PullRequest] | None] = {
        "DR-1": [_gh(1, "MERGED")],
        "DR-2": None,  # gh did not answer
        "DR-3": [_gh(3)],  # first look: baseline
    }

    def find(repo: Path, key: str) -> list[PullRequest] | None:
        if key == "DR-4":
            raise RuntimeError("boom")
        return answers[key]

    observed, events = triggers.observe_prs(
        previous,
        ["DR-1", "DR-2", "DR-3", "DR-4"],
        tickets=tickets,
        repo_for=lambda key: tmp_path,
        find=find,
    )
    assert observed == {
        "DR-1": (PrState(1, "MERGED", "sha1", "u1"),),
        "DR-2": (PrState(2, "OPEN"),),
        "DR-3": (PrState(3, "OPEN", "sha3", "u3"),),
    }
    assert [(e.key, e.value) for e in events] == [("DR-1", "merged")]


def test_a_ticket_that_leaves_the_lookup_is_rebaselined_when_it_returns(tmp_path: Path) -> None:
    # DR-5 is eligible and has no PRs; it is reassigned away (no longer in
    # keys) while a teammate opens and merges #40; then it comes back. The
    # merge happened while ephor was not looking: a baseline, not an event.
    tickets = {"DR-5": TicketState(status="In Progress"), "DR-6": TicketState()}
    answers: dict[str, list[PullRequest] | None] = {"DR-5": [], "DR-6": []}

    def observe(
        previous: dict[str, tuple[PrState, ...]], keys: list[str]
    ) -> tuple[dict[str, tuple[PrState, ...]], list[triggers.TriggerEvent]]:
        return triggers.observe_prs(
            previous,
            iter(keys),  # an iterable consumed once, as the tick passes it
            tickets=tickets,
            repo_for=lambda key: tmp_path,
            find=lambda repo, key: answers[key],
        )

    observed, events = observe({}, ["DR-5", "DR-6"])
    assert observed == {"DR-5": (), "DR-6": ()} and events == []
    observed, events = observe(observed, ["DR-6"])
    assert "DR-5" not in observed and events == []
    answers["DR-5"] = [_gh(40, "MERGED")]
    observed, events = observe(observed, ["DR-5", "DR-6"])
    assert events == []
    assert observed["DR-5"] == (PrState(40, "MERGED", "sha40", "u40"),)


def test_observe_prs_skips_tickets_with_no_repo() -> None:
    calls: list[str] = []
    observed, events = triggers.observe_prs(
        {},
        ["DR-1"],
        tickets={"DR-1": TicketState()},
        repo_for=lambda key: None,
        find=lambda repo, key: calls.append(key) or [],
    )
    assert (observed, events, calls) == ({}, [], [])


def _rule(**extra: object) -> board.FlowRule:
    config = board.parse(
        {
            "board": {"projects": ["DR"]},
            "flow": [{"name": "x", "on_pr": "opened", "command": "x.sh", **extra}],
        }
    )
    assert config.errors == ()
    return config.rules[0]


def test_pr_tickets_filters_by_board_assignee_and_done() -> None:
    snap = Snapshot(
        taken_at="",
        tickets={
            "DR-10": TicketState(status="In Progress", assignee_id="me"),
            "DR-9": TicketState(status="In Progress", assignee_id="me"),
            "DR-2": TicketState(status="In Progress", assignee_id="other"),
            "DR-3": TicketState(status="In Progress", assignee_id="me", unseen_ticks=2),
            "DR-4": TicketState(status="Done", status_category="done", assignee_id="me"),
            "DR-5": TicketState(status="Done", status_category="done", assignee_id="me"),
        },
        prs={"DR-5": (PrState(5, "OPEN"),), "DR-4": (PrState(4, "MERGED"),)},
    )
    assert triggers.pr_tickets(snap, [_rule()], "me") == ["DR-5", "DR-9", "DR-10"]
    assert triggers.pr_tickets(snap, [_rule(assignee="any")], "me") == [
        "DR-2",
        "DR-5",
        "DR-9",
        "DR-10",
    ]
    # assignee = "me" with no known account id matches nothing.
    assert triggers.pr_tickets(snap, [_rule()], "") == []


def test_pr_still_open_is_unknown_when_unseen() -> None:
    prs = {"DR-1": (PrState(1, "OPEN"), PrState(2, "CLOSED"))}
    assert triggers.pr_still_open("DR-1", 1, prs)
    assert not triggers.pr_still_open("DR-1", 2, prs)
    assert triggers.pr_still_open("DR-1", 3, prs)
    assert triggers.pr_still_open("DR-2", 1, prs)


# --- sessions ---------------------------------------------------------------------------


def _agent(
    sid: str,
    status: str = "IDLE",
    *,
    ticket: str = "DR-1",
    pid: int | None = 100,
    last: datetime = T0,
) -> AgentState:
    return AgentState.from_dict(
        {
            "session_id": sid,
            "cwd": "/x",
            "started_at": "2026-01-01T00:00:00+00:00",
            "status": status,
            "ticket": ticket,
            "agent_pid": pid,
            "last_event_time": runs.iso(last),
            "schema_version": 4,
        }
    )


def test_is_settled_needs_idle_with_no_event_for_settle() -> None:
    assert triggers.is_settled(_agent("a", last=T0), 600, T0 + timedelta(minutes=10))
    assert not triggers.is_settled(_agent("a", last=T0), 600, T0 + timedelta(minutes=9))
    assert not triggers.is_settled(_agent("a", "WORKING"), 600, T0 + timedelta(hours=1))
    assert not triggers.is_settled(_agent("a", "WAITING_PERMISSION"), 600, T0 + timedelta(hours=1))
    # A stamp from the future is not "idle for a long time".
    assert not triggers.is_settled(_agent("a", last=T0 + timedelta(days=1)), 600, T0)
    broken = _agent("a")
    broken.last_event_time = "yesterday"
    assert not triggers.is_settled(broken, 600, T0)


def test_waiting_for_input_counts_as_idle_but_a_permission_prompt_does_not() -> None:
    # What the real hook leaves behind for a quiet Claude session: Stop sets
    # IDLE, then ~60s later the idle_prompt Notification ("Claude is waiting
    # for your input") sets WAITING_ANSWER with a question notification and
    # moves last_event_time.
    stopped = T0
    nudged = stopped + timedelta(seconds=60)
    quiet = AgentState.from_dict(
        {
            "session_id": "a",
            "cwd": "/x",
            "started_at": "2026-01-01T00:00:00+00:00",
            "status": "WAITING_ANSWER",
            "notification": {"type": "question", "tool": None, "redacted_summary": None},
            "ticket": "DR-1",
            "agent_pid": 100,
            "last_event_time": runs.iso(nudged),
            "schema_version": 4,
        }
    )
    assert not triggers.is_settled(quiet, 600, nudged + timedelta(minutes=9))
    assert triggers.is_settled(quiet, 600, nudged + timedelta(minutes=10))
    # With no notification recorded it is still waiting for an answer.
    bare = _agent("a", "WAITING_ANSWER", last=nudged)
    assert triggers.is_settled(bare, 600, nudged + timedelta(minutes=10))
    # A WAITING_ANSWER that is really about a permission is not idle.
    asking = _agent("a", "WAITING_ANSWER", last=nudged)
    asking.notification = Notification(type="permission", tool="Bash")
    assert not triggers.is_settled(asking, 600, nudged + timedelta(hours=1))

    # And through observe_sessions: the tick before settle, then the one after.
    seen, events = _observe({}, [quiet], now=nudged + timedelta(minutes=5))
    assert events == [] and not seen["a"].settled
    _, events = _observe(seen, [quiet], now=nudged + timedelta(minutes=11))
    assert events == [("DR-1", "a", "idle")]


def _observe(
    previous: dict[str, SessionSeen],
    agents: list[AgentState],
    *,
    alive: set[int] | None = None,
    now: datetime = T0,
    ticket_of: triggers.TicketOf = lambda agent: "",
) -> tuple[dict[str, SessionSeen], list[tuple[str, str, str]]]:
    live = alive or set()
    seen, events = triggers.observe_sessions(
        previous,
        agents,
        ticket_of=ticket_of,
        pid_alive=lambda pid: pid in live,
        settle=600,
        now=now,
    )
    return seen, [(e.key, e.session_id, e.value) for e in events]


def test_a_session_first_seen_is_a_baseline_whatever_it_is_doing() -> None:
    seen, events = _observe({}, [_agent("a", "DEAD"), _agent("b", last=T0 - timedelta(hours=1))])
    assert events == []
    assert seen["a"] == SessionSeen("DR-1", "DEAD", 100, False)
    assert seen["b"].settled


def test_died_and_idle_fire_on_the_crossing_only() -> None:
    previous = {
        "a": SessionSeen("DR-1", "WORKING", 100),
        "b": SessionSeen("DR-2", "IDLE", 200, settled=False),
        "c": SessionSeen("DR-3", "IDLE", 300, settled=True),
        "d": SessionSeen("DR-4", "DEAD", 400),
    }
    agents = [
        _agent("a", "DEAD"),
        _agent("b", ticket="DR-2", pid=200, last=T0 - timedelta(minutes=11)),
        _agent("c", ticket="DR-3", pid=300, last=T0 - timedelta(hours=2)),
        _agent("d", "DEAD", ticket="DR-4", pid=400),
    ]
    _, events = _observe(previous, agents, alive={200, 300})
    assert events == [("DR-1", "a", "died"), ("DR-2", "b", "idle")]


def test_resume_residue_is_not_a_death_and_is_remembered_as_dead() -> None:
    previous = {"a": SessionSeen("DR-1", "IDLE", 100)}
    seen, events = _observe(previous, [_agent("a", "DEAD")], alive={100})
    assert events == []
    assert seen["a"].status == "DEAD"
    # When the process does exit later, the file is already "dead": no event.
    _, events = _observe(seen, [], alive=set())
    assert events == []


def test_a_vanished_file_is_a_death_only_when_its_process_is_gone() -> None:
    previous = {
        "gone": SessionSeen("DR-1", "WORKING", 100),
        "running": SessionSeen("DR-2", "IDLE", 200),
        "no-pid": SessionSeen("DR-3", "IDLE", 0),
        "was-dead": SessionSeen("DR-4", "DEAD", 400),
    }
    seen, events = _observe(previous, [], alive={200})
    assert events == [("DR-1", "gone", "died")]
    assert seen == {}


def test_the_ticket_comes_from_the_session_then_the_resolver_then_memory() -> None:
    previous = {"c": SessionSeen("DR-7", "WORKING", 300)}
    agents = [
        _agent("a", ticket="dr-1"),
        _agent("b", ticket="", pid=200),
        _agent("c", "DEAD", ticket="", pid=300),
        _agent("d", ticket="", pid=400),
    ]
    seen, events = _observe(
        previous, agents, ticket_of=lambda agent: "DR-2" if agent.session_id == "b" else ""
    )
    assert {sid: s.ticket for sid, s in seen.items()} == {"a": "DR-1", "b": "DR-2", "c": "DR-7"}
    assert events == [("DR-7", "c", "died")]


def test_a_resolver_that_raises_is_no_ticket() -> None:
    def boom(agent: AgentState) -> str:
        raise RuntimeError("git went away")

    seen, events = _observe({}, [_agent("a", ticket="")], ticket_of=boom)
    assert (seen, events) == ({}, [])


@pytest.mark.parametrize(("settled", "expected"), [(True, True), (False, False)])
def test_session_still_idle(settled: bool, expected: bool) -> None:
    sessions = {"a": SessionSeen("DR-1", "IDLE", 1, settled=settled)}
    assert triggers.session_still_idle("a", sessions) is expected
    assert not triggers.session_still_idle("b", sessions)

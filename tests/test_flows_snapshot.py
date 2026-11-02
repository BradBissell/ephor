"""flows/snapshot.json: the board search, the diff, and the rule-less tick."""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ephor import board, eventlog
from ephor.flows import printable, ticket_sort_key
from ephor.flows import snapshot as snap
from ephor.flows.snapshot import (
    DROP_AFTER_UNSEEN_SEC,
    PrState,
    SessionSeen,
    Snapshot,
    TicketState,
)
from ephor.jira_api import Issue

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _issue(key: str, status: str, status_id: str, **extra: Any) -> Issue:
    return Issue(key=key, status=status, status_id=status_id, **extra)


def _config(**board_table: Any) -> board.BoardConfig:
    table: dict[str, Any] = {"projects": ["DR"], **board_table}
    return board.parse({"board": table})


@pytest.fixture
def jira_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPHOR_JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("EPHOR_JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("EPHOR_JIRA_TOKEN", "t")


# --- advance (the pure diff) ------------------------------------------------


def test_the_first_snapshot_is_a_baseline_with_no_transitions() -> None:
    new, moves = snap.advance(None, [_issue("DR-1", "To Do", "1")], now=T0)
    assert moves == []
    assert new.tickets["DR-1"].seq == 0
    assert new.taken_at == "2026-10-01T12:00:00+00:00"


def test_a_status_change_bumps_seq_and_is_reported() -> None:
    first, _ = snap.advance(None, [_issue("DR-1", "To Do", "1", title="t")], now=T0)
    second, moves = snap.advance(first, [_issue("DR-1", "In Progress", "3", title="t")], now=T0)
    assert second.tickets["DR-1"].seq == 1
    assert len(moves) == 1
    move = moves[0]
    assert (move.key, move.from_status, move.to_status, move.seq) == (
        "DR-1",
        "To Do",
        "In Progress",
        1,
    )
    assert (move.from_status_id, move.to_status_id) == ("1", "3")
    assert not move.appeared


def test_the_same_visit_keeps_its_seq_and_reports_nothing() -> None:
    first, _ = snap.advance(None, [_issue("DR-1", "To Do", "1")], now=T0)
    moved, _ = snap.advance(first, [_issue("DR-1", "In Progress", "3")], now=T0)
    again, moves = snap.advance(moved, [_issue("DR-1", "In Progress", "3", title="new")], now=T0)
    assert moves == []
    assert again.tickets["DR-1"].seq == 1
    assert again.tickets["DR-1"].title == "new"  # fields still refresh


def test_leaving_and_returning_to_a_status_is_a_new_visit() -> None:
    a, _ = snap.advance(None, [_issue("DR-1", "Peer Review", "5")], now=T0)
    b, _ = snap.advance(a, [_issue("DR-1", "In Progress", "3")], now=T0)
    c, moves = snap.advance(b, [_issue("DR-1", "Peer Review", "5")], now=T0)
    assert c.tickets["DR-1"].seq == 2
    assert moves[0].to_status_id == "5"


def test_a_renamed_status_with_the_same_id_is_not_a_move() -> None:
    a, _ = snap.advance(None, [_issue("DR-1", "Review", "5")], now=T0)
    _, moves = snap.advance(a, [_issue("DR-1", "Peer Review", "5")], now=T0)
    assert moves == []


def test_without_ids_the_status_name_decides() -> None:
    a, _ = snap.advance(None, [_issue("DR-1", "To Do", "")], now=T0)
    _, same = snap.advance(a, [_issue("DR-1", "to do", "")], now=T0)
    _, moved = snap.advance(a, [_issue("DR-1", "Done", "")], now=T0)
    assert same == []
    assert len(moved) == 1


def test_a_ticket_new_to_the_board_is_reported_as_appearing() -> None:
    a, _ = snap.advance(None, [], now=T0)
    b, moves = snap.advance(a, [_issue("DR-2", "Peer Review", "5")], now=T0)
    assert len(moves) == 1
    assert moves[0].appeared
    assert moves[0].from_status == ""
    assert "new on board" in moves[0].detail
    assert b.tickets["DR-2"].seq == 0


def test_a_status_jira_returned_blank_is_unknown_not_a_move() -> None:
    """Unknown never means unmet: a missing status is not a visit to ''."""
    a, _ = snap.advance(None, [_issue("DR-1", "Peer Review", "3")], now=T0)
    b, blank = snap.advance(a, [_issue("DR-1", "", "")], now=T0)
    c, back = snap.advance(b, [_issue("DR-1", "Peer Review", "3")], now=T0)
    assert blank == [] and back == []
    assert [s.tickets["DR-1"].seq for s in (a, b, c)] == [0, 0, 0]
    assert b.tickets["DR-1"].status == "Peer Review"  # the old state is kept
    assert b.tickets["DR-1"].unseen_ticks == 1  # like a ticket the search missed
    assert c.tickets["DR-1"].on_board


def test_a_new_ticket_with_no_status_waits_for_one() -> None:
    a, _ = snap.advance(None, [], now=T0)
    b, moves = snap.advance(a, [_issue("DR-2", "", "")], now=T0)
    assert moves == [] and "DR-2" not in b.tickets
    _, moves = snap.advance(b, [_issue("DR-2", "To Do", "1")], now=T0)
    assert len(moves) == 1 and moves[0].appeared


def test_appearing_is_decided_by_the_previous_snapshot_not_a_blank_status() -> None:
    """A stored ticket with no status that gains one moved; it did not appear."""
    a = Snapshot(taken_at="", tickets={"DR-1": TicketState(seq=4)})
    _, moves = snap.advance(a, [_issue("DR-1", "To Do", "1")], now=T0)
    assert len(moves) == 1
    assert not moves[0].appeared
    assert moves[0].seq == 5
    assert "new on board" not in moves[0].detail


def test_absence_is_not_movement() -> None:
    a, _ = snap.advance(None, [_issue("DR-1", "In Progress", "3")], now=T0)
    b, moves = snap.advance(a, [], now=T0)
    assert moves == []
    kept = b.tickets["DR-1"]
    assert kept.unseen_ticks == 1
    assert kept.status == "In Progress"
    assert not kept.on_board
    assert b.on_board() == {}


def test_a_returning_ticket_resets_unseen_and_compares_to_its_last_status() -> None:
    a, _ = snap.advance(None, [_issue("DR-1", "In Progress", "3")], now=T0)
    b, _ = snap.advance(a, [], now=T0)
    c, moves = snap.advance(b, [_issue("DR-1", "Peer Review", "5")], now=T0)
    assert c.tickets["DR-1"].unseen_ticks == 0
    assert [m.from_status for m in moves] == ["In Progress"]


def test_a_ticket_unseen_for_too_long_is_dropped() -> None:
    """Measured in time, not searches: a thousand quick ticks drop nothing."""
    current, _ = snap.advance(None, [_issue("DR-1", "In Progress", "3")], now=T0)
    for _ in range(1000):
        current, _ = snap.advance(current, [], now=T0)
    assert current.tickets["DR-1"].unseen_ticks == 1000
    assert current.tickets["DR-1"].unseen_since == "2026-10-01T12:00:00+00:00"
    current, _ = snap.advance(current, [], now=T0 + timedelta(seconds=DROP_AFTER_UNSEEN_SEC - 1))
    assert "DR-1" in current.tickets
    current, _ = snap.advance(current, [], now=T0 + timedelta(seconds=DROP_AFTER_UNSEEN_SEC + 1))
    assert "DR-1" not in current.tickets


def test_a_ticket_seen_again_restarts_its_unseen_clock() -> None:
    current, _ = snap.advance(None, [_issue("DR-1", "In Progress", "3")], now=T0)
    current, _ = snap.advance(current, [], now=T0 + timedelta(minutes=50))
    current, _ = snap.advance(
        current, [_issue("DR-1", "In Progress", "3")], now=T0 + timedelta(minutes=55)
    )
    assert current.tickets["DR-1"].unseen_since == ""
    current, _ = snap.advance(current, [], now=T0 + timedelta(minutes=70))
    current, _ = snap.advance(current, [], now=T0 + timedelta(minutes=100))
    assert "DR-1" in current.tickets


def _drop(current: Snapshot, *, now: datetime = T0) -> Snapshot:
    current, _ = snap.advance(current, [], now=now)
    current, _ = snap.advance(current, [], now=now + timedelta(seconds=DROP_AFTER_UNSEEN_SEC + 1))
    return current


def test_a_dropped_ticket_returning_elsewhere_gets_a_new_seq() -> None:
    first, _ = snap.advance(None, [_issue("DR-1", "To Do", "1")], now=T0)
    second, _ = snap.advance(first, [_issue("DR-1", "Peer Review", "10")], now=T0)
    gone = _drop(second)
    assert "DR-1" not in gone.tickets
    assert gone.dropped["DR-1"].seq == 1
    assert gone.dropped["DR-1"].dropped_at == "2026-10-01T13:00:01+00:00"
    assert (gone.dropped["DR-1"].status, gone.dropped["DR-1"].status_id) == ("Peer Review", "10")
    back, moves = snap.advance(gone, [_issue("DR-1", "QA", "11")], now=T0)
    assert back.tickets["DR-1"].seq == 2
    assert [(m.appeared, m.seq) for m in moves] == [(True, 2)]
    assert "DR-1" not in back.dropped


def test_a_dropped_ticket_returning_in_the_same_status_resumes_its_visit() -> None:
    first, _ = snap.advance(None, [_issue("DR-1", "To Do", "1")], now=T0)
    second, _ = snap.advance(first, [_issue("DR-1", "Done", "30")], now=T0)
    gone = _drop(second)
    back, moves = snap.advance(gone, [_issue("DR-1", "Done", "30")], now=T0)
    assert moves == []
    assert back.tickets["DR-1"].seq == 1
    assert "DR-1" not in back.dropped


def test_a_tombstone_without_a_status_still_counts_a_return_as_a_visit() -> None:
    """Tombstones written before the status was kept read as they always did."""
    first, _ = snap.advance(None, [_issue("DR-1", "Done", "30")], now=T0)
    old_style = replace(
        first,
        tickets={},
        dropped={"DR-1": snap.DroppedTicket.from_dict({"seq": 4, "dropped_at": "x"})},
    )
    back, moves = snap.advance(old_style, [_issue("DR-1", "Done", "30")], now=T0)
    assert back.tickets["DR-1"].seq == 5
    assert [(m.appeared, m.seq) for m in moves] == [(True, 5)]


def test_old_tombstones_are_pruned() -> None:
    first, _ = snap.advance(None, [_issue("DR-1", "To Do", "1")], now=T0)
    gone = _drop(first)
    later = T0 + timedelta(days=snap.TOMBSTONE_KEEP_DAYS - 1)
    kept, _ = snap.advance(gone, [], now=later)
    assert "DR-1" in kept.dropped
    pruned, _ = snap.advance(kept, [], now=T0 + timedelta(days=snap.TOMBSTONE_KEEP_DAYS + 1))
    assert pruned.dropped == {}


def test_a_baseline_starts_each_ticket_at_its_seq_floor() -> None:
    new, moves = snap.advance(
        None,
        [_issue("DR-1", "To Do", "1"), _issue("DR-2", "To Do", "1")],
        now=T0,
        seq_floor={"DR-1": 4},
    )
    assert moves == []
    assert (new.tickets["DR-1"].seq, new.tickets["DR-2"].seq) == (4, 0)
    # Only a baseline is seeded: with a previous snapshot, the floor is ignored.
    again, _ = snap.advance(new, [_issue("DR-3", "To Do", "1")], now=T0, seq_floor={"DR-3": 9})
    assert again.tickets["DR-3"].seq == 0


def test_dropped_and_scope_round_trip() -> None:
    stored = Snapshot(
        taken_at="2026-01-01T00:00:00+00:00",
        dropped={"DR-1": snap.DroppedTicket(seq=3, dropped_at="2026-01-01T00:00:00+00:00")},
        scope="abc123",
    )
    assert snap.save(stored)
    loaded = snap.load()
    assert loaded is not None
    assert loaded.dropped == stored.dropped
    assert loaded.scope == "abc123"
    # A snapshot written before these fields existed reads them as empty.
    old = Snapshot.from_dict({"taken_at": "2026-01-01T00:00:00+00:00", "dropped": {"x": 1}})
    assert (old.dropped, old.scope) == ({}, "")


def test_board_scope_follows_the_search() -> None:
    one = snap.board_scope(_config().board)
    assert one == snap.board_scope(_config().board)
    assert len(one) == 12
    assert one != snap.board_scope(_config(projects=["DR", "OPS"]).board)
    assert one != snap.board_scope(_config(jql="assignee = currentUser()").board)


def test_duplicate_and_lowercase_keys_are_folded() -> None:
    new, _ = snap.advance(None, [_issue("dr-1", "To Do", "1"), _issue("DR-1", "Done", "9")], now=T0)
    assert list(new.tickets) == ["DR-1"]
    assert new.tickets["DR-1"].status == "To Do"


def test_fields_the_tick_owns_are_carried_forward() -> None:
    previous = Snapshot(
        taken_at="2026-01-01T00:00:00+00:00",
        rules_seen=("abc",),
        myself="acct-1",
        pr_checked_at="2026-01-01T00:00:00+00:00",
        prs={"DR-1": (PrState(number=7, state="OPEN", head_sha="deadbeef", url="u"),)},
        sessions_seen={"s1": SessionSeen(ticket="DR-1", status="IDLE", pid=9, settled=True)},
    )
    new, _ = snap.advance(previous, [], now=T0)
    assert new.rules_seen == ("abc",)
    assert new.myself == "acct-1"
    assert new.prs["DR-1"][0].number == 7
    assert new.sessions_seen == previous.sessions_seen
    assert new.taken_at != previous.taken_at


def test_age_and_done() -> None:
    new, _ = snap.advance(None, [], now=T0)
    assert new.age_sec(T0 + timedelta(minutes=5)) == 300
    assert Snapshot(taken_at="").age_sec() is None
    assert TicketState(status="Closed").is_done
    assert TicketState(status="Whatever", status_category="done").is_done
    assert not TicketState(status="Done", status_category="indeterminate").is_done


# --- persistence --------------------------------------------------------------


def test_save_and_load_round_trip_with_private_permissions() -> None:
    new, _ = snap.advance(
        None,
        [_issue("DR-1", "To Do", "1", title="Add retry", assignee="Me", assignee_id="a1")],
        now=T0,
    )
    assert snap.save(new)
    path = snap.snapshot_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = snap.load()
    assert loaded == new
    assert not list(path.parent.glob(".tmp.*"))


def test_a_corrupt_or_foreign_snapshot_reads_as_none() -> None:
    path = snap.snapshot_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert snap.load() is None
    path.write_text("[1, 2]")
    assert snap.load() is None


def test_malformed_entries_are_skipped_not_fatal() -> None:
    path = snap.snapshot_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "taken_at": "2026-10-01T12:00:00+00:00",
                "tickets": {"DR-1": {"status": "To Do", "seq": "x"}, "DR-2": "nope"},
                "prs": {
                    "DR-1": {"number": "7"},
                    "DR-3": {"number": 3},  # the older single-object shape
                    "DR-4": [{"number": 4, "state": "OPEN"}, {"number": "x"}],
                    "DR-5": [],
                    "DR-6": "nope",
                },
                "sessions_seen": {
                    "s1": {"ticket": "DR-1", "status": "IDLE", "pid": 3, "settled": True},
                    "s2": {"status": "IDLE"},
                    "s3": "nope",
                },
                "rules_seen": ["a", 3],
            }
        )
    )
    loaded = snap.load()
    assert loaded is not None
    assert list(loaded.tickets) == ["DR-1"]
    assert loaded.tickets["DR-1"].seq == 0
    # DR-4 holds one malformed PR: dropped whole, since a baseline missing a
    # PR would read as that PR just opening. An empty list is a real baseline.
    assert loaded.prs == {"DR-3": (PrState(number=3),), "DR-5": ()}
    assert loaded.sessions_seen == {
        "s1": SessionSeen(ticket="DR-1", status="IDLE", pid=3, settled=True)
    }
    assert loaded.rules_seen == ("a",)


def test_pr_and_session_observations_round_trip() -> None:
    new, _ = snap.advance(None, [_issue("DR-1", "To Do", "1")], now=T0)
    new = replace(
        new,
        pr_checked_at="2026-10-20T12:00:00+00:00",
        prs={"DR-1": (PrState(1, "MERGED", "a", "u1"), PrState(2, "OPEN", "b", "u2")), "DR-2": ()},
        sessions_seen={"s": SessionSeen(ticket="DR-1", status="DEAD", pid=5)},
    )
    assert snap.save(new)
    assert snap.load() == new


# --- fetch / transitions ------------------------------------------------------


def test_fetch_scopes_the_search_to_the_board() -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    def search(jql: str, fields: tuple[str, ...]) -> list[Issue]:
        calls.append((jql, fields))
        return []

    config = _config(jql="assignee = currentUser() ORDER BY rank")
    assert snap.fetch(config.board, search=search) == []
    assert calls == [
        ("project in (DR) AND (assignee = currentUser()) ORDER BY rank", snap.SEARCH_FIELDS)
    ]


def test_log_transitions_writes_one_event_per_move() -> None:
    first, _ = snap.advance(None, [_issue("DR-1", "To Do", "1")], now=T0)
    _, moves = snap.advance(
        first, [_issue("DR-1", "In Progress", "3"), _issue("DR-2", "To Do", "1")], now=T0
    )
    snap.log_transitions(moves)
    events = [e for e in eventlog.read() if e.kind is eventlog.EventKind.TICKET_TRANSITIONED]
    assert [(e.ticket, e.detail) for e in events] == [
        ("DR-1", "To Do → In Progress"),
        ("DR-2", "→ To Do (new on board)"),
    ]
    assert events[0].extra is not None
    assert events[0].extra["seq"] == 1
    assert events[0].extra["status_id"] == "3"


def test_there_is_no_rule_less_refresh() -> None:
    """Saving a snapshot consumes its moves; only the tick (rules included) may do it."""
    assert not hasattr(snap, "refresh")
    assert not hasattr(snap, "refresh_locked")


# --- display helpers ----------------------------------------------------------


def test_printable_strips_control_characters_and_truncates() -> None:
    assert printable("a\x1b[31mred\x07\nline") == "a [31mred line"
    assert printable("abcdef", 4) == "abc…"
    assert printable("") == ""


def test_ticket_sort_key_orders_numerically() -> None:
    keys = ["DR-10", "AB-2", "DR-9", "weird"]
    assert sorted(keys, key=ticket_sort_key) == ["AB-2", "DR-9", "DR-10", "weird"]


def test_snapshot_lives_beside_the_session_state(tmp_path: Path) -> None:
    assert snap.snapshot_path() == tmp_path / "flows" / "snapshot.json"


# --- entered_at, flagged, from_status, PR ci, prs_for -----------------------------------------


def test_entered_at_is_the_tick_that_saw_the_move_and_an_estimate_before_that() -> None:
    jira_date = "2026-09-28T08:00:00+00:00"
    first, _ = snap.advance(
        None, [_issue("DR-1", "To Do", "1", status_changed_at=jira_date)], now=T0
    )
    record = first.tickets["DR-1"]
    assert (record.entered_at, record.entered_estimated) == (jira_date, True)
    # Same status a tick later: kept.
    later = T0 + timedelta(minutes=1)
    second, _ = snap.advance(
        first, [_issue("DR-1", "To Do", "1", status_changed_at=jira_date)], now=later
    )
    assert second.tickets["DR-1"].entered_at == jira_date
    # A move: the tick's own clock, not an estimate, and where it came from.
    moved_at = T0 + timedelta(minutes=2)
    third, _ = snap.advance(second, [_issue("DR-1", "In Progress", "3")], now=moved_at)
    moved = third.tickets["DR-1"]
    assert (moved.entered_at, moved.entered_estimated) == (runs_iso(moved_at), False)
    assert (moved.from_status, moved.from_status_id) == ("To Do", "1")
    fourth, _ = snap.advance(third, [_issue("DR-1", "In Progress", "3")], now=moved_at)
    assert fourth.tickets["DR-1"].entered_at == runs_iso(moved_at)
    assert fourth.tickets["DR-1"].from_status == "To Do"


def runs_iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def test_an_old_snapshot_without_entered_at_borrows_the_estimate() -> None:
    old = Snapshot.from_dict(
        {"taken_at": runs_iso(T0), "tickets": {"DR-1": {"status": "QA", "status_id": "4"}}}
    )
    record = old.tickets["DR-1"]
    assert (record.entered_at, record.entered_estimated, record.flagged) == ("", False, False)
    new, _ = snap.advance(
        old, [_issue("DR-1", "QA", "4", status_changed_at="2026-09-30T00:00:00+00:00")], now=T0
    )
    assert new.tickets["DR-1"].entered_at == "2026-09-30T00:00:00+00:00"
    assert new.tickets["DR-1"].entered_estimated


def test_the_new_fields_round_trip() -> None:
    ticket = TicketState(
        status="QA",
        entered_at=runs_iso(T0),
        entered_estimated=True,
        flagged=True,
        from_status="In Progress",
        from_status_id="3",
        unseen_since=runs_iso(T0),
    )
    original = Snapshot(
        taken_at=runs_iso(T0),
        tickets={"DR-1": ticket},
        prs={"DR-1": (PrState(5, "OPEN", ci="fail"), PrState(6, "MERGED", ci="bogus"))},
        pr_repos={"DR-1": "/repo"},
        flagged_field="customfield_10021",
        flagged_for="abc",
        sessions_seen={"s": SessionSeen(ticket="DR-1", status="IDLE", idle_since=runs_iso(T0))},
    )
    back = Snapshot.from_dict(json.loads(original.to_json()))
    assert back.tickets["DR-1"] == ticket
    assert [pr.ci for pr in back.prs["DR-1"]] == ["fail", ""]  # an unknown word is unknown
    assert back.pr_repos == {"DR-1": "/repo"}
    assert (back.flagged_field, back.flagged_for) == ("customfield_10021", "abc")
    assert back.sessions_seen["s"].idle_since == runs_iso(T0)
    assert back.prs_for("dr-1") == back.prs["DR-1"]
    assert back.prs_for("DR-9") == ()


def test_the_flag_comes_from_the_issue() -> None:
    new, _ = snap.advance(None, [_issue("DR-1", "QA", "4", flagged=True)], now=T0)
    assert new.tickets["DR-1"].flagged
    unflagged, _ = snap.advance(new, [_issue("DR-1", "QA", "4")], now=T0)
    assert not unflagged.tickets["DR-1"].flagged


def test_the_search_asks_for_the_flagged_field_only_when_known() -> None:
    asked: list[tuple[str, ...]] = []

    def search(jql: str, fields: tuple[str, ...]) -> list[Issue]:
        asked.append(fields)
        return []

    settings = _config().board
    snap.fetch(settings, search=search)
    snap.fetch(settings, search=search, flagged_field="customfield_10021")
    assert asked[0] == snap.SEARCH_FIELDS
    assert "statuscategorychangedate" in asked[0]
    assert asked[1] == (*snap.SEARCH_FIELDS, "customfield_10021")

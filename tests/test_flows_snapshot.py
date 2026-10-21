"""flows/snapshot.json: the board search, the diff, and the rule-less tick."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ephor import board, eventlog
from ephor.filelock import locked
from ephor.flows import printable, ticket_sort_key
from ephor.flows import snapshot as snap
from ephor.flows.snapshot import (
    DROP_AFTER_UNSEEN,
    PrState,
    RefreshStatus,
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
    current, _ = snap.advance(None, [_issue("DR-1", "In Progress", "3")], now=T0)
    for _ in range(DROP_AFTER_UNSEEN):
        current, _ = snap.advance(current, [], now=T0)
    assert current.tickets["DR-1"].unseen_ticks == DROP_AFTER_UNSEEN
    current, _ = snap.advance(current, [], now=T0)
    assert "DR-1" not in current.tickets


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
        prs={"DR-1": PrState(number=7, state="OPEN", head_sha="deadbeef", url="u")},
        sessions_seen={"s1": {"status": "IDLE"}},
    )
    new, _ = snap.advance(previous, [], now=T0)
    assert new.rules_seen == ("abc",)
    assert new.myself == "acct-1"
    assert new.prs["DR-1"].number == 7
    assert new.sessions_seen == {"s1": {"status": "IDLE"}}
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
                "prs": {"DR-1": {"number": "7"}, "DR-3": {"number": 3}},
                "rules_seen": ["a", 3],
            }
        )
    )
    loaded = snap.load()
    assert loaded is not None
    assert list(loaded.tickets) == ["DR-1"]
    assert loaded.tickets["DR-1"].seq == 0
    assert list(loaded.prs) == ["DR-3"]
    assert loaded.rules_seen == ("a",)


# --- fetch / refresh ----------------------------------------------------------


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


def test_refresh_needs_a_config_a_sound_board_and_jira() -> None:
    assert snap.refresh(None).status is RefreshStatus.NO_CONFIG
    broken = _config(columns=[])
    assert snap.refresh(broken).status is RefreshStatus.BOARD_INVALID
    assert snap.refresh(_config()).status is RefreshStatus.JIRA_OFF
    assert not snap.snapshot_path().exists()


@pytest.mark.usefixtures("jira_on")
def test_a_failed_search_writes_nothing() -> None:
    first, _ = snap.advance(None, [_issue("DR-1", "To Do", "1")], now=T0)
    snap.save(first)
    before = snap.snapshot_path().read_text()

    result = snap.refresh(_config(), search=lambda *a, **k: None)
    assert result.status is RefreshStatus.SEARCH_FAILED
    assert "left as it was" in result.message
    assert snap.snapshot_path().read_text() == before
    assert eventlog.read() == []


@pytest.mark.usefixtures("jira_on")
def test_refresh_persists_the_snapshot_and_logs_each_move() -> None:
    first, _ = snap.advance(None, [_issue("DR-1", "To Do", "1")], now=T0)
    snap.save(first)

    result = snap.refresh(
        _config(),
        search=lambda *a, **k: [_issue("DR-1", "In Progress", "3"), _issue("DR-2", "To Do", "1")],
    )
    assert result.ok
    assert [t.key for t in result.transitions] == ["DR-1", "DR-2"]
    stored = snap.load()
    assert stored is not None
    assert stored.tickets["DR-1"].seq == 1

    events = [e for e in eventlog.read() if e.kind is eventlog.EventKind.TICKET_TRANSITIONED]
    assert [(e.ticket, e.detail) for e in events] == [
        ("DR-1", "To Do → In Progress"),
        ("DR-2", "→ To Do (new on board)"),
    ]
    assert events[0].extra is not None
    assert events[0].extra["seq"] == 1
    assert events[0].extra["status_id"] == "3"


@pytest.mark.usefixtures("jira_on")
def test_a_dry_run_writes_nothing_at_all() -> None:
    result = snap.refresh(
        _config(), dry_run=True, search=lambda *a, **k: [_issue("DR-1", "To Do", "1")]
    )
    assert result.ok
    assert result.snapshot is not None
    assert "DR-1" in result.snapshot.tickets
    assert not snap.flows_dir().exists()
    assert eventlog.read() == []


@pytest.mark.usefixtures("jira_on")
def test_a_held_tick_lock_means_someone_else_is_writing() -> None:
    called: list[bool] = []

    def search(*_a: object, **_k: object) -> list[Issue]:
        called.append(True)
        return []

    with locked(snap.tick_lock_path()) as held:
        assert held
        result = snap.refresh(_config(), search=search)
    assert result.status is RefreshStatus.LOCKED
    assert called == []  # no search, no write: the holder is doing both
    assert not snap.snapshot_path().exists()


@pytest.mark.usefixtures("jira_on")
def test_refresh_uses_jira_api_search_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from ephor import jira_api

    seen: list[str] = []

    def search(jql: str, fields: tuple[str, ...] = ()) -> list[Issue]:
        seen.append(jql)
        return [_issue("DR-9", "To Do", "1")]

    monkeypatch.setattr(jira_api, "search", search)
    assert snap.refresh(_config()).ok
    assert seen and seen[0].startswith("project in (DR)")


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

"""Work-item store: merge semantics, the confirmed ratchet, and durability."""

from __future__ import annotations

import json
from pathlib import Path

from ephor import work_items


def test_record_creates_and_merges_without_clobbering() -> None:
    """Each caller writes only what it knows; None means "nothing new"."""
    work_items.record("DR-8222", session_id="s1", branch="DR-8222-x")
    work_items.record("DR-8222", pr_number=12, pr_url="https://x/12", pr_state="OPEN")
    work_items.record("DR-8222", jira_status="In Progress")

    item = work_items.load("DR-8222")
    assert item is not None
    assert item.branch == "DR-8222-x"  # not clobbered by the later writes
    assert item.pr_number == 12
    assert item.jira_status == "In Progress"
    assert item.session_ids == ("s1",)


def test_confirmed_is_a_ratchet() -> None:
    """A later guess must never demote a key an earlier fact established."""
    work_items.record("DR-1", confirmed=True)
    work_items.record("DR-1", confirmed=False)
    item = work_items.load("DR-1")
    assert item is not None
    assert item.confirmed is True


def test_sessions_are_most_recent_first_and_deduped() -> None:
    for sid in ("a", "b", "a", "c"):
        work_items.record("DR-2", session_id=sid)
    item = work_items.load("DR-2")
    assert item is not None
    assert item.session_ids == ("c", "a", "b")


def test_session_list_is_bounded() -> None:
    """A long-lived ticket accumulating resumes must not grow without limit."""
    for i in range(work_items._MAX_SESSIONS + 10):
        work_items.record("DR-3", session_id=f"s{i}")
    item = work_items.load("DR-3")
    assert item is not None
    assert len(item.session_ids) == work_items._MAX_SESSIONS


def test_non_ticket_keys_are_refused() -> None:
    assert work_items.record("not a key") is None
    assert work_items.record("") is None
    assert work_items.load("../../etc/passwd") is None


def test_load_survives_a_corrupt_file() -> None:
    work_items.record("DR-4", branch="b")
    path = work_items.work_dir() / "DR-4.json"
    path.write_text("{ this is not json")
    assert work_items.load("DR-4") is None
    assert work_items.load_all() == {}


def test_files_are_written_atomically_and_privately() -> None:
    work_items.record("DR-5")
    path = work_items.work_dir() / "DR-5.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["key"] == "DR-5"
    # No atomic-write leftovers.
    assert not list(work_items.work_dir().glob(".tmp.*"))


def test_forget_and_prune() -> None:
    work_items.record("DR-6")
    assert work_items.forget("DR-6") is True
    assert work_items.load("DR-6") is None

    work_items.record("DR-7")
    assert work_items.prune(max_age_days=30.0) == 0
    assert work_items.prune(max_age_days=0.0) == 1


def test_round_trips_through_disk(tmp_path: Path) -> None:
    work_items.record(
        "DR-8",
        session_id="s",
        repo="ephor",
        branch="b",
        worktree=str(tmp_path),
        confirmed=True,
        pr_number=1,
        pr_url="u",
        pr_state="MERGED",
        jira_status="Done",
        jira_title="t",
        jira_assignee="a",
    )
    item = work_items.load("DR-8")
    assert item is not None
    assert item.has_pr
    restored = work_items.WorkItem.from_dict(json.loads(item.to_json()))
    assert restored == item

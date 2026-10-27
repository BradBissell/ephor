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


def test_record_holds_the_store_lock_across_its_read_modify_write(monkeypatch) -> None:
    """The flows tick writes from outside the TUI; a merge must not interleave."""
    from ephor.filelock import locked

    seen: list[bool] = []
    real_load = work_items.load

    def spying_load(key: str):
        with locked(work_items._lock_path(), timeout=0) as free:
            seen.append(free)
        return real_load(key)

    monkeypatch.setattr(work_items, "load", spying_load)
    work_items.record("DR-9", branch="b")
    assert seen == [False]  # someone (record) already held it


def test_concurrent_records_lose_no_updates() -> None:
    import threading

    def writer(i: int) -> None:
        for j in range(3):
            work_items.record("DR-10", session_id=f"t{i}-{j}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    item = work_items.load("DR-10")
    assert item is not None
    assert len(item.session_ids) == 24


def test_lock_file_is_not_mistaken_for_a_record() -> None:
    work_items.record("DR-11")
    assert (work_items.work_dir() / ".lock").exists()
    assert set(work_items.load_all()) == {"DR-11"}
    assert work_items.prune(max_age_days=0.0) == 1


def test_save_holds_the_store_lock_around_its_write(monkeypatch) -> None:
    from ephor.filelock import locked

    seen: list[bool] = []
    real_write = work_items._write

    def spying_write(path, item):
        with locked(work_items._lock_path(), timeout=0) as free:
            seen.append(free)
        return real_write(path, item)

    monkeypatch.setattr(work_items, "_write", spying_write)
    assert work_items.save(work_items.WorkItem(key="DR-12")) is True
    assert seen == [False]


def test_forget_holds_the_store_lock_so_a_record_cannot_resurrect_it(monkeypatch) -> None:
    """record() loading DR-1 while forget() unlinks it would write the ticket back."""
    import pathlib

    from ephor.filelock import locked

    work_items.record("DR-1", branch="b")
    seen: list[bool] = []
    real_unlink = pathlib.Path.unlink

    def spying_unlink(self, *args, **kwargs):
        with locked(work_items._lock_path(), timeout=0) as free:
            seen.append(free)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", spying_unlink)
    assert work_items.forget("DR-1") is True
    assert seen == [False]
    assert work_items.load("DR-1") is None

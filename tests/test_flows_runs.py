"""Run files: the O_EXCL claim, compare-and-set updates, approve/deny, badges."""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ephor import eventlog
from ephor.filelock import locked
from ephor.flows import runs
from ephor.flows.runs import Run, RunState

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _run(
    run_id: str = "DR-1.review.10-1", state: RunState = RunState.AWAITING_CONFIRM, **extra: object
) -> Run:
    ticket, flow, generation = run_id.split(".")
    fields: dict[str, object] = {
        "run_id": run_id,
        "flow": flow,
        "ticket": ticket,
        "trigger": "on_enter",
        "generation": generation,
        "state": state,
        "created_at": runs.iso(T0),
        "updated_at": runs.iso(T0),
        **extra,
    }
    return Run(**fields)  # type: ignore[arg-type]


def test_create_is_a_claim_only_one_caller_wins() -> None:
    assert runs.create(_run())
    assert not runs.create(_run(state=RunState.CLAIMED))
    stored = runs.load("DR-1.review.10-1")
    assert stored is not None and stored.state is RunState.AWAITING_CONFIRM
    assert stat.S_IMODE(runs.run_path(stored.run_id).stat().st_mode) == 0o600
    # No temporary file is left behind by either attempt.
    assert sorted(p.name for p in runs.runs_dir().iterdir()) == ["DR-1.review.10-1.json"]


def test_create_falls_back_to_o_excl_without_hard_links(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_links(*_: object) -> None:
        raise PermissionError("no hard links here")

    monkeypatch.setattr(os, "link", no_links)
    assert runs.create(_run())
    assert not runs.create(_run())
    assert runs.load("DR-1.review.10-1") is not None


def test_create_refuses_an_unsafe_id() -> None:
    assert not runs.create(replace(_run(), run_id="../../etc.passwd.x"))
    assert not runs.runs_dir().exists() or list(runs.runs_dir().glob("*.json")) == []


def test_round_trip_and_garbage_is_skipped() -> None:
    original = _run(stale=True, attempts=2, exit_code=3, pane="%4", title="T", seq=1)
    assert runs.create(original)
    assert runs.load(original.run_id) == original
    runs.runs_dir().joinpath("DR-2.x.y.json").write_text("{not json")
    runs.runs_dir().joinpath("DR-3.x.y.json").write_text(
        json.dumps({"run_id": "DR-3.x.y", "state": "bogus"})
    )
    assert [r.run_id for r in runs.list_runs()] == [original.run_id]


def test_list_is_newest_first() -> None:
    runs.create(_run("DR-1.a.1-0"))
    runs.create(_run("DR-2.a.1-0", created_at=runs.iso(T0 + timedelta(minutes=1))))
    assert [r.run_id for r in runs.list_runs()] == ["DR-2.a.1-0", "DR-1.a.1-0"]


def test_update_is_compare_and_set() -> None:
    runs.create(_run())
    assert runs.update("DR-1.review.10-1", expect={RunState.CLAIMED}, reason="x") is None
    updated = runs.update(
        "DR-1.review.10-1",
        expect={RunState.AWAITING_CONFIRM},
        state=RunState.EXPIRED,
        now=T0 + timedelta(hours=1),
    )
    assert updated is not None
    assert updated.state is RunState.EXPIRED
    assert updated.updated_at == runs.iso(T0 + timedelta(hours=1))
    assert runs.update("missing.run.x", expect=set(RunState)) is None


def test_update_refuses_while_another_holder_has_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    runs.create(_run())
    monkeypatch.setattr(runs, "_LOCK_TIMEOUT_SEC", 0.05)
    with locked(runs.runs_dir() / ".lock", timeout=0) as held:
        assert held
        assert (
            runs.update("DR-1.review.10-1", expect={RunState.AWAITING_CONFIRM}, reason="x") is None
        )
    assert (
        runs.update("DR-1.review.10-1", expect={RunState.AWAITING_CONFIRM}, reason="x") is not None
    )


def test_a_ui_approve_gives_up_quickly_and_says_busy() -> None:
    """The f screen answers on the event loop: it must not freeze behind runs/.lock."""
    runs.create(_run())
    with locked(runs.runs_dir() / ".lock", timeout=0) as held:
        assert held
        began = time.monotonic()
        approved, message = runs.approve("DR-1.review.10-1", lock_timeout=runs.UI_LOCK_TIMEOUT_SEC)
        denied, deny_message = runs.deny("DR-1.review.10-1", lock_timeout=runs.UI_LOCK_TIMEOUT_SEC)
        waited = time.monotonic() - began
    assert approved is None and denied is None
    assert "busy" in message and "try again" in message
    assert "busy" in deny_message
    assert runs.UI_LOCK_TIMEOUT_SEC <= 0.25
    assert waited < 1.0
    stored = runs.load("DR-1.review.10-1")
    assert stored is not None and stored.state is RunState.AWAITING_CONFIRM


def test_approve_and_deny_only_answer_an_awaiting_run() -> None:
    runs.create(_run())
    approved, message = runs.approve("DR-1.review.10-1")
    assert approved is not None and approved.state is RunState.CLAIMED
    assert "next tick" in message
    again, message = runs.approve("DR-1.review.10-1")
    assert again is None and "claimed, not awaiting_confirm" in message
    denied, message = runs.deny("DR-1.review.10-1")
    assert denied is None
    assert runs.deny("DR-9.review.1-0")[1].endswith("no such run")


def test_deny_logs_a_cancellation() -> None:
    runs.create(_run())
    denied, _ = runs.deny("DR-1.review.10-1")
    assert denied is not None and denied.state is RunState.DENIED
    (event,) = [e for e in eventlog.read() if e.kind is eventlog.EventKind.FLOW_CANCELLED]
    assert event.ticket == "DR-1"
    assert event.extra == {
        "run_id": "DR-1.review.10-1",
        "flow": "review",
        "generation": "10-1",
        "reason": "denied",
        "state": "denied",
    }


def test_an_approve_racing_an_expiry_cannot_resurrect_the_run() -> None:
    runs.create(_run())
    seen = runs.load("DR-1.review.10-1")
    assert seen is not None and seen.state is RunState.AWAITING_CONFIRM  # what approve read
    runs.update(seen.run_id, expect={RunState.AWAITING_CONFIRM}, state=RunState.EXPIRED)  # the tick
    approved, message = runs.approve(seen.run_id)
    assert approved is None and "expired" in message
    stored = runs.load(seen.run_id)
    assert stored is not None and stored.state is RunState.EXPIRED


def test_resolve_accepts_a_unique_prefix_in_any_case() -> None:
    runs.create(_run("DR-1.review.10-1"))
    runs.create(_run("DR-12.review.10-1", state=RunState.DONE))
    runs.create(_run("DR-2.notify.10-0"))
    assert runs.resolve("DR-1.review.10-1")[0] is not None
    found, _ = runs.resolve("dr-2")
    assert found is not None and found.run_id == "DR-2.notify.10-0"
    # A ticket key typed whole names that ticket, never DR-12 or DR-13.
    found, _ = runs.resolve("DR-1")
    assert found is not None and found.run_id == "DR-1.review.10-1"
    runs.create(_run("DR-13.review.10-1"))
    found, _ = runs.resolve("DR-1")
    assert found is not None and found.run_id == "DR-1.review.10-1"
    found, _ = runs.resolve("dr-1.rev")
    assert found is not None and found.run_id == "DR-1.review.10-1"
    assert runs.resolve("nope")[1] == "no run matches 'nope'"
    assert runs.resolve("  ")[1] == "no run id given"


def test_resolve_never_crosses_onto_a_longer_ticket_key() -> None:
    runs.create(_run("DR-1.review.10-1", state=RunState.DENIED))
    runs.create(_run("DR-12.review.10-1"))
    found, _ = runs.resolve("DR-1")
    assert found is not None and found.run_id == "DR-1.review.10-1"
    # A partial key is no key at all.
    found, problem = runs.resolve("DR-")
    assert found is None and problem == "no run matches 'DR-'"


def test_resolve_finds_nothing_for_a_ticket_with_no_run() -> None:
    runs.create(_run("DR-15.review.10001-1"))
    found, problem = runs.resolve("DR-1")
    assert found is None and problem == "no run matches 'DR-1'"


def test_the_awaiting_tie_break_stays_within_one_ticket() -> None:
    runs.create(_run("DR-1.review.10-1"))
    runs.create(_run("DR-1.review.10-3", state=RunState.DONE))
    found, _ = runs.resolve("DR-1")
    assert found is not None and found.run_id == "DR-1.review.10-1"
    runs.create(_run("DR-1.notify.10-1"))
    found, problem = runs.resolve("DR-1")
    assert found is None and "matches 3 runs" in problem


def test_status_generation_hashes_names_that_are_not_ids() -> None:
    assert runs.status_generation("10", "Peer Review", 2) == "10-2"
    named = runs.status_generation("", "Peer Review / QA", 0)
    assert named.startswith("n") and named.endswith("-0")
    assert runs.valid_run_id(runs.make_run_id("DR-1", "review", named))
    assert runs.status_generation("", "peer review / qa", 0) == named


def test_payloads_are_private(monkeypatch: pytest.MonkeyPatch) -> None:
    path = runs.write_payload("DR-1.review.10-1", {"ticket": "DR-1"})
    assert path is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"ticket": "DR-1"}


def test_read_exit() -> None:
    assert runs.read_exit("DR-1.a.1-0") is None
    runs.runs_dir().mkdir(parents=True)
    runs.exit_path("DR-1.a.1-0").write_text("7\n")
    assert runs.read_exit("DR-1.a.1-0") == 7
    runs.exit_path("DR-1.a.1-0").write_text("")
    assert runs.read_exit("DR-1.a.1-0") is None


def test_pause_and_resume() -> None:
    assert not runs.is_paused()
    assert runs.pause() and runs.is_paused()
    assert runs.pause()  # twice is fine
    assert runs.resume() and not runs.is_paused()
    assert runs.resume()


def test_prune_removes_old_terminal_runs_and_their_files() -> None:
    old = runs.iso(T0 - timedelta(days=31))
    runs.create(_run("DR-1.a.1-0", state=RunState.DONE, updated_at=old))
    runs.create(_run("DR-2.a.1-0", state=RunState.AWAITING_CONFIRM, updated_at=old))
    runs.create(_run("DR-3.a.1-0", state=RunState.DONE))
    runs.exit_path("DR-1.a.1-0").write_text("0")
    runs.write_payload("DR-1.a.1-0", {})
    assert runs.prune(T0) == 1
    assert sorted(r.run_id for r in runs.list_runs()) == ["DR-2.a.1-0", "DR-3.a.1-0"]
    assert not runs.exit_path("DR-1.a.1-0").exists()
    assert not runs.payload_path("DR-1.a.1-0").exists()


@pytest.mark.parametrize(
    ("state", "stale", "badge"),
    [
        (RunState.AWAITING_CONFIRM, False, "⏸ review?"),
        (RunState.AWAITING_CONFIRM, True, "⏸ review? (stale)"),
        (RunState.CLAIMED, False, "… review"),
        (RunState.BLOCKED, False, "⊘ review"),
        (RunState.LAUNCHED, False, "▶ review"),
        (RunState.STUCK, False, "⚠ review stuck"),
        (RunState.DONE, False, ""),
    ],
)
def test_badge(state: RunState, stale: bool, badge: str) -> None:
    assert runs.badge(_run(state=state, stale=stale)) == badge


def test_badges_pick_the_most_pressing_run_per_ticket() -> None:
    all_runs = [
        _run("DR-1.notify.10-1", state=RunState.LAUNCHED),
        _run("DR-1.review.10-1", state=RunState.AWAITING_CONFIRM),
        _run("DR-2.review.10-1", state=RunState.DONE),
        _run("DR-3.review.10-1", state=RunState.STUCK, updated_at=runs.iso(T0)),
        _run("DR-4.review.10-1", state=RunState.STUCK, updated_at=runs.iso(T0 - timedelta(days=2))),
    ]
    assert runs.badges_by_ticket(all_runs, now=T0 + timedelta(hours=1)) == {
        "DR-1": "⏸ review?",
        "DR-3": "⚠ review stuck",
    }

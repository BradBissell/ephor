"""Permission decisions: payload shape, standing vs one-shot, path safety."""

from __future__ import annotations

import json

from ephor import permissions
from ephor.config import pending_dir
from ephor.permissions import Decision


def test_decide_writes_a_payload_the_hook_can_emit() -> None:
    assert permissions.decide("abc", Decision.ALLOW) is True
    raw = json.loads((pending_dir() / "abc.json").read_text())
    # Both the nested Claude-shaped key and the flat one, so no per-provider
    # payload table is needed.
    assert raw["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert raw["permissionDecision"] == "allow"


def test_standing_decisions_live_in_their_own_file() -> None:
    permissions.decide("abc", Decision.ALLOW, standing=True)
    assert (pending_dir() / "abc.always.json").exists()
    assert not (pending_dir() / "abc.json").exists()
    assert permissions.standing_for("abc") is Decision.ALLOW


def test_standing_wins_over_one_shot_in_the_pending_view() -> None:
    permissions.decide("abc", Decision.DENY, standing=True)
    permissions.decide("abc", Decision.ALLOW)
    entry = permissions.pending()["abc"]
    assert entry.standing is True
    assert entry.decision is Decision.DENY


def test_revoke_clears_both_files() -> None:
    permissions.decide("abc", Decision.ALLOW)
    permissions.decide("abc", Decision.ALLOW, standing=True)
    assert permissions.revoke("abc") is True
    assert permissions.pending() == {}
    assert permissions.standing_for("abc") is None


def test_session_ids_that_could_escape_the_directory_are_refused() -> None:
    assert permissions.decide("../escape", Decision.ALLOW) is False
    assert permissions.decide("a/b", Decision.ALLOW) is False
    assert permissions.decide("", Decision.ALLOW) is False


def test_decision_files_are_private() -> None:
    permissions.decide("abc", Decision.ALLOW)
    assert (pending_dir() / "abc.json").stat().st_mode & 0o777 == 0o600


def test_corrupt_decision_files_are_ignored() -> None:
    pending_dir().mkdir(parents=True, exist_ok=True)
    (pending_dir() / "junk.json").write_text("nope")
    assert permissions.pending() == {}


def test_hook_wait_sec_is_clamped(monkeypatch) -> None:
    assert permissions.hook_wait_sec() == 0
    monkeypatch.setenv("EPHOR_PERMISSION_WAIT_SEC", "5")
    assert permissions.hook_wait_sec() == 5
    monkeypatch.setenv("EPHOR_PERMISSION_WAIT_SEC", "9999")
    assert permissions.hook_wait_sec() == 60
    monkeypatch.setenv("EPHOR_PERMISSION_WAIT_SEC", "nonsense")
    assert permissions.hook_wait_sec() == 0

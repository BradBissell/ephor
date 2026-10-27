"""Push notifications: opt-in gating, relevance, and the anti-noise rules.

The transport is three lines and never the problem. Everything worth testing
here is a rule about *suppression*, because the only way a notifier fails is
by sending too much.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ephor import notify
from ephor.notify import Attention, Notifier


def _item(sid: str = "s1", reason: str = "WAITING_PERMISSION") -> Attention:
    return Attention(session_id=sid, reason=reason, label=sid, detail="d")


@pytest.fixture
def posts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Arm a destination and capture what would go over the wire."""
    sent: list[tuple[str, str, str]] = []

    def capture(url: str, title: str, body: str, priority: str, tag: str, click: str = "") -> bool:
        sent.append((title, body, priority))
        return True

    monkeypatch.setenv("EPHOR_NOTIFY_URL", "https://ntfy.sh/mytopic")
    monkeypatch.setattr(notify, "_post", capture)
    return sent


def _eager(**kwargs: object) -> Notifier:
    """A notifier with the time-based gates opened, to test one rule at a time."""
    defaults: dict[str, object] = {"dwell_sec": 0.0, "cooldown_sec": 0.0, "batch_threshold": 99}
    defaults.update(kwargs)
    return Notifier(**defaults)  # type: ignore[arg-type]


# --- opt-in ---------------------------------------------------------------


def test_stays_off_until_a_destination_is_named() -> None:
    assert notify.is_configured() is False
    assert _eager().tick([_item()]) == 0


def test_only_http_destinations_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPHOR_NOTIFY_URL", "file:///etc/passwd")
    assert notify.is_configured() is False
    monkeypatch.setenv("EPHOR_NOTIFY_URL", "https://ntfy.sh/mytopic")
    assert notify.is_configured() is True


# --- relevance ------------------------------------------------------------


def test_conditions_are_silent_by_default(posts: list[tuple[str, str, str]]) -> None:
    """Red CI is a state of the work, not an interruption. Default says no."""
    assert notify.notify_level() == "blocked"
    _eager().tick([_item(reason="CI_FAILED"), _item(sid="s2", reason="REVIEW_REQUESTED")])
    assert posts == []


def test_level_all_lets_conditions_through(
    monkeypatch: pytest.MonkeyPatch, posts: list[tuple[str, str, str]]
) -> None:
    monkeypatch.setenv("EPHOR_NOTIFY_LEVEL", "all")
    _eager().tick([_item(reason="CI_FAILED")])
    assert len(posts) == 1
    assert posts[0][2] == "low"  # audible for a blocked agent, quiet for a condition


def test_level_off_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, posts: list[tuple[str, str, str]]
) -> None:
    monkeypatch.setenv("EPHOR_NOTIFY_LEVEL", "off")
    _eager().tick([_item()])
    assert posts == []


def test_an_explicit_reason_list_overrides_the_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPHOR_NOTIFY_LEVEL", "all")
    monkeypatch.setenv("EPHOR_NOTIFY_REASONS", "waiting_permission, ci_failed")
    assert notify.enabled_reasons() == frozenset({"WAITING_PERMISSION", "CI_FAILED"})


def test_a_blocked_agent_is_high_priority(posts: list[tuple[str, str, str]]) -> None:
    _eager().tick([_item()])
    assert posts[0][2] == "high"


# --- dwell ----------------------------------------------------------------


def test_a_condition_you_were_present_for_never_leaves_the_machine(
    posts: list[tuple[str, str, str]],
) -> None:
    """Blocked for four seconds while you sat there is not a phone event."""
    notifier = Notifier(dwell_sec=60.0, cooldown_sec=0.0)
    for _ in range(8):  # eight ticks, four seconds of dashboard time
        notifier.tick([_item()])
    assert posts == []


def test_it_sends_once_dwell_has_elapsed(
    monkeypatch: pytest.MonkeyPatch, posts: list[tuple[str, str, str]]
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(notify, "_now", lambda: clock[0])
    notifier = Notifier(dwell_sec=60.0, cooldown_sec=0.0)
    notifier.tick([_item()])
    assert posts == []
    clock[0] += 61
    notifier.tick([_item()])
    assert len(posts) == 1


# --- dedupe and settling --------------------------------------------------


def test_the_same_reason_is_sent_once(posts: list[tuple[str, str, str]]) -> None:
    """Forty minutes blocked is one event, not forty minutes of events."""
    notifier = _eager()
    for _ in range(5):
        notifier.tick([_item()])
    assert len(posts) == 1


def test_a_flapping_session_is_one_event(
    monkeypatch: pytest.MonkeyPatch, posts: list[tuple[str, str, str]]
) -> None:
    """The regression that made this aggressive: clearing on every idle tick.

    An agent that asks five permissions in a minute used to be five pushes,
    because the condition vanishing between prompts dropped the dedupe record.
    """
    clock = [1000.0]
    monkeypatch.setattr(notify, "_now", lambda: clock[0])
    notifier = _eager(settle_sec=300.0)
    for _ in range(5):
        notifier.tick([_item()])  # blocked
        clock[0] += 5
        notifier.tick([])  # answered; nothing wants anything
        clock[0] += 5
    assert len(posts) == 1


def test_a_recurrence_after_settling_is_a_new_event(
    monkeypatch: pytest.MonkeyPatch, posts: list[tuple[str, str, str]]
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(notify, "_now", lambda: clock[0])
    notifier = _eager(settle_sec=300.0)
    notifier.tick([_item()])
    clock[0] += 400  # well clear of the settle window
    notifier.tick([])
    notifier.tick([_item()])
    assert len(posts) == 2


def test_an_ended_session_forgets_immediately(posts: list[tuple[str, str, str]]) -> None:
    notifier = _eager()
    notifier.tick([_item()])
    notifier.forget("s1")
    notifier.tick([_item()])
    assert len(posts) == 2


def test_a_stuck_session_nudges_again_after_the_rearm_window(
    posts: list[tuple[str, str, str]],
) -> None:
    notifier = _eager(rearm_sec=0.0)
    notifier.tick([_item()])
    notifier.tick([_item()])
    assert len(posts) == 2


# --- cooldown -------------------------------------------------------------


def test_a_new_reason_still_respects_the_session_cooldown(
    monkeypatch: pytest.MonkeyPatch, posts: list[tuple[str, str, str]]
) -> None:
    """PERM -> CI_FAILED -> PERM used to be three unthrottled pushes."""
    monkeypatch.setenv("EPHOR_NOTIFY_LEVEL", "all")
    notifier = Notifier(dwell_sec=0.0, cooldown_sec=600.0, batch_threshold=99)
    notifier.tick([_item(reason="WAITING_PERMISSION")])
    notifier.tick([_item(reason="CI_FAILED")])
    notifier.tick([_item(reason="ERROR")])
    assert len(posts) == 1


def test_the_cooldown_expires(
    monkeypatch: pytest.MonkeyPatch, posts: list[tuple[str, str, str]]
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(notify, "_now", lambda: clock[0])
    notifier = Notifier(dwell_sec=0.0, cooldown_sec=600.0, settle_sec=10.0, batch_threshold=99)
    notifier.tick([_item(reason="WAITING_PERMISSION")])
    clock[0] += 700
    notifier.tick([_item(reason="ERROR")])
    assert len(posts) == 2


def test_the_cooldown_is_per_session(posts: list[tuple[str, str, str]]) -> None:
    notifier = Notifier(dwell_sec=0.0, cooldown_sec=600.0, batch_threshold=99)
    notifier.tick([_item(sid="s1"), _item(sid="s2")])
    assert len(posts) == 2


# --- batching -------------------------------------------------------------


def test_a_busy_board_collapses_into_one_digest(posts: list[tuple[str, str, str]]) -> None:
    """Ten blocked sessions is one notification, not ten."""
    notifier = Notifier(dwell_sec=0.0, cooldown_sec=0.0, batch_threshold=3)
    notifier.tick([_item(sid=f"s{i}") for i in range(10)])
    assert len(posts) == 1
    title, body, _ = posts[0]
    assert "10 sessions" in title
    assert body.count("\n") == 9


def test_below_the_threshold_stays_individual(posts: list[tuple[str, str, str]]) -> None:
    notifier = Notifier(dwell_sec=0.0, cooldown_sec=0.0, batch_threshold=3)
    notifier.tick([_item(sid="s1"), _item(sid="s2")])
    assert len(posts) == 2


def test_a_digest_carries_the_most_urgent_priority(
    monkeypatch: pytest.MonkeyPatch, posts: list[tuple[str, str, str]]
) -> None:
    monkeypatch.setenv("EPHOR_NOTIFY_LEVEL", "all")
    notifier = Notifier(dwell_sec=0.0, cooldown_sec=0.0, batch_threshold=2)
    notifier.tick(
        [
            _item(sid="s1", reason="CI_FAILED"),
            _item(sid="s2", reason="REVIEW_REQUESTED"),
            _item(sid="s3", reason="WAITING_PERMISSION"),
        ]
    )
    assert posts[0][2] == "high"


# --- quiet hours ----------------------------------------------------------


@pytest.mark.parametrize(
    ("window", "hour", "expected"),
    [
        ("22:00-07:00", 23, True),
        ("22:00-07:00", 3, True),
        ("22:00-07:00", 12, False),
        ("09:00-17:00", 12, True),
        ("09:00-17:00", 20, False),
        ("garbage", 3, False),
        ("", 3, False),
    ],
)
def test_quiet_window_parsing(
    monkeypatch: pytest.MonkeyPatch, window: str, hour: int, expected: bool
) -> None:
    monkeypatch.setenv("EPHOR_NOTIFY_QUIET", window)
    assert notify.in_quiet_hours(datetime(2026, 1, 1, hour, 30)) is expected


def test_quiet_hours_hold_conditions_but_not_a_blocked_agent(
    monkeypatch: pytest.MonkeyPatch, posts: list[tuple[str, str, str]]
) -> None:
    monkeypatch.setenv("EPHOR_NOTIFY_LEVEL", "all")
    monkeypatch.setenv("EPHOR_NOTIFY_QUIET", "22:00-07:00")
    monkeypatch.setattr(notify, "in_quiet_hours", lambda when=None: True)
    notifier = _eager()
    notifier.tick([_item(sid="s1", reason="CI_FAILED"), _item(sid="s2")])
    assert len(posts) == 1
    assert "s2" in posts[0][0]


def test_strict_quiet_hours_hold_everything(
    monkeypatch: pytest.MonkeyPatch, posts: list[tuple[str, str, str]]
) -> None:
    monkeypatch.setenv("EPHOR_NOTIFY_QUIET", "22:00-07:00")
    monkeypatch.setenv("EPHOR_NOTIFY_QUIET_STRICT", "1")
    monkeypatch.setattr(notify, "in_quiet_hours", lambda when=None: True)
    _eager().tick([_item()])
    assert posts == []


def test_a_held_notification_fires_when_quiet_ends(
    monkeypatch: pytest.MonkeyPatch, posts: list[tuple[str, str, str]]
) -> None:
    """Held, not dropped — an unresolved condition still gets announced."""
    monkeypatch.setenv("EPHOR_NOTIFY_LEVEL", "all")
    quiet = [True]
    monkeypatch.setattr(notify, "in_quiet_hours", lambda when=None: quiet[0])
    notifier = _eager()
    notifier.tick([_item(reason="CI_FAILED")])
    assert posts == []
    quiet[0] = False
    notifier.tick([_item(reason="CI_FAILED")])
    assert len(posts) == 1


# --- transport failure ----------------------------------------------------


def test_a_failed_post_is_not_recorded_as_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dropped notification must be retried, not silently swallowed."""
    clock = [1000.0]
    monkeypatch.setattr(notify, "_now", lambda: clock[0])
    monkeypatch.setenv("EPHOR_NOTIFY_URL", "https://ntfy.sh/mytopic")
    attempts: list[int] = []
    monkeypatch.setattr(notify, "_post", lambda *a, **k: bool(attempts.append(1)))
    notifier = _eager()
    notifier.tick([_item()])
    clock[0] += 3600  # past any backoff
    notifier.tick([_item()])
    assert len(attempts) == 2


def test_a_dead_endpoint_is_not_retried_every_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dashboard observes at 2Hz; the retry must not follow it."""
    monkeypatch.setenv("EPHOR_NOTIFY_URL", "https://ntfy.sh/mytopic")
    attempts: list[int] = []
    monkeypatch.setattr(notify, "_post", lambda *a, **k: bool(attempts.append(1)))
    notifier = _eager()
    for _ in range(20):
        notifier.tick([_item()])
    assert len(attempts) == 1


def test_backoff_grows_and_clears_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(notify, "_now", lambda: clock[0])
    monkeypatch.setenv("EPHOR_NOTIFY_URL", "https://ntfy.sh/mytopic")
    outcome = [False]
    attempts: list[int] = []

    def flaky(*_a: object, **_k: object) -> bool:
        attempts.append(1)
        return outcome[0]

    monkeypatch.setattr(notify, "_post", flaky)
    notifier = _eager()
    notifier.tick([_item()])  # attempt 1, fails, backoff 30s
    clock[0] += 10
    notifier.tick([_item()])  # inside backoff, no attempt
    assert len(attempts) == 1
    clock[0] += 25
    notifier.tick([_item()])  # attempt 2, fails, backoff 60s
    assert len(attempts) == 2
    clock[0] += 40
    notifier.tick([_item()])  # still inside the doubled backoff
    assert len(attempts) == 2
    clock[0] += 30
    outcome[0] = True
    notifier.tick([_item()])  # attempt 3, succeeds
    assert len(attempts) == 3


# --- persistence ----------------------------------------------------------


def test_a_restart_does_not_re_announce_the_board(
    monkeypatch: pytest.MonkeyPatch, posts: list[tuple[str, str, str]], tmp_path: object
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(notify, "_now", lambda: clock[0])
    first = _eager(persist=True, settle_sec=300.0)
    first.tick([_item()])
    assert len(posts) == 1
    clock[0] += 30
    second = _eager(persist=True, settle_sec=300.0)  # the dashboard restarted
    second.tick([_item()])
    assert len(posts) == 1


def test_a_stale_ledger_is_ignored_on_load(
    monkeypatch: pytest.MonkeyPatch, posts: list[tuple[str, str, str]]
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(notify, "_now", lambda: clock[0])
    _eager(persist=True, settle_sec=60.0).tick([_item()])
    clock[0] += 9999
    _eager(persist=True, settle_sec=60.0).tick([_item()])
    assert len(posts) == 2


def test_a_corrupt_ledger_is_survivable(
    monkeypatch: pytest.MonkeyPatch, posts: list[tuple[str, str, str]]
) -> None:
    path = notify.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    _eager(persist=True).tick([_item()])
    assert len(posts) == 1

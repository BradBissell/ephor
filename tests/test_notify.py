"""Push notifications: opt-in gating and dedupe."""

from __future__ import annotations

import pytest

from ephor import notify
from ephor.notify import Notifier


def test_stays_off_until_a_destination_is_named() -> None:
    assert notify.is_configured() is False
    assert Notifier().notify("s1", "PERM", title="t", body="b") is False


def test_only_http_destinations_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPHOR_NOTIFY_URL", "file:///etc/passwd")
    assert notify.is_configured() is False
    monkeypatch.setenv("EPHOR_NOTIFY_URL", "https://ntfy.sh/mytopic")
    assert notify.is_configured() is True


def _armed(monkeypatch: pytest.MonkeyPatch, posts: list[tuple[str, str]]) -> Notifier:
    monkeypatch.setenv("EPHOR_NOTIFY_URL", "https://ntfy.sh/mytopic")
    monkeypatch.setattr(
        notify, "_post", lambda url, title, body, priority, tag: posts.append((title, body)) is None
    )
    return Notifier()


def test_the_same_reason_is_sent_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forty minutes blocked is one event, not forty minutes of events."""
    posts: list[tuple[str, str]] = []
    notifier = _armed(monkeypatch, posts)
    for _ in range(5):
        notifier.notify("s1", "WAITING_PERMISSION", title="t", body="b")
    assert len(posts) == 1


def test_a_new_reason_sends_again(monkeypatch: pytest.MonkeyPatch) -> None:
    posts: list[tuple[str, str]] = []
    notifier = _armed(monkeypatch, posts)
    notifier.notify("s1", "WAITING_PERMISSION", title="t", body="b")
    notifier.notify("s1", "CI_FAILED", title="t", body="b")
    assert len(posts) == 2


def test_clearing_re_arms_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    posts: list[tuple[str, str]] = []
    notifier = _armed(monkeypatch, posts)
    notifier.notify("s1", "WAITING_PERMISSION", title="t", body="b")
    notifier.clear("s1")
    notifier.notify("s1", "WAITING_PERMISSION", title="t", body="b")
    assert len(posts) == 2


def test_a_stuck_session_nudges_again_after_the_rearm_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts: list[tuple[str, str]] = []
    monkeypatch.setenv("EPHOR_NOTIFY_URL", "https://ntfy.sh/mytopic")
    monkeypatch.setattr(
        notify, "_post", lambda url, title, body, priority, tag: posts.append((title, body)) is None
    )
    notifier = Notifier(rearm_sec=0.0)
    notifier.notify("s1", "WAITING_PERMISSION", title="t", body="b")
    notifier.notify("s1", "WAITING_PERMISSION", title="t", body="b")
    assert len(posts) == 2


def test_a_failed_post_is_not_recorded_as_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dropped notification must be retried, not silently swallowed."""
    monkeypatch.setenv("EPHOR_NOTIFY_URL", "https://ntfy.sh/mytopic")
    attempts: list[int] = []

    def failing(*args: object) -> bool:
        attempts.append(1)
        return False

    monkeypatch.setattr(notify, "_post", failing)
    notifier = Notifier()
    assert notifier.notify("s1", "PERM", title="t", body="b") is False
    assert notifier.notify("s1", "PERM", title="t", body="b") is False
    assert len(attempts) == 2

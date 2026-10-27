"""Getting a session's "I need you" off this machine and onto your phone.

ephor already solved noticing, for the case where you are at the desk: the
speech queue reads back a one-line summary so ten parallel sessions can
finish without you watching ten windows. Away from the desk that same problem
comes back undiluted — a session blocked on a permission prompt at 14:02 is
still blocked at 14:40 because nobody was in the room to hear it.

This is the same notification, pushed instead of spoken. It targets
`ntfy <https://ntfy.sh>`_ because a topic URL is the entire configuration —
no account, no app registration, no key exchange — but the transport is a
plain POST, so any webhook that accepts a body works.

A notifier only ever fails by becoming noise, and the naive version of this
is *very* noisy: the dashboard observes twice a second, so a session that
asks five permissions in a minute is five pushes, and a red pull request is
a push every time CI cycles. Six rules keep the stream survivable.

*Opt-in.* No configured URL means no network call. Like :mod:`ephor.jira_api`,
this stays off until the user names a destination.

*Relevance.* Not everything the dashboard highlights deserves a phone. A
blocked agent does; a pull request that has been waiting on review since
Tuesday does not. ``EPHOR_NOTIFY_LEVEL`` draws that line, and defaults to
drawing it tightly.

*Dwell.* A condition has to persist for ``EPHOR_NOTIFY_DWELL_SEC`` before it
is worth a push. If you were at the desk and answered in four seconds, your
phone never hears about it.

*Settling.* A ``(session, reason)`` that clears is *remembered* for
``EPHOR_NOTIFY_SETTLE_SEC``, so an agent flapping in and out of a prompt is
one event rather than one per flap. Only a session that actually ends forgets
immediately.

*Cooldown and batching.* One session pushes at most once per
``EPHOR_NOTIFY_COOLDOWN_SEC`` whatever its reason, and once
``EPHOR_NOTIFY_BATCH_THRESHOLD`` sessions come due in the same tick they
collapse into a single digest. Ten blocked sessions is one notification.

*Quiet hours.* ``EPHOR_NOTIFY_QUIET=22:00-07:00`` holds everything but a live
permission prompt until morning — held, not dropped: an unresolved condition
pushes on the first tick after the window ends.

State is persisted next to the session files, so restarting the dashboard
does not re-announce a board you have already seen.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_TIMEOUT_SEC = 5.0

# Re-arm a (session, reason) after this long even if it never cleared, so a
# genuinely stuck session nudges again on a human timescale instead of once
# and then silently forever.
REARM_SEC = 1800.0

# How long a condition must persist before it is worth a push. Filters out
# everything you were present for anyway.
DWELL_SEC = 45.0

# How long a cleared (session, reason) is remembered. Inside this window a
# recurrence is the same event, not a new one — this is what stops a flapping
# agent from emitting a push per flap.
SETTLE_SEC = 300.0

# Floor between two pushes for one session, whatever the reasons. Stops
# WAITING_PERMISSION -> CI_FAILED -> WAITING_PERMISSION from being three
# unthrottled pushes.
COOLDOWN_SEC = 600.0

# Once this many sessions come due in one tick, send one digest instead.
BATCH_THRESHOLD = 3

# Transport backoff after a failed POST, so a dead endpoint is retried on a
# human timescale rather than at the dashboard's refresh rate.
_BACKOFF_START_SEC = 30.0
_BACKOFF_MAX_SEC = 600.0

# Reasons that mean "a live agent is stopped and cannot continue". These are
# interruptive by nature: the work is not progressing until you act.
BLOCKED_REASONS: frozenset[str] = frozenset({"WAITING_PERMISSION", "WAITING_ANSWER", "ERROR"})

# Reasons that describe a *condition* of the work rather than a stopped
# agent. True for hours or days at a time, so they are announced quietly and
# only when the user asks for them.
CONDITION_REASONS: frozenset[str] = frozenset(
    {"CI_FAILED", "CONFLICT", "CHANGES_REQUESTED", "REVIEW_REQUESTED", "TICKET_DRIFT"}
)

# Conditions that are ambient enough to be excluded even from level=all's
# spirit — they are only ever informational. Included at level=all, never at
# level=blocked, and always at the lowest ntfy priority.
_AMBIENT_REASONS: frozenset[str] = frozenset({"REVIEW_REQUESTED", "TICKET_DRIFT"})

# (ntfy priority, ntfy tag) per reason. Priority is what decides whether the
# phone makes a sound, so it carries most of the perceived aggression.
_REASON_STYLE: dict[str, tuple[str, str]] = {
    "WAITING_PERMISSION": ("high", "warning"),
    "WAITING_ANSWER": ("default", "question"),
    "ERROR": ("high", "rotating_light"),
    "CI_FAILED": ("low", "x"),
    "CONFLICT": ("low", "twisted_rightwards_arrows"),
    "CHANGES_REQUESTED": ("low", "pencil2"),
    "REVIEW_REQUESTED": ("min", "eyes"),
    "TICKET_DRIFT": ("min", "left_right_arrow"),
}
_DEFAULT_STYLE = ("default", "bell")

_PRIORITY_RANK = {"min": 0, "low": 1, "default": 2, "high": 3, "urgent": 4}

# The one thing that still reaches you at 3am, unless quiet hours are strict.
_QUIET_EXEMPT: frozenset[str] = frozenset({"WAITING_PERMISSION"})

_RECORD_SEP = "|"
_STATE_VERSION = 1


def _now() -> float:
    """Wall clock, not monotonic: these windows outlive a process restart."""
    return time.time()


# --- configuration -------------------------------------------------------


def notify_url() -> str:
    """Configured destination, or "" when notification is off."""
    return (os.environ.get("EPHOR_NOTIFY_URL") or "").strip()


def notify_token() -> str:
    """Optional bearer token for the destination."""
    return (os.environ.get("EPHOR_NOTIFY_TOKEN") or "").strip()


def is_configured() -> bool:
    """True when a push would actually be attempted."""
    url = notify_url()
    return url.lower().startswith(("http://", "https://"))


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


def notify_level() -> str:
    """``off`` | ``blocked`` | ``all``. Defaults to ``blocked``.

    ``blocked`` is the useful default because it maps to the only question a
    phone can usefully answer while you are away from the desk: is something
    stopped and waiting on me?
    """
    raw = (os.environ.get("EPHOR_NOTIFY_LEVEL") or "").strip().lower()
    return raw if raw in {"off", "blocked", "all"} else "blocked"


def enabled_reasons() -> frozenset[str]:
    """Reasons that may push, from ``EPHOR_NOTIFY_REASONS`` or the level.

    An explicit list wins over the level, so a user who wants exactly
    ``WAITING_PERMISSION,CI_FAILED`` does not have to accept a whole tier.
    """
    raw = (os.environ.get("EPHOR_NOTIFY_REASONS") or "").strip()
    if raw:
        return frozenset(part.strip().upper() for part in raw.split(",") if part.strip())
    level = notify_level()
    if level == "off":
        return frozenset()
    if level == "all":
        return BLOCKED_REASONS | CONDITION_REASONS
    return BLOCKED_REASONS


def _parse_quiet(raw: str) -> tuple[int, int] | None:
    """``"22:00-07:00"`` -> (1320, 420) in minutes past midnight."""
    val = raw.strip()
    if not val or "-" not in val:
        return None
    start_s, _, end_s = val.partition("-")

    def minutes(text: str) -> int | None:
        hh, _, mm = text.strip().partition(":")
        try:
            hour, minute = int(hh), int(mm or 0)
        except ValueError:
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute

    start, end = minutes(start_s), minutes(end_s)
    if start is None or end is None or start == end:
        return None
    return start, end


def in_quiet_hours(when: datetime | None = None) -> bool:
    """True when the configured quiet window currently applies."""
    window = _parse_quiet(os.environ.get("EPHOR_NOTIFY_QUIET") or "")
    if window is None:
        return False
    start, end = window
    now = when or datetime.now()
    current = now.hour * 60 + now.minute
    if start < end:
        return start <= current < end
    # Window wraps past midnight (the normal case for sleeping hours).
    return current >= start or current < end


def _quiet_strict() -> bool:
    return (os.environ.get("EPHOR_NOTIFY_QUIET_STRICT") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def state_path() -> Path:
    """Where the dedupe ledger is persisted, beside the session state files."""
    from ephor.config import state_dir

    return state_dir().parent / "notify.json"


# --- transport -----------------------------------------------------------


def _post(url: str, title: str, body: str, priority: str, tag: str, click: str = "") -> bool:
    """One POST. False on any failure — a missed push never raises."""
    headers = {
        # ntfy reads these; a generic webhook ignores them and reads the body.
        "Title": title,
        "Priority": priority,
        "Tags": tag,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if click:
        headers["Click"] = click
    token = notify_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(  # noqa: S310 - destination is the user's own config
        url, data=body.encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SEC) as response:  # noqa: S310
            return bool(200 <= response.status < 300)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _notify_send(title: str, body: str) -> bool:
    """Local desktop fallback via libnotify. Best-effort, never raises."""
    import subprocess

    try:
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["/usr/bin/env", "notify-send", "-a", "ephor", title, body],
            capture_output=True,
            timeout=_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


# --- model ---------------------------------------------------------------


@dataclass(frozen=True)
class Attention:
    """One session's current claim on your attention, as seen this tick."""

    session_id: str
    reason: str
    label: str  # ticket key, or project name, or short session id
    detail: str  # one line: what is wanted and where
    url: str = ""  # click-through, when the reason came from a pull request

    @property
    def priority(self) -> str:
        return _REASON_STYLE.get(self.reason, _DEFAULT_STYLE)[0]

    @property
    def tag(self) -> str:
        return _REASON_STYLE.get(self.reason, _DEFAULT_STYLE)[1]


@dataclass
class _Record:
    """What the notifier remembers about one (session, reason)."""

    first_seen: float
    last_seen: float
    sent_at: float | None = None

    def as_json(self) -> dict[str, float | None]:
        return {"first": self.first_seen, "last": self.last_seen, "sent": self.sent_at}


@dataclass
class _Outbound:
    title: str
    body: str
    priority: str
    tag: str
    click: str = ""
    keys: tuple[str, ...] = field(default_factory=tuple)


class _Sender:
    """Serialises pushes onto one background thread.

    The dashboard's observe pass runs on the asyncio event loop, and
    :func:`_post` is a blocking urllib call with a five-second timeout. Doing
    that inline stalls every row on the board behind a slow phone. The worker
    keeps the loop free and reports failures back so the notifier can back
    off and retry rather than mark a dropped push as delivered.
    """

    def __init__(
        self, deliver: Callable[[_Outbound], bool], on_result: Callable[[_Outbound, bool], None]
    ) -> None:
        self._deliver = deliver
        self._on_result = on_result
        self._queue: queue.Queue[_Outbound | None] = queue.Queue(maxsize=64)
        self._thread = threading.Thread(target=self._run, name="ephor-notify", daemon=True)
        self._thread.start()

    def submit(self, message: _Outbound) -> None:
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            # A backlog this deep means the endpoint is wedged; dropping is
            # strictly better than growing an unbounded queue of stale alerts.
            self._on_result(message, False)

    def _run(self) -> None:
        while True:
            message = self._queue.get()
            if message is None:
                return
            try:
                ok = self._deliver(message)
            except Exception:
                log.debug("notify: delivery raised", exc_info=True)
                ok = False
            try:
                self._on_result(message, ok)
            except Exception:
                log.debug("notify: result handler raised", exc_info=True)

    def close(self) -> None:
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)


# --- notifier ------------------------------------------------------------


class Notifier:
    """Policy-filtered, deduping push notifier for sessions that need a human.

    Driven by :meth:`tick`, which is handed the *whole* board each pass. It
    needs the whole board rather than one session at a time because two of
    the noise rules — batching and the per-session cooldown — are decisions
    about the set, not the row.
    """

    def __init__(
        self,
        *,
        rearm_sec: float | None = None,
        dwell_sec: float | None = None,
        settle_sec: float | None = None,
        cooldown_sec: float | None = None,
        batch_threshold: int | None = None,
        desktop_fallback: bool = False,
        persist: bool = False,
        async_send: bool = False,
    ) -> None:
        self._rearm = (
            _env_float("EPHOR_NOTIFY_REARM_SEC", REARM_SEC) if rearm_sec is None else rearm_sec
        )
        self._dwell = (
            _env_float("EPHOR_NOTIFY_DWELL_SEC", DWELL_SEC) if dwell_sec is None else dwell_sec
        )
        self._settle = (
            _env_float("EPHOR_NOTIFY_SETTLE_SEC", SETTLE_SEC) if settle_sec is None else settle_sec
        )
        self._cooldown = (
            _env_float("EPHOR_NOTIFY_COOLDOWN_SEC", COOLDOWN_SEC)
            if cooldown_sec is None
            else cooldown_sec
        )
        self._batch_threshold = (
            _env_int("EPHOR_NOTIFY_BATCH_THRESHOLD", BATCH_THRESHOLD)
            if batch_threshold is None
            else batch_threshold
        )
        self._desktop = desktop_fallback
        self._persist = persist

        self._lock = threading.Lock()
        self._records: dict[str, _Record] = {}
        self._session_sent: dict[str, float] = {}
        self._backoff_until: float = 0.0
        self._backoff_step: float = 0.0

        if self._persist:
            self._load()

        self._sender: _Sender | None = None
        if async_send:
            self._sender = _Sender(self._deliver, self._on_result)

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        """Restore the ledger. A missing or malformed file is simply empty."""
        try:
            raw = json.loads(state_path().read_text())
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("version") != _STATE_VERSION:
            return
        now = _now()
        records = raw.get("records")
        if isinstance(records, dict):
            for key, value in records.items():
                if not isinstance(value, dict):
                    continue
                try:
                    record = _Record(
                        first_seen=float(value["first"]),
                        last_seen=float(value["last"]),
                        sent_at=None if value.get("sent") is None else float(value["sent"]),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                # Drop anything that already settled while we were down.
                if now - record.last_seen <= self._settle:
                    self._records[key] = record
        sessions = raw.get("sessions")
        if isinstance(sessions, dict):
            for sid, at in sessions.items():
                try:
                    self._session_sent[sid] = float(at)
                except (TypeError, ValueError):
                    continue

    def _save(self) -> None:
        if not self._persist:
            return
        path = state_path()
        payload = {
            "version": _STATE_VERSION,
            "records": {key: rec.as_json() for key, rec in self._records.items()},
            "sessions": dict(self._session_sent),
        }
        tmp = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, sort_keys=True))
            # Owner-only: the ledger names sessions and reasons, and an
            # atomic rename carries the mode with it.
            tmp.chmod(0o600)
            tmp.replace(path)
        except OSError:
            log.debug("notify: could not persist ledger to %s", path, exc_info=True)

    # -- state ------------------------------------------------------------

    @staticmethod
    def _key(session_id: str, reason: str) -> str:
        return f"{session_id}{_RECORD_SEP}{reason}"

    def forget(self, session_id: str) -> None:
        """Drop everything remembered about a session that has ended.

        Only an *ended* session forgets eagerly. A session that merely stopped
        needing you keeps its record until it settles, which is the whole
        reason a flapping agent no longer pushes per flap.
        """
        prefix = f"{session_id}{_RECORD_SEP}"
        with self._lock:
            for key in [k for k in self._records if k.startswith(prefix)]:
                del self._records[key]
            self._session_sent.pop(session_id, None)
        self._save()

    def _expire(self, now: float) -> None:
        """Forget records whose condition cleared longer than settle ago."""
        for key in [k for k, r in self._records.items() if now - r.last_seen > self._settle]:
            del self._records[key]
        horizon = max(self._cooldown, self._rearm, self._settle)
        for sid in [s for s, at in self._session_sent.items() if now - at > horizon]:
            del self._session_sent[sid]

    # -- decision ---------------------------------------------------------

    def _due(self, item: Attention, record: _Record, now: float, quiet: bool) -> bool:
        """Whether this item has earned a push on this tick."""
        if quiet and (_quiet_strict() or item.reason not in _QUIET_EXEMPT):
            return False
        if now - record.first_seen < self._dwell:
            return False
        if record.sent_at is None:
            last_for_session = self._session_sent.get(item.session_id)
            return last_for_session is None or now - last_for_session >= self._cooldown
        # Already announced: only a stuck-session re-nudge gets through.
        return now - record.sent_at >= self._rearm

    def tick(self, items: Sequence[Attention]) -> int:
        """Observe the whole board once. Returns the number of pushes emitted.

        ``items`` is every session that currently wants something. A session
        absent from the list is not cleared — it settles.
        """
        # Opt-in, checked before any bookkeeping: with no destination and no
        # desktop fallback there is nothing to suppress, and churning the
        # ledger would only mean the first real push is already deduped away.
        if not is_configured() and not self._desktop:
            return 0
        allowed = enabled_reasons()
        if not allowed:
            return 0
        now = _now()
        quiet = in_quiet_hours()

        with self._lock:
            self._expire(now)
            live: list[tuple[Attention, _Record]] = []
            for item in items:
                if item.reason not in allowed:
                    continue
                key = self._key(item.session_id, item.reason)
                record = self._records.get(key)
                if record is None:
                    record = _Record(first_seen=now, last_seen=now)
                    self._records[key] = record
                else:
                    record.last_seen = now
                live.append((item, record))

            if self._backoff_until and now < self._backoff_until:
                self._save()
                return 0

            due = [(item, record) for item, record in live if self._due(item, record, now, quiet)]
            if not due:
                self._save()
                return 0

            messages = self._compose(due, now)
            # Mark optimistically: the sender un-marks on failure, which is
            # what keeps a dropped push retryable without retrying at 2Hz.
            for item, record in due:
                record.sent_at = now
                self._session_sent[item.session_id] = now
            self._save()

        for message in messages:
            self._dispatch(message)
        return len(messages)

    def _compose(self, due: list[tuple[Attention, _Record]], now: float) -> list[_Outbound]:
        """One message per item, or a single digest once there are enough."""
        del now
        if len(due) >= self._batch_threshold:
            items = [item for item, _ in due]
            top = max(items, key=lambda i: _PRIORITY_RANK.get(i.priority, 2))
            lines = "\n".join(
                f"• {item.label} — {item.reason.replace('_', ' ').lower()}" for item in items
            )
            return [
                _Outbound(
                    title=f"ephor: {len(items)} sessions need you",
                    body=lines,
                    priority=top.priority,
                    tag="bell",
                    keys=tuple(self._key(i.session_id, i.reason) for i in items),
                )
            ]
        return [
            _Outbound(
                title=f"ephor: {item.label}",
                body=item.detail,
                priority=item.priority,
                tag=item.tag,
                click=item.url,
                keys=(self._key(item.session_id, item.reason),),
            )
            for item, _ in due
        ]

    # -- delivery ---------------------------------------------------------

    def _dispatch(self, message: _Outbound) -> None:
        if self._sender is not None:
            self._sender.submit(message)
            return
        self._on_result(message, self._deliver(message))

    def _deliver(self, message: _Outbound) -> bool:
        """Actually send. Runs on the worker thread when one exists."""
        sent = False
        if is_configured():
            sent = _post(
                notify_url(),
                message.title,
                message.body,
                message.priority,
                message.tag,
                message.click,
            )
        if self._desktop and not sent:
            sent = _notify_send(message.title, message.body)
        return sent

    def _on_result(self, message: _Outbound, ok: bool) -> None:
        """Arm or clear transport backoff, and un-mark a push that failed."""
        with self._lock:
            if ok:
                self._backoff_until = 0.0
                self._backoff_step = 0.0
                return
            # Nothing was delivered: let these keys try again, but not until
            # the backoff expires.
            for key in message.keys:
                record = self._records.get(key)
                if record is not None:
                    record.sent_at = None
            self._backoff_step = (
                _BACKOFF_START_SEC
                if self._backoff_step <= 0
                else min(self._backoff_step * 2, _BACKOFF_MAX_SEC)
            )
            self._backoff_until = _now() + self._backoff_step
        self._save()

    def close(self) -> None:
        """Stop the worker thread, if one is running."""
        if self._sender is not None:
            self._sender.close()
            self._sender = None

    # -- introspection ----------------------------------------------------

    def snapshot(self) -> str:
        """Debug view of the ledger, for `ephor doctor`."""
        with self._lock:
            now = _now()
            view = {
                key: {
                    "age": round(now - rec.first_seen, 1),
                    "sent": rec.sent_at is not None,
                }
                for key, rec in self._records.items()
            }
        return json.dumps(view, sort_keys=True)


def describe_config() -> list[tuple[str, str]]:
    """Human-readable current policy, for `ephor doctor`."""
    level = notify_level()
    reasons = sorted(enabled_reasons())
    quiet = (os.environ.get("EPHOR_NOTIFY_QUIET") or "").strip() or "off"
    return [
        ("level", level),
        ("reasons", ", ".join(reasons) if reasons else "(none)"),
        ("dwell", f"{_env_float('EPHOR_NOTIFY_DWELL_SEC', DWELL_SEC):.0f}s"),
        ("settle", f"{_env_float('EPHOR_NOTIFY_SETTLE_SEC', SETTLE_SEC):.0f}s"),
        ("cooldown", f"{_env_float('EPHOR_NOTIFY_COOLDOWN_SEC', COOLDOWN_SEC):.0f}s"),
        ("re-arm", f"{_env_float('EPHOR_NOTIFY_REARM_SEC', REARM_SEC):.0f}s"),
        ("batch at", str(_env_int("EPHOR_NOTIFY_BATCH_THRESHOLD", BATCH_THRESHOLD))),
        ("quiet hours", quiet + (" (strict)" if quiet != "off" and _quiet_strict() else "")),
    ]

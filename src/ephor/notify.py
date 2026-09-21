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

Two rules keep it from becoming noise, which is the only way a notifier ever
fails:

*Dedupe.* A session sitting in ``WAITING_PERMISSION`` for forty minutes is one
event, not forty minutes of events. A given (session, reason) is sent once and
not again until the session leaves that state.

*Opt-in.* No configured URL means no network call. Like :mod:`ephor.jira_api`,
this stays off until the user names a destination.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

_TIMEOUT_SEC = 5.0

# Re-arm a (session, reason) after this long even if it never cleared, so a
# genuinely stuck session nudges again on a human timescale instead of once
# and then silently forever.
REARM_SEC = 1800.0


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


def _post(url: str, title: str, body: str, priority: str, tag: str) -> bool:
    """One POST. False on any failure — a missed push never raises."""
    headers = {
        # ntfy reads these; a generic webhook ignores them and reads the body.
        "Title": title,
        "Priority": priority,
        "Tags": tag,
        "Content-Type": "text/plain; charset=utf-8",
    }
    token = notify_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(  # noqa: S310 - destination is the user's own config
        url, data=body.encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SEC) as response:  # noqa: S310
            return 200 <= response.status < 300
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


@dataclass
class _Sent:
    reason: str
    at: float


class Notifier:
    """Deduping push notifier for sessions that need a human.

    Holds its state in memory: a restart re-notifies, which is the right
    trade for a tool whose whole job is to make sure you did not miss
    something.
    """

    def __init__(self, *, rearm_sec: float = REARM_SEC, desktop_fallback: bool = False) -> None:
        self._rearm = rearm_sec
        self._desktop = desktop_fallback
        self._sent: dict[str, _Sent] = {}

    def clear(self, session_id: str) -> None:
        """Forget ``session_id``'s last notification so its next one sends."""
        self._sent.pop(session_id, None)

    def _should_send(self, session_id: str, reason: str) -> bool:
        last = self._sent.get(session_id)
        if last is None:
            return True
        if last.reason != reason:
            return True
        return (time.monotonic() - last.at) >= self._rearm

    def notify(
        self,
        session_id: str,
        reason: str,
        *,
        title: str,
        body: str,
        priority: str = "default",
        tag: str = "warning",
    ) -> bool:
        """Push once for this (session, reason). False when suppressed or failed."""
        if not self._should_send(session_id, reason):
            return False
        url = notify_url()
        sent = False
        if is_configured():
            sent = _post(url, title, body, priority, tag)
        if self._desktop and not sent:
            sent = _notify_send(title, body)
        if sent:
            self._sent[session_id] = _Sent(reason=reason, at=time.monotonic())
        return sent

    def snapshot(self) -> str:
        """Debug view of what has been sent, for `ephor doctor`."""
        return json.dumps({sid: s.reason for sid, s in self._sent.items()}, sort_keys=True)

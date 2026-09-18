"""Answering permission prompts from the dashboard instead of the pane.

At three sessions, jumping to a pane to type one character is fine. At
twenty-five it is the job: the board tells you *which* session is blocked,
you press Enter, tmux switches windows, you read the prompt, you answer, you
come back, and the next one is already waiting. The dashboard knows
everything needed to answer in place — which session, which tool, what it
wants — and the transport for answering already exists, because the hook
handler has always emitted a decision from ``$EPHOR_PENDING_DIR/<sid>.json``
when one is there.

This module writes those files.

Two shapes, because they solve different halves of the problem:

*One-shot* answers a single prompt and is consumed by the hook that reads it.
Useful once the hook is configured to wait (``EPHOR_PERMISSION_WAIT_SEC``),
and useful without that too — a decision written now answers the session's
*next* request, which is how you pre-clear a session you already trust for the
thing it is about to do.

*Standing* answers every request from one session until revoked. This is the
one that scales: "yes, this session may edit files in its own worktree for the
next twenty minutes" is a single judgement a human makes once, and today it is
re-litigated at every prompt.

Decisions are per session and never global. A blanket approval that outlived
the session that earned it would be exactly the footgun this tool exists to
prevent.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ephor.config import pending_dir

# Session ids reach here from state files the hook wrote, which already
# anchored them — but this builds a path, so it re-checks rather than trusts.
_SAFE_SID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class Decision(StrEnum):
    """What to tell the agent."""

    ALLOW = "allow"
    DENY = "deny"


# Claude Code's hook protocol and the dialects that copied it read the
# decision from `hookSpecificOutput.permissionDecision`. The flat
# `permissionDecision` key is carried alongside for the agents that read it
# at the top level — emitting both costs nothing and avoids a per-provider
# payload table for a two-key object.
def _payload(decision: Decision, reason: str) -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "permissionDecision": str(decision),
            "permissionDecisionReason": reason,
        },
        "permissionDecision": str(decision),
        "reason": reason,
    }


@dataclass(frozen=True)
class PendingDecision:
    """A decision waiting on disk for a session."""

    session_id: str
    decision: Decision
    standing: bool


def _path_for(session_id: str, *, standing: bool) -> Path | None:
    if not session_id or not _SAFE_SID.match(session_id):
        return None
    suffix = ".always.json" if standing else ".json"
    return pending_dir() / f"{session_id}{suffix}"


def _write(path: Path, payload: dict[str, object]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp.decision.")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            return False
    except OSError:
        return False
    return True


def decide(
    session_id: str,
    decision: Decision,
    *,
    standing: bool = False,
    reason: str = "answered from the ephor dashboard",
) -> bool:
    """Queue a decision for ``session_id``. False when it could not be written."""
    path = _path_for(session_id, standing=standing)
    if path is None:
        return False
    return _write(path, _payload(decision, reason))


def revoke(session_id: str) -> bool:
    """Clear both decision files for a session. True when one was removed."""
    removed = False
    for standing in (False, True):
        path = _path_for(session_id, standing=standing)
        if path is None:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed = True
    return removed


def standing_for(session_id: str) -> Decision | None:
    """The standing decision in force for ``session_id``, or None."""
    path = _path_for(session_id, standing=True)
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    value = raw.get("permissionDecision")
    try:
        return Decision(str(value))
    except ValueError:
        return None


def pending() -> dict[str, PendingDecision]:
    """Every queued decision, keyed by session id. Standing ones win.

    Used by the dashboard to show which sessions are already answered so a
    blocked row does not keep asking for input that is on its way.
    """
    directory = pending_dir()
    if not directory.is_dir():
        return {}
    found: dict[str, PendingDecision] = {}
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith(".tmp"):
            continue
        standing = path.name.endswith(".always.json")
        session_id = path.name[: -len(".always.json")] if standing else path.stem
        try:
            raw = json.loads(path.read_text())
            decision = Decision(str(raw["permissionDecision"]))
        except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError):
            continue
        if not standing and session_id in found and found[session_id].standing:
            continue
        found[session_id] = PendingDecision(
            session_id=session_id, decision=decision, standing=standing
        )
    return found


def hook_wait_sec() -> int:
    """How long the hook is configured to wait for a dashboard answer.

    Zero means the inbox can only pre-answer the *next* prompt, which the
    dashboard says out loud rather than letting the user believe a keypress
    unblocked a session it did not.
    """
    raw = os.environ.get("EPHOR_PERMISSION_WAIT_SEC") or "0"
    try:
        return max(0, min(60, int(raw)))
    except ValueError:
        return 0

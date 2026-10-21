"""``flows/runs/<run_id>.json``: one file per time a flow fired, and what became of it.

A flow firing is recorded before anything is launched, as a file whose *name*
is the whole dedupe: ``<KEY>.<flow>.<generation>``, where the generation names
the one event that fired it (a visit to a status, a pull request, a session).
Two ticks that both see the same move both try to create the same file, and
the second create fails — so a crash, a retry, or two dashboards racing can
never launch the same flow twice for the same event. There is no ledger to
keep in step with a cursor; the file *is* the claim.

After the claim, the run moves through a small state machine::

    awaiting_confirm ──approve──► claimed ──► launched ──► done | failed
          │                         │  ▲
          ├─deny──► denied          ▼  │ (retried while the generation is current)
          └─ttl / ticket moved──► expired      blocked
                                    │
          generation moved on ──► superseded      too many attempts ──► stuck

Every change after the claim is a **compare-and-set** under the
``runs/.lock`` sidecar: the file is re-read under the lock, its state checked
against what the caller expected, and only then replaced by atomic rename.
That is what stops an ``approve`` that read ``awaiting_confirm`` from
resurrecting a run a tick expired a moment earlier. The lock is separate
from ``tick.lock`` because the tick holds that one across Jira and tmux
calls, and an approve from the dashboard must not wait behind the network.

The same directory holds ``<run_id>.exit`` — the exit code a ``command =``
flow's wrapper writes when it finishes — and ``flows/payloads/<run_id>.json``
holds what the flow is told about the event that fired it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ephor import eventlog
from ephor.filelock import locked
from ephor.flows import flows_dir

RUNS_DIR = "runs"
PAYLOADS_DIR = "payloads"
PAUSED_FILE = "paused"
RUN_SCHEMA_VERSION = 1

# A launch that began (attempts was bumped) but never reported a pane is
# retried after this long, up to MAX_ATTEMPTS times, and then marked stuck.
# Ten minutes is long enough that a slow `git worktree add` is not mistaken
# for a crash, and the launch guard's own window is the same length.
CLAIM_TIMEOUT_SEC = 10 * 60.0
MAX_ATTEMPTS = 3
# How long an update waits for runs/.lock. Every holder does a read and a
# rename; a quarter of a second already means something is wedged.
_LOCK_TIMEOUT_SEC = 2.0
# Terminal runs older than this are deleted by the tick, with their payload
# and exit file. Their story stays in the event log.
PRUNE_AFTER_DAYS = 30.0

_RUN_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+\.[a-z0-9][a-z0-9-]*\.[A-Za-z0-9_-]+$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]+$")


class RunState(StrEnum):
    AWAITING_CONFIRM = "awaiting_confirm"
    CLAIMED = "claimed"
    LAUNCHED = "launched"
    BLOCKED = "blocked"  # a guard said "not now"; retried while the generation is current
    DONE = "done"
    FAILED = "failed"
    DENIED = "denied"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    STUCK = "stuck"


# States a run never leaves. Stuck is among them: it is the tick giving up,
# and only a human (or a new generation) should start the flow again.
TERMINAL_STATES = frozenset(
    {
        RunState.DONE,
        RunState.FAILED,
        RunState.DENIED,
        RunState.EXPIRED,
        RunState.SUPERSEDED,
        RunState.STUCK,
    }
)
# States the tick still has work to do on.
PENDING_STATES = frozenset(
    {RunState.AWAITING_CONFIRM, RunState.CLAIMED, RunState.BLOCKED, RunState.LAUNCHED}
)


def runs_dir() -> Path:
    return flows_dir() / RUNS_DIR


def payloads_dir() -> Path:
    return flows_dir() / PAYLOADS_DIR


def paused_path() -> Path:
    return flows_dir() / PAUSED_FILE


def _lock_path() -> Path:
    return runs_dir() / ".lock"


def _now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def age_sec(raw: str, now: datetime | None = None) -> float | None:
    """Seconds since the ISO timestamp ``raw``, or None when it is not one."""
    moment = parse_iso(raw)
    if moment is None:
        return None
    return max(0.0, ((now or _now()) - moment).total_seconds())


# --- naming -------------------------------------------------------------------


def status_token(status_id: str, status: str) -> str:
    """A filename-safe name for a status: its id, else a hash of its name.

    Jira's status ids are digits. A site that returns none falls back to the
    name, which can hold spaces and slashes, so it is hashed rather than
    pasted into a path.
    """
    if status_id and _TOKEN_RE.match(status_id):
        return status_id
    name = (status_id or status).strip().lower()
    return "n" + hashlib.sha256(name.encode()).hexdigest()[:10]


def status_generation(status_id: str, status: str, seq: int) -> str:
    """``<status>-<seq>``: one visit to one status (see :mod:`ephor.flows.snapshot`)."""
    return f"{status_token(status_id, status)}-{seq}"


def make_run_id(ticket: str, flow: str, generation: str) -> str:
    return f"{ticket.upper()}.{flow}.{generation}"


def valid_run_id(run_id: str) -> bool:
    return bool(_RUN_ID_RE.match(run_id or ""))


# --- the record ------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """One firing of one flow, and where it has got to."""

    run_id: str
    flow: str
    ticket: str
    trigger: str  # the rule's trigger key, e.g. "on_enter"
    generation: str
    state: RunState
    created_at: str
    updated_at: str
    stale: bool = False  # created after a gap longer than max_event_age
    attempts: int = 0
    reason: str = ""
    window: str = ""
    pane: str = ""
    payload_path: str = ""
    exit_code: int | None = None
    # What fired it — kept so a later tick can tell whether the generation is
    # still current, and so `ephor flows ls` can say what the run is about.
    kind: str = ""  # "start" | "command"
    title: str = ""
    from_status: str = ""
    to_status: str = ""
    from_status_id: str = ""
    status_id: str = ""
    seq: int = 0
    launched_at: str = ""
    schema_version: int = RUN_SCHEMA_VERSION

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def matched_status(self) -> str:
        """The status the rule matched: entered for on_enter, left for on_exit."""
        return self.from_status if self.trigger == "on_exit" else self.to_status

    def to_json(self) -> str:
        raw: dict[str, Any] = asdict(self)
        raw["state"] = str(self.state)
        return json.dumps(raw, sort_keys=True, indent=1)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Run | None:
        """Decode a stored run, or None when it is not the shape we write."""
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not valid_run_id(run_id):
            return None
        try:
            state = RunState(str(data.get("state")))
        except ValueError:
            return None
        exit_code = data.get("exit_code")
        return cls(
            run_id=run_id,
            flow=_str(data.get("flow")),
            ticket=_str(data.get("ticket")),
            trigger=_str(data.get("trigger")),
            generation=_str(data.get("generation")),
            state=state,
            created_at=_str(data.get("created_at")),
            updated_at=_str(data.get("updated_at")),
            stale=data.get("stale") is True,
            attempts=_int(data.get("attempts")),
            reason=_str(data.get("reason")),
            window=_str(data.get("window")),
            pane=_str(data.get("pane")),
            payload_path=_str(data.get("payload_path")),
            exit_code=exit_code
            if isinstance(exit_code, int) and not isinstance(exit_code, bool)
            else None,
            kind=_str(data.get("kind")),
            title=_str(data.get("title")),
            from_status=_str(data.get("from_status")),
            to_status=_str(data.get("to_status")),
            from_status_id=_str(data.get("from_status_id")),
            status_id=_str(data.get("status_id")),
            seq=_int(data.get("seq")),
            launched_at=_str(data.get("launched_at")),
            schema_version=_int(data.get("schema_version")) or RUN_SCHEMA_VERSION,
        )


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


# --- storage ----------------------------------------------------------------------


def run_path(run_id: str) -> Path:
    return runs_dir() / f"{run_id}.json"


def exit_path(run_id: str) -> Path:
    return runs_dir() / f"{run_id}.exit"


def exit_tmp_path(run_id: str) -> Path:
    """Where a command wrapper writes the exit status before renaming it into place."""
    final = exit_path(run_id)
    return final.with_name(f".tmp.{final.name}")


def clear_exit(run_id: str) -> None:
    """Remove any exit status an earlier attempt left, so a new one is not judged by it."""
    for path in (exit_path(run_id), exit_tmp_path(run_id)):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def payload_path(run_id: str) -> Path:
    return payloads_dir() / f"{run_id}.json"


def _write_tmp(directory: Path, text: str, prefix: str) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=prefix)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Path(tmp)


def create(run: Run) -> bool:
    """Claim ``run``'s id. True only for the one caller that created the file.

    The file is written in full under a temporary name and then hard-linked
    into place: ``link`` fails when the name exists, exactly like
    ``O_EXCL``, and a reader can never see a half-written claim. A
    filesystem without hard links falls back to an ``O_EXCL`` create.
    """
    if not valid_run_id(run.run_id):
        return False
    target = run_path(run.run_id)
    try:
        tmp = _write_tmp(target.parent, run.to_json(), ".tmp.run.")
    except OSError:
        return False
    try:
        os.link(tmp, target)
    except FileExistsError:
        return False
    except OSError:
        return _create_excl(target, run)
    finally:
        tmp.unlink(missing_ok=True)
    return True


def _create_excl(target: Path, run: Run) -> bool:
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(run.to_json())
    return True


def exists(run_id: str) -> bool:
    return run_path(run_id).exists()


def load(run_id: str) -> Run | None:
    if not valid_run_id(run_id):
        return None
    try:
        data = json.loads(run_path(run_id).read_text())
    except (OSError, ValueError, RecursionError):
        return None
    return Run.from_dict(data) if isinstance(data, dict) else None


def list_runs() -> list[Run]:
    """Every readable run, newest first (by creation, then id)."""
    directory = runs_dir()
    if not directory.is_dir():
        return []
    found: list[Run] = []
    for path in directory.glob("*.json"):
        if path.name.startswith("."):
            continue
        run = load(path.name[: -len(".json")])
        if run is not None:
            found.append(run)
    found.sort(key=lambda r: (r.created_at, r.run_id), reverse=True)
    return found


def _save(run: Run) -> bool:
    target = run_path(run.run_id)
    try:
        tmp = _write_tmp(target.parent, run.to_json(), ".tmp.run.")
        os.replace(tmp, target)
    except OSError:
        return False
    return True


def update(
    run_id: str,
    *,
    expect: Iterable[RunState],
    now: datetime | None = None,
    **changes: Any,
) -> Run | None:
    """Compare-and-set: apply ``changes`` only if the run is still in ``expect``.

    Re-reads the run under ``runs/.lock``. Returns the updated run, or None
    when the lock could not be taken, the run is gone, or its state is no
    longer one the caller expected — in which case the caller's view was
    stale and it must not act on it.
    """
    allowed = frozenset(expect)
    with locked(_lock_path(), timeout=_LOCK_TIMEOUT_SEC) as acquired:
        if not acquired:
            return None
        current = load(run_id)
        if current is None or current.state not in allowed:
            return None
        updated = replace(current, updated_at=iso(now or _now()), **changes)
        if not _save(updated):
            return None
        return updated


# --- approve / deny ------------------------------------------------------------------


def resolve(ref: str, candidates: Iterable[Run] | None = None) -> tuple[Run | None, str]:
    """The run ``ref`` names — its full id or a unique prefix — and an error if not."""
    wanted = (ref or "").strip()
    if not wanted:
        return None, "no run id given"
    pool = list(candidates) if candidates is not None else list_runs()
    exact = [r for r in pool if r.run_id == wanted]
    if exact:
        return exact[0], ""
    # Ticket keys are upper case and flow names lower case; accept either
    # half typed in the "wrong" case rather than reporting no match.
    low = wanted.lower()

    def _hit(run_id: str) -> bool:
        rid = run_id.lower()
        if not rid.startswith(low):
            return False
        ticket = rid.split(".", 1)[0]
        # The ticket key must be typed whole: "DR-1" never reaches DR-12,
        # because approving an agent onto a ticket nobody named is the one
        # thing a prefix must never do.
        return len(low) > len(ticket) or low == ticket

    matches = [r for r in pool if _hit(r.run_id)]
    if not matches:
        return None, f"no run matches {wanted!r}"
    if len(matches) > 1:
        # "The one still waiting for you" is only a fair reading of a prefix
        # when every match is about the same ticket.
        pending = [r for r in matches if r.state is RunState.AWAITING_CONFIRM]
        if len(pending) == 1 and len({r.ticket for r in matches}) == 1:
            return pending[0], ""
        names = ", ".join(r.run_id for r in matches[:5])
        more = "" if len(matches) <= 5 else f" and {len(matches) - 5} more"
        return None, f"{wanted!r} matches {len(matches)} runs ({names}{more})"
    return matches[0], ""


def approve(run_id: str) -> tuple[Run | None, str]:
    """``awaiting_confirm`` → ``claimed``; the next tick dispatches it."""
    updated = update(
        run_id, expect={RunState.AWAITING_CONFIRM}, state=RunState.CLAIMED, reason="approved"
    )
    if updated is None:
        return None, _refusal(run_id, "approve")
    return updated, f"approved {run_id} — the next tick dispatches it"


def deny(run_id: str) -> tuple[Run | None, str]:
    """``awaiting_confirm`` → ``denied``."""
    updated = update(
        run_id, expect={RunState.AWAITING_CONFIRM}, state=RunState.DENIED, reason="denied"
    )
    if updated is None:
        return None, _refusal(run_id, "deny")
    log_event(eventlog.EventKind.FLOW_CANCELLED, updated, "denied")
    return updated, f"denied {run_id}"


def _refusal(run_id: str, verb: str) -> str:
    current = load(run_id)
    if current is None:
        return f"cannot {verb} {run_id}: no such run"
    if current.state is not RunState.AWAITING_CONFIRM:
        return f"cannot {verb} {run_id}: it is {current.state}, not awaiting_confirm"
    return f"cannot {verb} {run_id}: the run file is locked — try again"


# --- events, payloads, exit codes, pause ---------------------------------------------


def log_event(kind: eventlog.EventKind, run: Run, reason: str = "") -> None:
    """One FLOW_* event for ``run`` (extra: run_id, flow, generation, reason)."""
    eventlog.append(
        kind,
        ticket=run.ticket,
        detail=f"{run.flow}: {reason or run.state}",
        run_id=run.run_id,
        flow=run.flow,
        generation=run.generation,
        reason=reason or run.reason,
        state=str(run.state),
    )


def write_payload(run_id: str, payload: Mapping[str, Any]) -> Path | None:
    """Write the trigger payload a flow reads (0600, atomic). None on failure."""
    target = payload_path(run_id)
    try:
        tmp = _write_tmp(
            target.parent, json.dumps(dict(payload), sort_keys=True, indent=1), ".tmp.payload."
        )
        os.replace(tmp, target)
    except OSError:
        return None
    return target


def next_seq_by_ticket(all_runs: Iterable[Run] | None = None) -> dict[str, int]:
    """ticket -> one past the highest ``seq`` any of its run files records.

    A snapshot rebuilt from nothing (snapshot.json lost or unreadable) seeds
    its tickets from this, so a later visit cannot reuse the generation —
    and so the run id — of a visit whose run file is still on disk.
    """
    floor: dict[str, int] = {}
    for run in all_runs if all_runs is not None else list_runs():
        if run.ticket:
            floor[run.ticket] = max(floor.get(run.ticket, 0), run.seq + 1)
    return floor


def read_exit(run_id: str) -> int | None:
    """The exit code a command flow's wrapper recorded, or None if none yet."""
    try:
        raw = exit_path(run_id).read_text().strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def is_paused() -> bool:
    return paused_path().exists()


def pause() -> bool:
    try:
        flows_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
        paused_path().touch(mode=0o600, exist_ok=True)
    except OSError:
        return False
    return True


def resume() -> bool:
    try:
        paused_path().unlink(missing_ok=True)
    except OSError:
        return False
    return True


def prune(now: datetime | None = None, max_age_days: float = PRUNE_AFTER_DAYS) -> int:
    """Delete terminal runs untouched for ``max_age_days``, with their side files."""
    cutoff = max_age_days * 86400.0
    removed = 0
    for run in list_runs():
        if not run.terminal:
            continue
        age = age_sec(run.updated_at, now)
        if age is None or age < cutoff:
            continue
        with contextlib.suppress(OSError):
            run_path(run.run_id).unlink()
            removed += 1
        with contextlib.suppress(OSError):
            exit_path(run.run_id).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            payload_path(run.run_id).unlink(missing_ok=True)
    return removed


# --- badges -------------------------------------------------------------------------------

# How long a stuck run keeps its badge. Stuck is terminal, so without a limit
# one bad afternoon would mark a ticket forever.
STUCK_BADGE_SEC = 24 * 3600.0

# Which run a ticket's badge shows when it has several: the one that needs
# you first, then the one doing something.
_BADGE_PRIORITY = {
    RunState.AWAITING_CONFIRM: 0,
    RunState.STUCK: 1,
    RunState.BLOCKED: 2,
    RunState.CLAIMED: 3,
    RunState.LAUNCHED: 4,
}


def badge(run: Run) -> str:
    """The short label a row shows for ``run`` (``⏸ review?``, ``▶ review`` …)."""
    if run.state is RunState.AWAITING_CONFIRM:
        return f"⏸ {run.flow}?" + (" (stale)" if run.stale else "")
    if run.state is RunState.CLAIMED:
        return f"… {run.flow}"
    if run.state is RunState.BLOCKED:
        return f"⊘ {run.flow}"
    if run.state is RunState.LAUNCHED:
        return f"▶ {run.flow}"
    if run.state is RunState.STUCK:
        return f"⚠ {run.flow} stuck"
    return ""


def shows_badge(run: Run, now: datetime | None = None) -> bool:
    if run.state in PENDING_STATES:
        return True
    if run.state is RunState.STUCK:
        age = age_sec(run.updated_at, now)
        return age is not None and age < STUCK_BADGE_SEC
    return False


def badges_by_ticket(all_runs: Iterable[Run], now: datetime | None = None) -> dict[str, str]:
    """ticket -> the badge of its most pressing run, for tickets that have one."""
    best: dict[str, Run] = {}
    for run in all_runs:
        if not shows_badge(run, now):
            continue
        held = best.get(run.ticket)
        if held is None or (_BADGE_PRIORITY[run.state], held.created_at) < (
            _BADGE_PRIORITY[held.state],
            run.created_at,
        ):
            best[run.ticket] = run
    return {ticket: badge(run) for ticket, run in best.items()}

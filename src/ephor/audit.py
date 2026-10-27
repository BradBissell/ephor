"""What a session cost, how fast it went, and whether it actually shipped.

The dashboard answers *what is happening*; the event log answers *what
happened*. Neither answers the question you ask at the end of a week with
twenty sessions behind you — **was any of that worth it?**

Three sources already hold the answer and nothing joined them:

* the agent's own transcript (``~/.claude/projects/<cwd>/<session>.jsonl``),
  whose every assistant line carries ``message.usage`` — the four token
  classes, the model, the timestamp;
* :mod:`ephor.work_items`, which knows the ticket, the PR and its state, and
  — crucially — ``session_ids``, the join key;
* :mod:`ephor.eventlog`, whose timestamped transitions turn one undifferentiated
  wall-clock span into phases: time to PR, time to green, time to merge.

Three things this module refuses to pretend:

**Cost here is not a bill.** Claude Code on a subscription is not billed per
token. Every dollar figure is *equivalent API cost* — what the same traffic
would have cost at list price — which is a good comparator between sessions
and a bad number to quote at a finance team. Real quota lives in
:mod:`ephor.account_usage`.

**Summed tokens are meaningless.** A measured session here read 253M cached
tokens against 1,422 uncached input ones. Anything that adds those together
is measuring conversation length, not work, so the four classes are kept
apart all the way to the report and the cache hit ratio is always shown.

**"Did it achieve the goal" is not directly observable.** What *is* observable
is whether the PR merged, whether CI was green, and how many review rounds it
took to get there. Those proxies are free and honest; :func:`judge` offers the
subjective read for spot checks, opt-in, because it costs real money and a
model grading work against a ticket it helped write is partly circular.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ephor import eventlog, work_items
from ephor.config import state_dir

log = logging.getLogger(__name__)

# Bumped independently of the session and work schemas: a third writer, a
# third lifetime, and no hook contract to honour.
AUDIT_SCHEMA_VERSION = 1

# A gap longer than this between two turns is not the agent working, it is
# you asleep. Wall clock includes it; `active_sec` does not. Ten minutes is
# comfortably longer than any single tool call and far shorter than a break.
IDLE_GAP_SEC = 600.0

# How long a PR sits untouched before "open" becomes "stalled", and how long
# a ticket goes without a PR before it is called abandoned.
STALE_DAYS = 7.0

# Cache writes cost 1.25x the input rate, reads 0.1x — unless a model prices
# reads explicitly (see PRICES).
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1

# Where `claude` keeps transcripts. Overridable so tests — and anyone whose
# agent writes elsewhere — are not pinned to a real home directory.
ENV_TRANSCRIPT_ROOT = "EPHOR_TRANSCRIPT_ROOT"

# Session ids are interpolated into a glob below. Anchor them to the same
# alphabet the hook handler already enforces so a hostile id cannot escape
# into a pattern that matches somebody else's transcript.
_SAFE_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# `detail` on a transition event reads "#123: BEFORE → AFTER". We only ever
# want the state it landed in.
_TRANSITION_TAIL = re.compile(r"→\s*(\S+)\s*$")


@dataclass(frozen=True)
class Price:
    """List price for one model, in dollars per million tokens."""

    input_per_mtok: float
    output_per_mtok: float
    # Set only where a model prices cache reads off the input-rate default.
    cache_read_per_mtok: float | None = None

    @property
    def cache_read(self) -> float:
        if self.cache_read_per_mtok is not None:
            return self.cache_read_per_mtok
        return self.input_per_mtok * CACHE_READ_MULTIPLIER

    @property
    def cache_write(self) -> float:
        return self.input_per_mtok * CACHE_WRITE_MULTIPLIER


# Anthropic first-party list prices. Partner platforms (Bedrock, Vertex) bill
# separately and are deliberately not modelled — this table exists to compare
# sessions with each other, not to reproduce an invoice.
PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(10.0, 50.0, cache_read_per_mtok=0.25),
    "claude-mythos-5-1": Price(10.0, 50.0, cache_read_per_mtok=0.25),
    "claude-fable-5": Price(10.0, 50.0),
    "claude-opus-5": Price(5.0, 25.0),
    "claude-opus-4-8": Price(5.0, 25.0),
    "claude-opus-4-7": Price(5.0, 25.0),
    "claude-opus-4-6": Price(5.0, 25.0),
    "claude-sonnet-5": Price(2.0, 10.0),
    "claude-sonnet-4-6": Price(3.0, 15.0),
    "claude-haiku-4-5": Price(1.0, 5.0),
}


def price_for(model: str) -> Price | None:
    """List price for ``model``, or None when we have never heard of it.

    Returning None rather than guessing is the point: an unpriced model's
    tokens still get counted, but its dollars are excluded and the report
    says so. A silently-assumed price is worse than a visible gap, because
    only one of the two gets questioned.
    """
    if not model:
        return None
    exact = PRICES.get(model)
    if exact is not None:
        return exact
    # Tolerate a dated or suffixed id (`claude-opus-5-20260401`) by matching
    # the longest known prefix — new snapshots of a known model price like it.
    best: Price | None = None
    best_len = 0
    for known, price in PRICES.items():
        if model.startswith(known) and len(known) > best_len:
            best, best_len = price, len(known)
    return best


def transcript_root() -> Path:
    """Directory holding per-project transcript folders."""
    raw = os.environ.get(ENV_TRANSCRIPT_ROOT)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".claude" / "projects"


def find_transcript(session_id: str) -> Path | None:
    """The transcript for ``session_id``, found by glob rather than by rule.

    The folder name is the session's cwd with its separators mangled, and
    reimplementing that encoding means keeping it in sync with a format we do
    not own — the same trap :mod:`ephor.hooks` hit with grok's session paths.
    Globbing the id, which is unique, sidesteps the question entirely.
    """
    if not _SAFE_SESSION.match(session_id or ""):
        return None
    try:
        matches = sorted(transcript_root().glob(f"*/{session_id}.jsonl"))
    except OSError:
        return None
    return matches[0] if matches else None


@dataclass(frozen=True)
class TranscriptStats:
    """What one transcript says about the work that produced it."""

    session_id: str = ""
    cwd: str = ""
    git_branch: str = ""
    turns: int = 0
    sidechain_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    equiv_cost_usd: float = 0.0
    # Models whose tokens were counted but whose dollars could not be.
    unpriced_models: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    effort: str = ""
    tool_counts: dict[str, int] = field(default_factory=dict)
    truncated_turns: int = 0  # stop_reason == max_tokens
    first_at: str = ""
    last_at: str = ""
    wall_clock_sec: float = 0.0
    active_sec: float = 0.0

    @property
    def cache_hit_ratio(self) -> float:
        """Share of read context served from cache. 0.0 when nothing was read."""
        total = self.cache_read_tokens + self.input_tokens + self.cache_write_tokens
        return self.cache_read_tokens / total if total else 0.0

    @property
    def output_per_active_min(self) -> float:
        """Output tokens per minute of non-idle time — the honest speed figure."""
        return self.output_tokens / (self.active_sec / 60.0) if self.active_sec > 0 else 0.0


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def analyze_transcript(path: Path) -> TranscriptStats:
    """Parse one transcript into token, cost and timing totals.

    Every failure mode here is a partial read, never an exception: transcripts
    are written by another process and may be appended to mid-parse, so a
    truncated final line is normal rather than exceptional.
    """
    turns = sidechain = truncated = 0
    tok_in = tok_out = tok_cw = tok_cr = 0
    cost = 0.0
    unpriced: set[str] = set()
    models: dict[str, int] = {}
    tools: dict[str, int] = {}
    stamps: list[datetime] = []
    cwd = branch = effort = ""

    try:
        handle = path.open()
    except OSError:
        return TranscriptStats(session_id=path.stem)

    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue

            when = _parse_ts(str(entry.get("timestamp") or ""))
            if when is not None:
                stamps.append(when)
            cwd = cwd or str(entry.get("cwd") or "")
            branch = branch or str(entry.get("gitBranch") or "")
            effort = str(entry.get("effort") or "") or effort

            if entry.get("type") != "assistant":
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue

            turns += 1
            if entry.get("isSidechain"):
                sidechain += 1
            if entry.get("stopReason") == "max_tokens":
                truncated += 1

            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = str(block.get("name") or "?")
                    tools[name] = tools.get(name, 0) + 1

            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            model = str(message.get("model") or "")
            models[model] = models.get(model, 0) + 1

            def _count(key: str) -> int:
                value = usage.get(key)  # noqa: B023 - consumed before next loop
                return value if isinstance(value, int) and value >= 0 else 0

            this_in = _count("input_tokens")
            this_out = _count("output_tokens")
            this_cw = _count("cache_creation_input_tokens")
            this_cr = _count("cache_read_input_tokens")
            tok_in += this_in
            tok_out += this_out
            tok_cw += this_cw
            tok_cr += this_cr

            price = price_for(model)
            if price is None:
                unpriced.add(model or "?")
                continue
            cost += (
                this_in * price.input_per_mtok
                + this_cw * price.cache_write
                + this_cr * price.cache_read
                + this_out * price.output_per_mtok
            ) / 1_000_000

    stamps.sort()
    wall = (stamps[-1] - stamps[0]).total_seconds() if len(stamps) > 1 else 0.0
    active = 0.0
    for earlier, later in itertools.pairwise(stamps):
        gap = (later - earlier).total_seconds()
        if 0 <= gap <= IDLE_GAP_SEC:
            active += gap

    return TranscriptStats(
        session_id=path.stem,
        cwd=cwd,
        git_branch=branch,
        turns=turns,
        sidechain_turns=sidechain,
        input_tokens=tok_in,
        output_tokens=tok_out,
        cache_write_tokens=tok_cw,
        cache_read_tokens=tok_cr,
        equiv_cost_usd=round(cost, 4),
        unpriced_models=tuple(sorted(unpriced)),
        models=tuple(sorted(models, key=lambda m: -models[m])),
        effort=effort,
        tool_counts=tools,
        truncated_turns=truncated,
        first_at=stamps[0].isoformat(timespec="seconds") if stamps else "",
        last_at=stamps[-1].isoformat(timespec="seconds") if stamps else "",
        wall_clock_sec=round(wall, 1),
        active_sec=round(active, 1),
    )


class Outcome(StrEnum):
    """How a chunk of work ended, on the cheap-and-objective proxy ladder.

    Deliberately ordered from best to worst so a report can sort on it, and
    deliberately free — every value here is derived from data ephor already
    holds. The subjective read is :func:`judge`, and it is opt-in.
    """

    SHIPPED_CLEAN = "shipped_clean"  # merged, CI never failed, <=1 review round
    SHIPPED_REWORK = "shipped_rework"  # merged, but it took work to get there
    OPEN = "open"  # PR exists, still moving
    STALLED = "stalled"  # PR open and untouched past STALE_DAYS
    CLOSED = "closed"  # PR closed unmerged — a deliberate abandonment
    ABANDONED = "abandoned"  # no PR at all, and gone quiet
    UNKNOWN = "unknown"  # no work record to join against


@dataclass(frozen=True)
class PhaseMetrics:
    """Durations and rework counts recovered from the event log."""

    time_to_pr_sec: float | None = None
    time_to_merge_sec: float | None = None
    review_rounds: int = 0
    ci_failures: int = 0


def _transition_target(detail: str) -> str:
    """The state a transition landed in, read off its `A → B` detail string."""
    match = _TRANSITION_TAIL.search(detail or "")
    return match.group(1) if match else ""


def phase_metrics(events: list[eventlog.Event]) -> PhaseMetrics:
    """Reduce one ticket's event tape to durations and rework counts."""
    started: datetime | None = None
    linked: datetime | None = None
    merged: datetime | None = None
    reviews = ci_fail = 0

    for event in events:
        when = _parse_ts(event.at)
        if event.kind is eventlog.EventKind.SESSION_STARTED and started is None:
            started = when
        elif event.kind is eventlog.EventKind.PR_LINKED and linked is None:
            linked = when
        elif event.kind is eventlog.EventKind.PR_STATE_CHANGED:
            if _transition_target(event.detail).upper() == "MERGED" and merged is None:
                merged = when
        elif (
            event.kind is eventlog.EventKind.REVIEW_CHANGED
            and _transition_target(event.detail).upper() == "CHANGES_REQUESTED"
        ):
            reviews += 1
        elif (
            event.kind is eventlog.EventKind.CI_CHANGED
            and _transition_target(event.detail).upper() == "FAILING"
        ):
            ci_fail += 1

    to_pr = (linked - started).total_seconds() if started and linked else None
    # Merge time is measured from the PR appearing, not from the session
    # starting: waiting on review is not the agent being slow.
    to_merge = (merged - linked).total_seconds() if linked and merged else None
    return PhaseMetrics(
        time_to_pr_sec=to_pr,
        time_to_merge_sec=to_merge,
        review_rounds=reviews,
        ci_failures=ci_fail,
    )


def classify(
    item: work_items.WorkItem | None,
    phases: PhaseMetrics,
    *,
    now: float | None = None,
    stale_days: float = STALE_DAYS,
) -> Outcome:
    """Place a work item on the outcome ladder. Never raises, never guesses."""
    if item is None:
        return Outcome.UNKNOWN
    state = (item.pr_state or "").upper()
    if state == "MERGED":
        clean = phases.ci_failures == 0 and phases.review_rounds <= 1
        return Outcome.SHIPPED_CLEAN if clean else Outcome.SHIPPED_REWORK
    if state == "CLOSED":
        return Outcome.CLOSED

    last = _parse_ts(item.last_seen)
    reference = datetime.fromtimestamp(now, UTC) if now is not None else datetime.now(UTC)
    quiet = last is not None and (reference - last).total_seconds() > stale_days * 86400
    if state == "OPEN":
        return Outcome.STALLED if quiet else Outcome.OPEN
    # No PR at all.
    return Outcome.ABANDONED if quiet else Outcome.OPEN


@dataclass(frozen=True)
class SessionAudit:
    """One session's performance record, transcript joined to outcome."""

    session_id: str
    ticket: str = ""
    repo: str = ""
    provider: str = "claude"
    models: tuple[str, ...] = ()
    effort: str = ""
    turns: int = 0
    sidechain_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    equiv_cost_usd: float = 0.0
    unpriced_models: tuple[str, ...] = ()
    wall_clock_sec: float = 0.0
    active_sec: float = 0.0
    truncated_turns: int = 0
    tool_counts: dict[str, int] = field(default_factory=dict)
    pr_number: int | None = None
    pr_state: str = ""
    review_rounds: int = 0
    ci_failures: int = 0
    outcome: str = Outcome.UNKNOWN
    time_to_pr_sec: float | None = None
    time_to_merge_sec: float | None = None
    first_at: str = ""
    last_at: str = ""
    judged_met: bool | None = None  # set only by an explicit `judge` run
    judged_note: str = ""
    schema_version: int = AUDIT_SCHEMA_VERSION

    @property
    def cache_hit_ratio(self) -> float:
        total = self.cache_read_tokens + self.input_tokens + self.cache_write_tokens
        return self.cache_read_tokens / total if total else 0.0

    @property
    def shipped(self) -> bool:
        return self.outcome in (Outcome.SHIPPED_CLEAN, Outcome.SHIPPED_REWORK)

    def to_json(self) -> str:
        raw: dict[str, Any] = asdict(self)
        raw["models"] = list(self.models)
        raw["unpriced_models"] = list(self.unpriced_models)
        return json.dumps(raw, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionAudit:
        """Build from a stored record, tolerating fields a later version adds."""
        sid = data.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("audit record missing session_id")

        def _seq(name: str) -> tuple[str, ...]:
            value = data.get(name)
            return tuple(str(v) for v in value) if isinstance(value, list) else ()

        def _num(name: str) -> float:
            value = data.get(name)
            return float(value) if isinstance(value, int | float) else 0.0

        def _opt(name: str) -> float | None:
            value = data.get(name)
            return float(value) if isinstance(value, int | float) else None

        tools = data.get("tool_counts")
        judged = data.get("judged_met")
        return cls(
            session_id=sid,
            ticket=str(data.get("ticket") or ""),
            repo=str(data.get("repo") or ""),
            provider=str(data.get("provider") or "claude"),
            models=_seq("models"),
            effort=str(data.get("effort") or ""),
            turns=int(_num("turns")),
            sidechain_turns=int(_num("sidechain_turns")),
            input_tokens=int(_num("input_tokens")),
            output_tokens=int(_num("output_tokens")),
            cache_write_tokens=int(_num("cache_write_tokens")),
            cache_read_tokens=int(_num("cache_read_tokens")),
            equiv_cost_usd=_num("equiv_cost_usd"),
            unpriced_models=_seq("unpriced_models"),
            wall_clock_sec=_num("wall_clock_sec"),
            active_sec=_num("active_sec"),
            truncated_turns=int(_num("truncated_turns")),
            tool_counts={str(k): int(v) for k, v in tools.items()}
            if isinstance(tools, dict)
            else {},
            pr_number=data.get("pr_number") if isinstance(data.get("pr_number"), int) else None,
            pr_state=str(data.get("pr_state") or ""),
            review_rounds=int(_num("review_rounds")),
            ci_failures=int(_num("ci_failures")),
            outcome=str(data.get("outcome") or Outcome.UNKNOWN),
            time_to_pr_sec=_opt("time_to_pr_sec"),
            time_to_merge_sec=_opt("time_to_merge_sec"),
            first_at=str(data.get("first_at") or ""),
            last_at=str(data.get("last_at") or ""),
            judged_met=judged if isinstance(judged, bool) else None,
            judged_note=str(data.get("judged_note") or ""),
            schema_version=int(_num("schema_version")) or AUDIT_SCHEMA_VERSION,
        )


def _session_to_ticket() -> dict[str, str]:
    """Reverse index of the work store: which ticket each session touched."""
    index: dict[str, str] = {}
    for item in work_items.load_all().values():
        for sid in item.session_ids:
            index.setdefault(sid, item.key)
    return index


def audit_session(
    session_id: str,
    *,
    ticket: str = "",
    item: work_items.WorkItem | None = None,
    now: float | None = None,
) -> SessionAudit | None:
    """Build one session's audit from its transcript, work record and events.

    Returns None when there is no transcript to read — an audit with no token
    data would be a row of zeroes claiming a session cost nothing.
    """
    path = find_transcript(session_id)
    if path is None:
        return None
    stats = analyze_transcript(path)
    if item is None and ticket:
        item = work_items.load(ticket)
    events = eventlog.read(ticket=ticket) if ticket else eventlog.read(session_id=session_id)
    phases = phase_metrics(events)
    outcome = classify(item, phases, now=now)
    return SessionAudit(
        session_id=session_id,
        ticket=ticket,
        repo=(item.repo if item else "") or Path(stats.cwd).name,
        models=stats.models,
        effort=stats.effort,
        turns=stats.turns,
        sidechain_turns=stats.sidechain_turns,
        input_tokens=stats.input_tokens,
        output_tokens=stats.output_tokens,
        cache_write_tokens=stats.cache_write_tokens,
        cache_read_tokens=stats.cache_read_tokens,
        equiv_cost_usd=stats.equiv_cost_usd,
        unpriced_models=stats.unpriced_models,
        wall_clock_sec=stats.wall_clock_sec,
        active_sec=stats.active_sec,
        truncated_turns=stats.truncated_turns,
        tool_counts=stats.tool_counts,
        pr_number=item.pr_number if item else None,
        pr_state=item.pr_state if item else "",
        review_rounds=phases.review_rounds,
        ci_failures=phases.ci_failures,
        outcome=str(outcome),
        time_to_pr_sec=phases.time_to_pr_sec,
        time_to_merge_sec=phases.time_to_merge_sec,
        first_at=stats.first_at,
        last_at=stats.last_at,
    )


def collect(
    *,
    since_sec: float | None = None,
    ticket: str | None = None,
    now: float | None = None,
) -> list[SessionAudit]:
    """Every auditable session, newest first.

    Live transcripts are the source of truth; persisted records
    (:func:`record`) fill in sessions whose transcript has since been cleaned
    up. Live wins on conflict — it is strictly fresher.
    """
    index = _session_to_ticket()
    items = work_items.load_all()
    want = ticket.upper() if ticket else None
    cutoff = (time.time() - since_sec) if since_sec is not None else None

    audits: dict[str, SessionAudit] = {}
    for stored in load_records():
        if want and stored.ticket.upper() != want:
            continue
        audits[stored.session_id] = stored

    try:
        candidates = sorted(transcript_root().glob("*/*.jsonl"))
    except OSError:
        candidates = []
    for path in candidates:
        sid = path.stem
        # mtime before parse: a whole-history scan that opens every transcript
        # to discover it is three months old wastes most of its work.
        if cutoff is not None:
            try:
                if path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
        key = index.get(sid, "")
        if want and key.upper() != want:
            continue
        built = audit_session(sid, ticket=key, item=items.get(key), now=now)
        if built is not None:
            audits[sid] = built

    return sorted(audits.values(), key=lambda a: a.last_at, reverse=True)


# ---------------------------------------------------------------------------
# Persistence — the archive that outlives transcripts and log rotation.
# ---------------------------------------------------------------------------


def audit_path() -> Path:
    """Where audit snapshots live (beside the session state dir)."""
    return state_dir().parent / "audit.ndjson"


def record(audit: SessionAudit) -> bool:
    """Snapshot one audit. False on any failure — never raises at the caller.

    Append-only and last-wins: a session re-audited after its PR merged writes
    a second, better line, and :func:`load_records` keeps the newest per
    session. That is cheaper and far safer under concurrent writers than
    rewriting a file in place, and it matches :mod:`ephor.eventlog`.
    """
    path = audit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        line = audit.to_json() + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError):
        return False
    return True


def record_all(audits: list[SessionAudit]) -> int:
    """Snapshot many audits. Returns how many were written."""
    return sum(1 for audit in audits if record(audit))


def load_records() -> list[SessionAudit]:
    """Stored snapshots, newest-per-session. Corrupt lines are skipped."""
    newest: dict[str, SessionAudit] = {}
    try:
        raw = audit_path().read_text()
    except OSError:
        return []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        try:
            audit = SessionAudit.from_dict(parsed)
        except (ValueError, TypeError):
            continue
        # Later lines win: the file is append-only and ordered by write time.
        newest[audit.session_id] = audit
    return list(newest.values())


def compact(keep: list[SessionAudit] | None = None) -> int:
    """Rewrite the store with one line per session. Returns lines kept.

    Append-only means a session re-audited nightly accumulates a line a night.
    None of them are wrong, but the file grows without bound, so this collapses
    it — atomically, via rename, so a reader never sees a half-written store.
    """
    records = keep if keep is not None else load_records()
    path = audit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp.audit.")
        try:
            with os.fdopen(fd, "w") as handle:
                for audit in records:
                    handle.write(audit.to_json() + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            return 0
    except OSError:
        return 0
    return len(records)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Summary:
    """Fleet-level totals. The headline is cost per merged PR, not per session."""

    sessions: int = 0
    tickets: int = 0
    equiv_cost_usd: float = 0.0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    input_tokens: int = 0
    cache_write_tokens: int = 0
    active_sec: float = 0.0
    wall_clock_sec: float = 0.0
    merged: int = 0
    shipped_clean: int = 0
    shipped_rework: int = 0
    stalled: int = 0
    abandoned: int = 0
    closed: int = 0
    open_: int = 0
    unknown: int = 0
    # Cost of the sessions that actually produced merged PRs, as opposed to
    # everything spent in the window.
    merged_cost_usd: float = 0.0
    unpriced_models: tuple[str, ...] = ()

    @property
    def cost_per_merged_pr(self) -> float | None:
        """Unit economics: what the work that shipped cost, per shipped PR.

        Charges merged PRs only for the sessions that produced them. This is
        the number to compare across weeks — it does not move just because
        more work happens to be in flight on the day you run the report.
        None when nothing merged; zero would claim shipping was free.
        """
        return self.merged_cost_usd / self.merged if self.merged else None

    @property
    def fleet_cost_per_merged_pr(self) -> float | None:
        """Burn ratio: *everything* spent in the window over what shipped.

        Always at least :attr:`cost_per_merged_pr`, and usually far above it,
        because the window is full of work that has not landed yet. Useful as
        a throughput signal, misleading as a unit cost — which is why they are
        reported as two separate numbers rather than one ambiguous one.
        """
        return self.equiv_cost_usd / self.merged if self.merged else None

    @property
    def rework_rate(self) -> float | None:
        """Share of merged work that needed a second pass."""
        return self.shipped_rework / self.merged if self.merged else None

    @property
    def cache_hit_ratio(self) -> float:
        total = self.cache_read_tokens + self.input_tokens + self.cache_write_tokens
        return self.cache_read_tokens / total if total else 0.0


def summarize(audits: list[SessionAudit]) -> Summary:
    """Roll a list of audits up into fleet totals."""
    counts: dict[str, int] = {}
    unpriced: set[str] = set()
    for audit in audits:
        counts[audit.outcome] = counts.get(audit.outcome, 0) + 1
        unpriced.update(audit.unpriced_models)
    clean = counts.get(Outcome.SHIPPED_CLEAN, 0)
    rework = counts.get(Outcome.SHIPPED_REWORK, 0)
    return Summary(
        sessions=len(audits),
        tickets=len({a.ticket for a in audits if a.ticket}),
        equiv_cost_usd=round(sum(a.equiv_cost_usd for a in audits), 4),
        output_tokens=sum(a.output_tokens for a in audits),
        cache_read_tokens=sum(a.cache_read_tokens for a in audits),
        input_tokens=sum(a.input_tokens for a in audits),
        cache_write_tokens=sum(a.cache_write_tokens for a in audits),
        active_sec=round(sum(a.active_sec for a in audits), 1),
        wall_clock_sec=round(sum(a.wall_clock_sec for a in audits), 1),
        merged=clean + rework,
        shipped_clean=clean,
        shipped_rework=rework,
        stalled=counts.get(Outcome.STALLED, 0),
        abandoned=counts.get(Outcome.ABANDONED, 0),
        closed=counts.get(Outcome.CLOSED, 0),
        open_=counts.get(Outcome.OPEN, 0),
        unknown=counts.get(Outcome.UNKNOWN, 0),
        merged_cost_usd=round(sum(a.equiv_cost_usd for a in audits if a.shipped), 4),
        unpriced_models=tuple(sorted(unpriced)),
    )


# ---------------------------------------------------------------------------
# Phase 3 — the subjective read. Opt-in, because it costs real money.
# ---------------------------------------------------------------------------

# A judge run is a real model call against a real diff. Thirty seconds is
# generous for a verdict on a diffstat and keeps a wedged CLI from hanging a
# batch of them.
JUDGE_TIMEOUT_SEC = 60.0

# How much of the diff the judge is shown. A merged PR can be tens of
# thousands of lines; the judge is deciding whether the change *addresses the
# ticket*, which the stat plus the PR's own description answers far more
# cheaply than the full patch — and a truncated patch would be worse than
# none, because the judge could not tell what it was missing.
JUDGE_MAX_BODY_CHARS = 4000

ENV_JUDGE_MODEL = "EPHOR_JUDGE_MODEL"
DEFAULT_JUDGE_MODEL = "claude-sonnet-5"

_JUDGE_SYSTEM = (
    "You grade whether a merged pull request actually accomplished the ticket "
    "it claims to. You are shown the ticket, the PR description and the "
    "diffstat — never the full patch. Reply with one line of JSON and nothing "
    'else: {"met": true|false|null, "note": "<=140 chars"}. Use null for met '
    "when the evidence shown is genuinely insufficient to decide; that is a "
    "valid and useful answer, and guessing is not. The note must cite what in "
    "the evidence drove the verdict."
)


@dataclass(frozen=True)
class Verdict:
    """One judge run's answer. ``met=None`` means it declined to decide."""

    met: bool | None = None
    note: str = ""
    ok: bool = False  # False when the judge could not be run at all


def _run(args: list[str], *, timeout: float, stdin: str = "") -> tuple[int, str]:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            args,
            input=stdin or None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("audit: %s failed: %s", args[0], exc)
        return 1, ""
    return proc.returncode, proc.stdout.strip()


def gather_evidence(audit: SessionAudit) -> str:
    """Assemble what the judge is allowed to see, or "" when there is nothing.

    Deliberately narrow: ticket, PR title and body, and the diffstat. No
    transcript — the agent's own account of what it did is the least
    trustworthy evidence available for the question being asked.
    """
    if audit.pr_number is None:
        return ""
    code, meta = _run(
        [
            "/usr/bin/env",
            "gh",
            "pr",
            "view",
            str(audit.pr_number),
            "--json",
            "title,body",
        ],
        timeout=JUDGE_TIMEOUT_SEC,
    )
    if code != 0:
        return ""
    try:
        parsed = json.loads(meta)
    except (json.JSONDecodeError, ValueError):
        return ""
    title = str(parsed.get("title") or "")
    body = str(parsed.get("body") or "")[:JUDGE_MAX_BODY_CHARS]
    _, stat = _run(
        ["/usr/bin/env", "gh", "pr", "diff", str(audit.pr_number), "--name-only"],
        timeout=JUDGE_TIMEOUT_SEC,
    )
    item = work_items.load(audit.ticket) if audit.ticket else None
    ticket_line = f"{audit.ticket}: {item.jira_title}" if item and item.jira_title else audit.ticket
    return (
        f"TICKET: {ticket_line or 'unknown'}\n"
        f"PR #{audit.pr_number} ({audit.pr_state}): {title}\n\n"
        f"PR DESCRIPTION:\n{body or '(empty)'}\n\n"
        f"FILES CHANGED:\n{stat or '(unavailable)'}\n"
    )


def judge(
    audit: SessionAudit,
    *,
    binary: str = "claude",
    model: str = "",
    timeout: float = JUDGE_TIMEOUT_SEC,
) -> Verdict:
    """Ask a model whether the merged work actually addressed its ticket.

    **This spends money on every call.** Nothing in ephor invokes it on a
    schedule or as part of a default report; it exists for spot checks, and
    the caller is expected to have said so explicitly.

    A refusal to decide (``met=None``) is a real answer and is stored as one.
    The circularity is worth naming: if the agent wrote the ticket *and* the
    PR description, a judge reading both is partly grading its own homework —
    which is exactly why the free proxies in :class:`Outcome` lead the report
    and this does not.
    """
    evidence = gather_evidence(audit)
    if not evidence:
        return Verdict(ok=False, note="no PR evidence available")
    chosen = model or os.environ.get(ENV_JUDGE_MODEL) or DEFAULT_JUDGE_MODEL
    code, out = _run(
        [
            binary,
            "-p",
            "--model",
            chosen,
            "--append-system-prompt",
            _JUDGE_SYSTEM,
        ],
        timeout=timeout,
        stdin=evidence,
    )
    if code != 0 or not out:
        return Verdict(ok=False, note="judge did not answer")
    return parse_verdict(out)


def parse_verdict(raw: str) -> Verdict:
    """Read the judge's reply, tolerating a model that wrapped it in prose."""
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return Verdict(ok=False, note="judge reply was not JSON")
    try:
        parsed = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return Verdict(ok=False, note="judge reply was not JSON")
    if not isinstance(parsed, dict):
        return Verdict(ok=False, note="judge reply was not JSON")
    met = parsed.get("met")
    return Verdict(
        met=met if isinstance(met, bool) else None,
        note=" ".join(str(parsed.get("note") or "").split())[:140],
        ok=True,
    )

"""Local-transcript usage figures via ``ccusage``.

We shell out to `ccusage <https://github.com/ryoppippi/ccusage>`_ — an npm CLI
that reads ``~/.claude/projects/**/*.jsonl`` and computes token/$ totals from
message-level token counts. Two commands answer the dashboard's questions:

* ``ccusage blocks --active --json --offline`` — the active 5-hour billing
  block: tokens used, cost, when the block ends.
* ``ccusage weekly --json --offline`` — per-ISO-week totals; we pick the
  most recent week.

Why not Anthropic's own ``/api/oauth/usage``? It's rate-limited to roughly
once an hour per account; the TUI was burning the budget every refresh and
showing "rate-limited" indefinitely. ccusage is local-only — no auth, no
rate limit, no network — at the cost of being an estimate (the numbers come
from token counts in transcripts rather than the server's accounting).

Caveats:
    * Requires either ``ccusage`` on PATH or ``npx`` for an on-demand run.
      First ``npx`` invocation downloads ccusage (~5s); subsequent are fast.
    * Each invocation rescans all transcripts. Typical cost on a busy
      account is 3-5s, so we keep an hourly cache on disk and a 2-minute
      refresh cadence — the dominant cost is not waiting on data, it's
      avoiding the per-tick scan.
    * 5-hour blocks are ccusage's interpretation of Claude's session
      windowing; "weekly" is the calendar ISO week, not a rolling 7 days.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Subprocess timeout per ccusage invocation. Cold npx adds ~5s; a warm scan
# of ~hundreds of MB of transcripts on a hot SSD lands around 3-5s. 30s is
# generous headroom — past that, something is wrong (missing node, wedged
# install) and the TUI should surface a degraded state rather than hang.
SUBPROCESS_TIMEOUT_SEC = 30.0

# Mirror the original cadence rationale: ccusage is fast enough to poll
# more often, but a 2-minute refresh is plenty for "how much have I spent
# this week" — the underlying transcripts only change on assistant turn
# completion, which is much slower than that anyway.
DEFAULT_REFRESH_INTERVAL_SEC = 120.0
# How long a cached snapshot stays useful for the TUI's bottom strip.
# Generous so a transient ccusage failure (e.g. node restart) doesn't blank
# the strip while the next refresh is queued.
CACHE_MAX_AGE_SEC = 7200.0


@dataclass(frozen=True)
class FiveHourBlock:
    """Active 5-hour billing block as ccusage sees it.

    ``limit_tokens`` and ``percent_used`` are populated when ccusage was
    invoked with ``--token-limit`` (numeric or "max"); they're the
    benchmark the percentage in the TUI is computed against. When both
    are None the strip falls back to a raw token total without %.
    """

    tokens: int
    cost_usd: float
    end_time: datetime  # when the 5h window closes
    is_active: bool
    limit_tokens: int | None = None
    percent_used: float | None = None


@dataclass(frozen=True)
class WeeklyTotal:
    """ISO-week totals (Monday-anchored) for the most recent week."""

    tokens: int
    cost_usd: float
    week_start: str  # "YYYY-MM-DD"


@dataclass(frozen=True)
class UsageSnapshot:
    """Result of one refresh.

    ``error`` is non-None when the snapshot is degraded — windows are None
    and callers should render a hint rather than zero-token bars.
    """

    five_hour: FiveHourBlock | None
    seven_day: WeeklyTotal | None
    fetched_at: datetime
    error: str | None = None


# ---- subprocess plumbing ---------------------------------------------------


def _ccusage_argv() -> list[str] | None:
    """Build the argv prefix for ccusage, or None if neither runner exists.

    Prefers a globally-installed ``ccusage`` (faster, ~3-5s) over ``npx``
    (~5-10s on a cold cache). Returns None when nothing is callable so the
    TUI can render a "ccusage not installed" hint instead of crashing.
    """
    direct = shutil.which("ccusage")
    if direct:
        return [direct]
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", "ccusage@latest"]
    return None


def _run_ccusage(args: list[str], *, timeout: float) -> tuple[str | None, str | None]:
    """Invoke ccusage and return (stdout, error). Stdout is None on failure."""
    argv_prefix = _ccusage_argv()
    if argv_prefix is None:
        return None, "ccusage_missing"
    cmd = [*argv_prefix, *args]
    try:
        proc = subprocess.run(  # noqa: S603 — argv is constructed from PATH lookups
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError as exc:
        return None, f"spawn: {exc}"
    if proc.returncode != 0:
        # ccusage prints diagnostics to stderr; surface a short hint without
        # quoting the whole stream into the UI.
        log.debug("ccusage %s exited %d: %s", args, proc.returncode, proc.stderr[:200])
        return None, f"exit_{proc.returncode}"
    return proc.stdout, None


# ---- parsing ---------------------------------------------------------------


def _parse_iso(s: Any) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_blocks(raw: str) -> FiveHourBlock | None:
    """Pull the active block out of ``ccusage blocks --active --json``.

    Picks up ``tokenLimitStatus`` if ccusage emitted one (i.e. we passed
    ``--token-limit``). Both numeric and "max" limits show up here.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    blocks = data.get("blocks")
    if not isinstance(blocks, list):
        return None
    # ccusage with --active still wraps the result in a list; pick the first
    # entry flagged active. If --active was honored, that's just blocks[0].
    active = next(
        (b for b in blocks if isinstance(b, dict) and b.get("isActive")),
        None,
    )
    if active is None:
        return None
    tokens = active.get("totalTokens")
    cost = active.get("costUSD")
    end_time = _parse_iso(active.get("endTime"))
    if not isinstance(tokens, int) or not isinstance(cost, int | float) or end_time is None:
        return None

    limit_tokens: int | None = None
    percent_used: float | None = None
    status = active.get("tokenLimitStatus")
    if isinstance(status, dict):
        lim = status.get("limit")
        pct = status.get("percentUsed")
        if isinstance(lim, int) and lim > 0:
            limit_tokens = int(lim)
        if isinstance(pct, int | float):
            percent_used = float(pct)

    return FiveHourBlock(
        tokens=int(tokens),
        cost_usd=float(cost),
        end_time=end_time,
        is_active=True,
        limit_tokens=limit_tokens,
        percent_used=percent_used,
    )


def _parse_weekly(raw: str) -> WeeklyTotal | None:
    """Pull the most recent week from ``ccusage weekly --json``."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    weeks = data.get("weekly")
    if not isinstance(weeks, list) or not weeks:
        return None

    # Pick the chronologically-latest week. ccusage default sort is asc; rely
    # on the explicit week string for safety rather than trusting position.
    def _key(w: Any) -> str:
        if isinstance(w, dict):
            ws = w.get("week")
            if isinstance(ws, str):
                return ws
        return ""

    latest = max(weeks, key=_key)
    if not isinstance(latest, dict):
        return None
    tokens = latest.get("totalTokens")
    cost = latest.get("totalCost")
    week_start = latest.get("week")
    if (
        not isinstance(tokens, int)
        or not isinstance(cost, int | float)
        or not isinstance(week_start, str)
    ):
        return None
    return WeeklyTotal(tokens=int(tokens), cost_usd=float(cost), week_start=week_start)


# ---- main entry point ------------------------------------------------------


def fetch_usage(
    *,
    timeout: float = SUBPROCESS_TIMEOUT_SEC,
    runner: Any = None,
    five_hour_cap_tokens: int | None = None,
) -> UsageSnapshot:
    """Refresh token usage by running ccusage twice (blocks + weekly).

    ``five_hour_cap_tokens`` is forwarded as ``--token-limit`` so ccusage
    emits ``tokenLimitStatus`` with a percentage. When None (no user
    config), we use ``--token-limit max`` so ccusage benchmarks against
    the highest historical 5h block — a personal ceiling rather than a
    plan limit, but still useful as a "how heavy is this session"
    signal. Always returns a ``UsageSnapshot`` — failures are reported
    via ``snapshot.error``.

    ``runner`` is injectable for tests; production calls go through the
    real subprocess path.
    """
    now = datetime.now(UTC)
    invoke = runner if runner is not None else _run_ccusage

    token_limit_arg = (
        str(five_hour_cap_tokens) if five_hour_cap_tokens and five_hour_cap_tokens > 0 else "max"
    )
    blocks_out, blocks_err = invoke(
        ["blocks", "--active", "--json", "--offline", "--token-limit", token_limit_arg],
        timeout=timeout,
    )
    weekly_out, weekly_err = invoke(["weekly", "--json", "--offline"], timeout=timeout)

    # Both calls must work to produce a clean snapshot. If one failed but
    # the other succeeded, surface the partial data with the failure hint —
    # the merge step in the TUI will keep the previous good snapshot if we
    # had one.
    five_hour = _parse_blocks(blocks_out) if blocks_out else None
    seven_day = _parse_weekly(weekly_out) if weekly_out else None
    err = blocks_err or weekly_err
    if err is None and five_hour is None and seven_day is None:
        # Both calls "succeeded" but produced nothing parseable — most often
        # means there are no transcripts yet (brand-new install).
        err = "no_data"
    return UsageSnapshot(
        five_hour=five_hour,
        seven_day=seven_day,
        fetched_at=now,
        error=err,
    )


# ---- disk cache ------------------------------------------------------------


def _cache_path() -> Path:
    """Default cache location, with env override for tests."""
    raw = os.environ.get("EPHOR_USAGE_CACHE")
    if raw:
        return Path(raw).expanduser()
    base_raw = os.environ.get("XDG_CACHE_HOME")
    base = Path(base_raw).expanduser() if base_raw else Path.home() / ".cache"
    return base / "ephor" / "usage.json"


def _five_hour_to_dict(b: FiveHourBlock | None) -> dict[str, Any] | None:
    if b is None:
        return None
    return {
        "tokens": b.tokens,
        "cost_usd": b.cost_usd,
        "end_time": b.end_time.isoformat(),
        "is_active": b.is_active,
        "limit_tokens": b.limit_tokens,
        "percent_used": b.percent_used,
    }


def _weekly_to_dict(w: WeeklyTotal | None) -> dict[str, Any] | None:
    if w is None:
        return None
    return {"tokens": w.tokens, "cost_usd": w.cost_usd, "week_start": w.week_start}


def _five_hour_from_dict(d: Any) -> FiveHourBlock | None:
    if not isinstance(d, dict):
        return None
    tokens = d.get("tokens")
    cost = d.get("cost_usd")
    end_time = _parse_iso(d.get("end_time"))
    is_active = d.get("is_active")
    if (
        not isinstance(tokens, int)
        or not isinstance(cost, int | float)
        or end_time is None
        or not isinstance(is_active, bool)
    ):
        return None
    lim_raw = d.get("limit_tokens")
    pct_raw = d.get("percent_used")
    limit_tokens = int(lim_raw) if isinstance(lim_raw, int) and lim_raw > 0 else None
    percent_used = float(pct_raw) if isinstance(pct_raw, int | float) else None
    return FiveHourBlock(
        tokens=int(tokens),
        cost_usd=float(cost),
        end_time=end_time,
        is_active=is_active,
        limit_tokens=limit_tokens,
        percent_used=percent_used,
    )


def _weekly_from_dict(d: Any) -> WeeklyTotal | None:
    if not isinstance(d, dict):
        return None
    tokens = d.get("tokens")
    cost = d.get("cost_usd")
    week_start = d.get("week_start")
    if (
        not isinstance(tokens, int)
        or not isinstance(cost, int | float)
        or not isinstance(week_start, str)
    ):
        return None
    return WeeklyTotal(int(tokens), float(cost), week_start)


def write_cached_snapshot(snap: UsageSnapshot, path: Path | None = None) -> None:
    """Persist a successful snapshot to disk for the next TUI launch.

    No-op when the snapshot is degraded — never overwrite a real reading
    with an error. Best-effort: filesystem failures are swallowed.

    Writes are atomic (tempfile + rename) and 0600 / parent 0700 so
    other local users on a shared host can't read aggregate token
    counts and usage cadence.
    """
    if snap.error is not None:
        return
    target = path or _cache_path()
    payload = {
        "five_hour": _five_hour_to_dict(snap.five_hour),
        "seven_day": _weekly_to_dict(snap.seven_day),
        "fetched_at": snap.fetched_at.isoformat(),
    }
    tmp = target.with_name(f".tmp.{target.name}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            target.parent.chmod(0o700)
        tmp.write_text(json.dumps(payload))
        tmp.chmod(0o600)
        tmp.replace(target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        log.debug("usage cache write failed: %s", exc)


def load_cached_snapshot(
    path: Path | None = None,
    *,
    max_age_sec: float = CACHE_MAX_AGE_SEC,
    now: datetime | None = None,
) -> UsageSnapshot | None:
    """Load the last-good snapshot if recent enough; else None."""
    target = path or _cache_path()
    try:
        raw = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    fetched_raw = raw.get("fetched_at")
    if not isinstance(fetched_raw, str):
        return None
    try:
        fetched_at = datetime.fromisoformat(fetched_raw)
    except ValueError:
        return None
    ref = now or datetime.now(UTC)
    if (ref - fetched_at).total_seconds() > max_age_sec:
        return None
    return UsageSnapshot(
        five_hour=_five_hour_from_dict(raw.get("five_hour")),
        seven_day=_weekly_from_dict(raw.get("seven_day")),
        fetched_at=fetched_at,
        error=None,
    )


def merge_with_previous(fresh: UsageSnapshot, previous: UsageSnapshot | None) -> UsageSnapshot:
    """Pick which snapshot to display going forward.

    A degraded fresh result shouldn't blow away a previously-good reading;
    keep the previous one until a fresh clean reading replaces it. This
    matters because ccusage occasionally returns no_data when transcripts
    are mid-write, or fails outright if node was restarted, etc.
    """
    if fresh.error is None:
        return fresh
    if previous is not None and previous.error is None:
        return previous
    return fresh


# ---- formatting ------------------------------------------------------------


def _pct_color(p: int) -> str:
    """Match the four-bucket palette used elsewhere in the summary line."""
    if p >= 95:
        return "#f85149"
    if p >= 80:
        return "#ff8c00"
    if p >= 60:
        return "#EBCB8B"
    return "#A3BE8C"


def _fmt_tokens(n: int) -> str:
    """Human-friendly token magnitude. Mirrors tui.tokens.format_tokens but
    we don't import it here to keep this module free of TUI dependencies."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}G"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_remaining(end: datetime, now: datetime) -> str:
    secs = int((end - now).total_seconds())
    if secs <= 0:
        return ""
    if secs < 3600:
        return f" · {secs // 60}m left"
    h, m = divmod(secs, 3600)
    m //= 60
    return f" · {h}h{m:02d}m left" if m else f" · {h}h left"


def _fmt_five_hour(b: FiveHourBlock, now: datetime) -> str:
    """Render the 5h segment: ``5h: 285.9M / 400.0M (71%) · 54m left``.

    When neither a configured cap nor ccusage's "max" landed (older
    cached snapshots, parse failures), drop back to the bare token total
    so the strip still says something useful.
    """
    base = f"5h: {_fmt_tokens(b.tokens)}"
    if b.limit_tokens and b.percent_used is not None:
        # Clamp display to 999% so a runaway estimate doesn't overflow the
        # strip's width budget; the color bucket already pegs at red.
        pct = min(999, round(b.percent_used))
        color = _pct_color(pct)
        base = f"{base} / {_fmt_tokens(b.limit_tokens)} ([{color}]{pct}%[/])"
    return f"{base}{_fmt_remaining(b.end_time, now)}"


def _fmt_seven_day(w: WeeklyTotal, weekly_cap: int | None) -> str:
    base = f"7d: {_fmt_tokens(w.tokens)}"
    if weekly_cap and weekly_cap > 0:
        pct = min(999, round(100 * w.tokens / weekly_cap))
        color = _pct_color(pct)
        return f"{base} / {_fmt_tokens(weekly_cap)} ([{color}]{pct}%[/])"
    return base


# ---- per-account anchor fetch ---------------------------------------------

OAUTH_USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


@dataclass(frozen=True)
class OAuthUsageSnapshot:
    """Authoritative server-side usage for the currently-active OAuth account.

    Used as an anchor for per-account extrapolation. Not used for the
    primary TUI strip — ccusage drives that. We hit this endpoint
    sparingly (on detected account switches, then hourly) because it's
    rate-limited to roughly 1 request/hour per account.
    """

    five_hour_pct: float | None
    seven_day_pct: float | None
    five_hour_resets_at: datetime | None
    seven_day_resets_at: datetime | None
    fetched_at: datetime
    error: str | None = None


def _read_oauth_token(path: Path = CREDENTIALS_PATH) -> str | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


def _oauth_window(raw: Any) -> tuple[float | None, datetime | None]:
    if not isinstance(raw, dict):
        return None, None
    util = raw.get("utilization")
    pct = float(util) if isinstance(util, int | float) else None
    rs_raw = raw.get("resets_at")
    rs: datetime | None = None
    if isinstance(rs_raw, str):
        try:
            rs = datetime.fromisoformat(rs_raw.replace("Z", "+00:00"))
        except ValueError:
            rs = None
    return pct, rs


def fetch_oauth_usage(
    *,
    timeout: float = 5.0,
    credentials_path: Path = CREDENTIALS_PATH,
    opener: Any = None,
) -> OAuthUsageSnapshot:
    """Hit ``/api/oauth/usage`` for the active account's authoritative %.

    Always returns a snapshot. Failures surface via ``snapshot.error``;
    callers (the per-account anchor refresh path) keep the previous
    anchor on failure rather than blanking the display.
    """
    now = datetime.now(UTC)
    token = _read_oauth_token(credentials_path)
    if token is None:
        return OAuthUsageSnapshot(None, None, None, None, now, error="no_oauth")
    req = urllib.request.Request(  # endpoint is hardcoded https
        OAUTH_USAGE_ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ephor/ephor",
        },
    )
    fetcher = opener if opener is not None else urllib.request.urlopen
    try:
        with fetcher(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            err = "auth_expired"
        elif exc.code == 429:
            err = "rate_limited"
        else:
            err = f"http_{exc.code}"
        return OAuthUsageSnapshot(None, None, None, None, now, error=err)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return OAuthUsageSnapshot(None, None, None, None, now, error=f"network: {exc}")
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return OAuthUsageSnapshot(None, None, None, None, now, error="bad_json")
    if not isinstance(data, dict):
        return OAuthUsageSnapshot(None, None, None, None, now, error="bad_json")
    p5, r5 = _oauth_window(data.get("five_hour"))
    p7, r7 = _oauth_window(data.get("seven_day"))
    return OAuthUsageSnapshot(
        five_hour_pct=p5,
        seven_day_pct=p7,
        five_hour_resets_at=r5,
        seven_day_resets_at=r7,
        fetched_at=now,
        error=None,
    )


def format_usage_segment(
    snap: UsageSnapshot | None,
    *,
    weekly_cap: int | None = None,
    now: datetime | None = None,
) -> str:
    """Compact rich-markup segment for the TUI summary line.

    Returns "" when there's nothing useful to show. Surfaces a short hint
    on degraded states so the user can tell ccusage-missing from a
    transient parse failure.
    """
    if snap is None:
        return ""
    if snap.error == "ccusage_missing":
        return "[dim]usage: install ccusage (npm i -g ccusage)[/]"
    if snap.error == "timeout":
        return "[dim]usage: ccusage timed out[/]"
    if snap.error == "no_data":
        # Brand-new install with no transcripts yet — silent rather than noisy.
        return ""
    if snap.error is not None and snap.five_hour is None and snap.seven_day is None:
        return "[dim]usage: unavailable[/]"

    ref = now or datetime.now(UTC)
    parts: list[str] = []
    if snap.five_hour is not None:
        parts.append(_fmt_five_hour(snap.five_hour, ref))
    if snap.seven_day is not None:
        parts.append(_fmt_seven_day(snap.seven_day, weekly_cap))
    if not parts:
        return ""
    return "[dim]" + "  ".join(parts) + "[/]"

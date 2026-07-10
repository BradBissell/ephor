"""Per-account usage tracking via anchor + ccusage delta.

Solves the cross-account attribution problem: ccusage scans all transcripts
indiscriminately, with no organization or account identifier in the JSONL,
so its 5h/7d totals are an aggregate across whatever accounts were active.

The fix: when we detect an account switch, hit ``/api/oauth/usage`` once to
get the authoritative server-side percentage *for the now-active account*.
That's our anchor. Between switches, ccusage's running token totals tell
us how much the active account has consumed since the anchor — we
extrapolate the percentage forward by dividing ccusage's delta by a per-
account token limit.

The per-account limit comes from one of three sources, in order:

1. **Calibration** — derived from two server anchors in the same window
   on the same account: ``limit = (token_delta) / (pct_delta) * 100``.
2. **User config** — ``[profiles.<subscription_type>]`` blocks in
   ``account.toml`` (per-tier ceilings the user sets manually).
3. **No limit** — display the anchor pct stale, plus the raw token delta.
   Honest about uncertainty, less noisy than fabricating a number.

Why it works: ccusage's *delta* between two points is correct even though
its absolute total is cross-account, because the user can only consume on
one account at a time. Anchor every switch and the deltas always describe
the active account's consumption since the anchor.

Caveats:
    * ``/api/oauth/usage`` is rate-limited to ~1 request/hr/account; we
      only fire it on detected switches and an hourly refresh per active
      account, well below the limit.
    * Calibration needs two anchors in the same window (same ``resets_at``)
      to work — until that lands, percentages stay anchored to the last
      server snapshot.
    * Window rollovers (5h block expires, 7d week resets) invalidate the
      anchor — we trigger a fresh fetch when ``now > anchor.resets_at``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

# How long an anchor stays valid before we force a fresh /api/oauth/usage
# fetch. Mirrors the official client's 1-hour TTL on its /usage cache.
ANCHOR_MAX_AGE_SEC = 3600.0
# Minimum percentage delta between two anchors before we trust the
# calibration. Below this the noise floor swallows the signal — a small
# pct delta combined with a small token delta gives wildly wrong limits.
CALIBRATION_MIN_PCT_DELTA = 1.0
# Cap how many historical anchors we keep per account. Anchors past this
# count get pruned — calibration only needs the most recent same-window
# pair, and the file shouldn't grow without bound.
ANCHOR_HISTORY_LIMIT = 16


@dataclass(frozen=True)
class AccountFingerprint:
    """Stable identifier for the currently-active account.

    ``fp`` is the opaque hash used as a dict key in the store; ``label``
    is human-friendly (matches profile names in account.toml).
    """

    fp: str
    subscription_type: str
    rate_limit_tier: str

    @property
    def label(self) -> str:
        return self.subscription_type


@dataclass(frozen=True)
class AccountAnchor:
    """One ``/api/oauth/usage`` snapshot, paired with ccusage at the same instant.

    ccusage tokens are *cross-account aggregates* — that's fine because we
    only ever use them as a baseline for deltas going forward; the
    absolute value at anchor time is irrelevant to the per-account math.
    """

    anchored_at: datetime
    server_5h_pct: float
    server_7d_pct: float
    server_5h_resets_at: datetime | None
    server_7d_resets_at: datetime | None
    ccusage_5h_tokens: int  # ccusage active-block tokens at anchor time
    ccusage_7d_tokens: int  # ccusage current-week tokens at anchor time


@dataclass(frozen=True)
class AccountState:
    """Persisted state for one account: anchors + calibrated limits."""

    fingerprint: AccountFingerprint
    anchors: list[AccountAnchor] = field(default_factory=list)
    five_hour_limit_tokens: int | None = None
    seven_day_limit_tokens: int | None = None
    limit_calibrated_at: datetime | None = None


@dataclass(frozen=True)
class AccountUsage:
    """Display-ready per-account usage. ``stale=True`` means the anchor's
    window has rolled over and we need a fresh fetch."""

    label: str  # subscription_type — used for the leading "max | …" tag
    five_hour_pct: float | None
    seven_day_pct: float | None
    five_hour_resets_at: datetime | None
    seven_day_resets_at: datetime | None
    is_extrapolated: bool  # True when ccusage delta moved the pct off the anchor
    stale: bool  # True when the anchor is past its ``resets_at``


# ---- fingerprint detection -------------------------------------------------


def read_active_fingerprint(
    credentials_path: Path = CREDENTIALS_PATH,
) -> AccountFingerprint | None:
    """Read the credentials file and produce a stable per-account fingerprint.

    Returns None for API-key users (no ``claudeAiOauth`` block) or any IO/
    parse failure. The hash inputs are chosen to survive token rotation
    within a single login session: ``refreshToken`` only changes on a fresh
    ``claude /login``, while ``accessToken`` rotates every ~hour.
    """
    try:
        data = json.loads(credentials_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    refresh = oauth.get("refreshToken")
    sub = oauth.get("subscriptionType")
    tier = oauth.get("rateLimitTier")
    if not isinstance(refresh, str) or not isinstance(sub, str) or not isinstance(tier, str):
        return None
    raw = f"{refresh}|{sub}|{tier}".encode()
    fp = hashlib.sha256(raw).hexdigest()[:16]
    return AccountFingerprint(fp=fp, subscription_type=sub, rate_limit_tier=tier)


# ---- store I/O -------------------------------------------------------------


def _store_path() -> Path:
    raw = os.environ.get("EPHOR_USAGE_BY_ACCOUNT_CACHE")
    if raw:
        return Path(raw).expanduser()
    base_raw = os.environ.get("XDG_CACHE_HOME")
    base = Path(base_raw).expanduser() if base_raw else Path.home() / ".cache"
    return base / "ephor" / "usage-by-account.json"


def _parse_iso(s: Any) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _anchor_to_dict(a: AccountAnchor) -> dict[str, Any]:
    return {
        "anchored_at": a.anchored_at.isoformat(),
        "server_5h_pct": a.server_5h_pct,
        "server_7d_pct": a.server_7d_pct,
        "server_5h_resets_at": a.server_5h_resets_at.isoformat() if a.server_5h_resets_at else None,
        "server_7d_resets_at": a.server_7d_resets_at.isoformat() if a.server_7d_resets_at else None,
        "ccusage_5h_tokens": a.ccusage_5h_tokens,
        "ccusage_7d_tokens": a.ccusage_7d_tokens,
    }


def _anchor_from_dict(d: Any) -> AccountAnchor | None:
    if not isinstance(d, dict):
        return None
    anchored_at = _parse_iso(d.get("anchored_at"))
    p5 = d.get("server_5h_pct")
    p7 = d.get("server_7d_pct")
    t5 = d.get("ccusage_5h_tokens")
    t7 = d.get("ccusage_7d_tokens")
    if (
        anchored_at is None
        or not isinstance(p5, int | float)
        or not isinstance(p7, int | float)
        or not isinstance(t5, int)
        or not isinstance(t7, int)
    ):
        return None
    return AccountAnchor(
        anchored_at=anchored_at,
        server_5h_pct=float(p5),
        server_7d_pct=float(p7),
        server_5h_resets_at=_parse_iso(d.get("server_5h_resets_at")),
        server_7d_resets_at=_parse_iso(d.get("server_7d_resets_at")),
        ccusage_5h_tokens=int(t5),
        ccusage_7d_tokens=int(t7),
    )


def _state_to_dict(s: AccountState) -> dict[str, Any]:
    return {
        "fingerprint": {
            "fp": s.fingerprint.fp,
            "subscription_type": s.fingerprint.subscription_type,
            "rate_limit_tier": s.fingerprint.rate_limit_tier,
        },
        "anchors": [_anchor_to_dict(a) for a in s.anchors],
        "five_hour_limit_tokens": s.five_hour_limit_tokens,
        "seven_day_limit_tokens": s.seven_day_limit_tokens,
        "limit_calibrated_at": s.limit_calibrated_at.isoformat() if s.limit_calibrated_at else None,
    }


def _state_from_dict(d: Any) -> AccountState | None:
    if not isinstance(d, dict):
        return None
    fp_raw = d.get("fingerprint")
    if not isinstance(fp_raw, dict):
        return None
    fp = fp_raw.get("fp")
    sub = fp_raw.get("subscription_type")
    tier = fp_raw.get("rate_limit_tier")
    if not isinstance(fp, str) or not isinstance(sub, str) or not isinstance(tier, str):
        return None
    anchors_raw = d.get("anchors") or []
    anchors: list[AccountAnchor] = []
    if isinstance(anchors_raw, list):
        for a in anchors_raw:
            parsed = _anchor_from_dict(a)
            if parsed is not None:
                anchors.append(parsed)
    lim5 = d.get("five_hour_limit_tokens")
    lim7 = d.get("seven_day_limit_tokens")
    return AccountState(
        fingerprint=AccountFingerprint(fp=fp, subscription_type=sub, rate_limit_tier=tier),
        anchors=anchors,
        five_hour_limit_tokens=int(lim5) if isinstance(lim5, int) and lim5 > 0 else None,
        seven_day_limit_tokens=int(lim7) if isinstance(lim7, int) and lim7 > 0 else None,
        limit_calibrated_at=_parse_iso(d.get("limit_calibrated_at")),
    )


def load_store(path: Path | None = None) -> dict[str, AccountState]:
    """Load the per-account state map keyed by fingerprint hash."""
    target = path or _store_path()
    try:
        raw = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    accounts = raw.get("accounts")
    if not isinstance(accounts, dict):
        return {}
    out: dict[str, AccountState] = {}
    for fp_key, state_raw in accounts.items():
        if not isinstance(fp_key, str):
            continue
        state = _state_from_dict(state_raw)
        if state is not None:
            out[fp_key] = state
    return out


def save_store(store: dict[str, AccountState], path: Path | None = None) -> None:
    """Persist the store. Best-effort — IO failures are logged, not raised.

    Writes are atomic (tempfile + rename) and 0600 / parent 0700. The
    contents include account fingerprint hashes, server-side OAuth
    utilization percentages, and ccusage token totals — not secret
    enough to encrypt, but private enough that other local users on
    a shared host shouldn't be able to inventory them.
    """
    target = path or _store_path()
    payload = {"accounts": {fp: _state_to_dict(s) for fp, s in store.items()}}
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
        log.debug("usage-by-account cache write failed: %s", exc)


# ---- mutation --------------------------------------------------------------


def record_anchor(
    store: dict[str, AccountState],
    fingerprint: AccountFingerprint,
    *,
    server_5h_pct: float,
    server_7d_pct: float,
    server_5h_resets_at: datetime | None,
    server_7d_resets_at: datetime | None,
    ccusage_5h_tokens: int,
    ccusage_7d_tokens: int,
    now: datetime | None = None,
) -> AccountState:
    """Append a fresh anchor for the given account, returning the new state.

    Drops anchors past ``ANCHOR_HISTORY_LIMIT`` so the file doesn't bloat
    indefinitely. Triggers calibration after each new anchor — once we
    have two same-window anchors, that's enough to derive limits.
    """
    ts = now or datetime.now(UTC)
    state = store.get(fingerprint.fp) or AccountState(fingerprint=fingerprint)
    new_anchor = AccountAnchor(
        anchored_at=ts,
        server_5h_pct=server_5h_pct,
        server_7d_pct=server_7d_pct,
        server_5h_resets_at=server_5h_resets_at,
        server_7d_resets_at=server_7d_resets_at,
        ccusage_5h_tokens=ccusage_5h_tokens,
        ccusage_7d_tokens=ccusage_7d_tokens,
    )
    anchors = [*state.anchors, new_anchor][-ANCHOR_HISTORY_LIMIT:]
    state = AccountState(
        fingerprint=fingerprint,
        anchors=anchors,
        five_hour_limit_tokens=state.five_hour_limit_tokens,
        seven_day_limit_tokens=state.seven_day_limit_tokens,
        limit_calibrated_at=state.limit_calibrated_at,
    )
    state = _calibrate(state, now=ts)
    store[fingerprint.fp] = state
    return state


def _calibrate(state: AccountState, *, now: datetime) -> AccountState:
    """Derive per-account 5h and 7d token limits from anchor history.

    Two anchors in the same window (same ``resets_at``) give us a
    ``tokens / pct`` rate. Picks the chronologically-furthest pair within
    each window so noise on a single short interval doesn't dominate.
    Returns the state unchanged when calibration isn't possible yet.
    """
    five_hour = _calibrate_window(
        state.anchors,
        token_field="ccusage_5h_tokens",  # noqa: S106 — dataclass field name, not a secret
        pct_field="server_5h_pct",
        resets_field="server_5h_resets_at",
    )
    seven_day = _calibrate_window(
        state.anchors,
        token_field="ccusage_7d_tokens",  # noqa: S106 — dataclass field name, not a secret
        pct_field="server_7d_pct",
        resets_field="server_7d_resets_at",
    )
    if five_hour is None and seven_day is None:
        return state
    return AccountState(
        fingerprint=state.fingerprint,
        anchors=state.anchors,
        five_hour_limit_tokens=five_hour or state.five_hour_limit_tokens,
        seven_day_limit_tokens=seven_day or state.seven_day_limit_tokens,
        limit_calibrated_at=now,
    )


def _calibrate_window(
    anchors: list[AccountAnchor], *, token_field: str, pct_field: str, resets_field: str
) -> int | None:
    """Calibrate one window (5h or 7d) from the most recent same-window pair.

    Picks anchors grouped by ``resets_at`` so a window rollover between
    two anchors doesn't poison the math. Within the most-recent group
    that has ≥2 anchors, takes the chronologically-furthest pair (largest
    pct delta = least noise-sensitive).
    """
    by_window: dict[datetime, list[AccountAnchor]] = {}
    for a in anchors:
        rs = getattr(a, resets_field)
        if rs is None:
            continue
        by_window.setdefault(rs, []).append(a)
    if not by_window:
        return None
    # Most recent window first.
    for window_key in sorted(by_window.keys(), reverse=True):
        group = sorted(by_window[window_key], key=lambda a: a.anchored_at)
        if len(group) < 2:
            continue
        a1, aN = group[0], group[-1]
        pct_delta = getattr(aN, pct_field) - getattr(a1, pct_field)
        if pct_delta < CALIBRATION_MIN_PCT_DELTA:
            continue
        token_delta = getattr(aN, token_field) - getattr(a1, token_field)
        if token_delta <= 0:
            # ccusage rolled over locally between anchors — skip, the
            # next anchor will give us a clean window.
            continue
        # tokens-per-percent → tokens-per-100-percent = limit
        return int(token_delta / pct_delta * 100)
    return None


# ---- display ---------------------------------------------------------------


def latest_anchor(state: AccountState | None) -> AccountAnchor | None:
    if state is None or not state.anchors:
        return None
    return max(state.anchors, key=lambda a: a.anchored_at)


def needs_refresh(
    state: AccountState | None, *, now: datetime, max_age_sec: float = ANCHOR_MAX_AGE_SEC
) -> bool:
    """Should we re-anchor this account? True when there's no anchor, the
    latest is past its window, or it's older than ``max_age_sec``.
    """
    anchor = latest_anchor(state)
    if anchor is None:
        return True
    if (now - anchor.anchored_at).total_seconds() > max_age_sec:
        return True
    if anchor.server_5h_resets_at and now >= anchor.server_5h_resets_at:
        return True
    return bool(anchor.server_7d_resets_at and now >= anchor.server_7d_resets_at)


def compute_usage(
    state: AccountState | None,
    *,
    ccusage_5h_tokens: int | None,
    ccusage_7d_tokens: int | None,
    config_5h_cap: int | None = None,
    config_7d_cap: int | None = None,
    now: datetime | None = None,
) -> AccountUsage | None:
    """Combine anchor + ccusage delta into a display-ready usage struct.

    Returns None when there's no anchor yet (caller renders "fetching…"
    or the existing strip without per-account info). Configured caps in
    ``account.toml`` win over calibration when both are present — user
    intent always trumps inference.
    """
    anchor = latest_anchor(state)
    if anchor is None or state is None:
        return None
    ref = now or datetime.now(UTC)

    five_hour_pct, five_hour_extrapolated, five_hour_stale = _extrapolate(
        anchor_pct=anchor.server_5h_pct,
        anchor_tokens=anchor.ccusage_5h_tokens,
        current_tokens=ccusage_5h_tokens,
        resets_at=anchor.server_5h_resets_at,
        limit=config_5h_cap or state.five_hour_limit_tokens,
        now=ref,
    )
    seven_day_pct, seven_day_extrapolated, seven_day_stale = _extrapolate(
        anchor_pct=anchor.server_7d_pct,
        anchor_tokens=anchor.ccusage_7d_tokens,
        current_tokens=ccusage_7d_tokens,
        resets_at=anchor.server_7d_resets_at,
        limit=config_7d_cap or state.seven_day_limit_tokens,
        now=ref,
    )

    return AccountUsage(
        label=state.fingerprint.subscription_type,
        five_hour_pct=five_hour_pct,
        seven_day_pct=seven_day_pct,
        five_hour_resets_at=anchor.server_5h_resets_at,
        seven_day_resets_at=anchor.server_7d_resets_at,
        is_extrapolated=five_hour_extrapolated or seven_day_extrapolated,
        stale=five_hour_stale or seven_day_stale,
    )


def _extrapolate(
    *,
    anchor_pct: float,
    anchor_tokens: int,
    current_tokens: int | None,
    resets_at: datetime | None,
    limit: int | None,
    now: datetime,
) -> tuple[float | None, bool, bool]:
    """Compute the live percentage from anchor + ccusage delta.

    Returns (pct, was_extrapolated, is_stale). ``stale=True`` signals to
    the caller that this window's anchor is no longer valid (rolled over)
    — UI should hide or dim until the next anchor lands.
    """
    if resets_at is not None and now >= resets_at:
        return None, False, True
    if current_tokens is None:
        return anchor_pct, False, False
    delta = current_tokens - anchor_tokens
    if delta <= 0:
        # ccusage's window rolled over locally between anchor and now,
        # OR the anchor was taken right after a window reset. Either way,
        # treat the anchor pct as authoritative; next anchor will catch up.
        return anchor_pct, False, False
    if not limit or limit <= 0:
        # Uncalibrated and no user config — anchor pct is all we have.
        return anchor_pct, False, False
    pct = anchor_pct + (delta / limit) * 100.0
    return min(999.0, pct), True, False


# ---- profile config integration -------------------------------------------


def caps_for(
    fp: AccountFingerprint, profiles: dict[str, dict[str, int]]
) -> tuple[int | None, int | None]:
    """Resolve per-profile caps from ``account.toml``'s ``[profiles.X]`` blocks.

    Returns (five_hour_cap, seven_day_cap). Profile lookup is by
    ``subscription_type`` (the most natural label for users to write in
    config). Falls back to (None, None) when no profile matches.
    """
    profile = profiles.get(fp.subscription_type)
    if not profile:
        return None, None
    five = profile.get("five_hour_cap_tokens")
    seven = profile.get("weekly_cap_tokens")
    return (
        five if isinstance(five, int) and five > 0 else None,
        seven if isinstance(seven, int) and seven > 0 else None,
    )


# ---- formatting ------------------------------------------------------------


def _pct_color(p: int) -> str:
    """Same palette as the cross-account strip; consistency over the bottom row."""
    if p >= 95:
        return "#f85149"
    if p >= 80:
        return "#ff8c00"
    if p >= 60:
        return "#EBCB8B"
    return "#A3BE8C"


def _fmt_resets(reset: datetime | None, now: datetime) -> str:
    if reset is None:
        return ""
    secs = int((reset - now).total_seconds())
    if secs <= 0:
        return ""
    if secs < 3600:
        return f" · {secs // 60}m"
    if secs < 86400:
        h, m = divmod(secs, 3600)
        m //= 60
        return f" · {h}h{m:02d}m" if m else f" · {h}h"
    return f" · {secs // 86400}d"


def _fmt_pct_with_reset(label: str, pct: float, reset: datetime | None, now: datetime) -> str:
    p = min(999, round(pct))
    color = _pct_color(p)
    return f"{label}: [{color}]{p}%[/]{_fmt_resets(reset, now)}"


def format_account_usage_segment(usage: AccountUsage | None, now: datetime | None = None) -> str:
    """Compact rich-markup rendering for the TUI bottom strip.

    Format: ``[max] 5h: 47% · 47m   7d: 8% · 5d``. When a window is past
    its reset (``stale=True``), shows ``5h: refreshing…`` so the user
    knows we're between anchors rather than that the data is wrong.
    """
    if usage is None:
        return ""
    ref = now or datetime.now(UTC)
    label_seg = f"[bold]{usage.label}[/]"
    parts: list[str] = []
    if usage.five_hour_pct is not None:
        parts.append(_fmt_pct_with_reset("5h", usage.five_hour_pct, usage.five_hour_resets_at, ref))
    elif usage.five_hour_resets_at is not None and ref >= usage.five_hour_resets_at:
        parts.append("5h: refreshing…")
    if usage.seven_day_pct is not None:
        parts.append(_fmt_pct_with_reset("7d", usage.seven_day_pct, usage.seven_day_resets_at, ref))
    elif usage.seven_day_resets_at is not None and ref >= usage.seven_day_resets_at:
        parts.append("7d: refreshing…")
    if not parts:
        return f"[dim]{label_seg}[/]"
    return f"[dim]{label_seg}  " + "  ".join(parts) + "[/]"


# Sentinel for tests that need to bypass the real EPHOR_USAGE_BY_ACCOUNT_CACHE
# resolution without messing with environment globals.
__all__ = [
    "ANCHOR_MAX_AGE_SEC",
    "AccountAnchor",
    "AccountFingerprint",
    "AccountState",
    "AccountUsage",
    "caps_for",
    "compute_usage",
    "format_account_usage_segment",
    "latest_anchor",
    "load_store",
    "needs_refresh",
    "read_active_fingerprint",
    "record_anchor",
    "save_store",
]


# ---- helper for hooking ccusage timestamps ---------------------------------


def stale_window_age(reset: datetime, now: datetime) -> timedelta:
    """How long past expiry an anchor is. Useful for tests + diagnostics."""
    return now - reset

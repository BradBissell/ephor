"""Tests for the ccusage-backed usage fetcher."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ephor.usage import (
    FiveHourBlock,
    UsageSnapshot,
    WeeklyTotal,
    _parse_blocks,
    _parse_weekly,
    fetch_oauth_usage,
    fetch_usage,
    format_usage_segment,
    load_cached_snapshot,
    merge_with_previous,
    write_cached_snapshot,
)

# ---- canned ccusage output -------------------------------------------------

_BLOCKS_OK = json.dumps(
    {
        "blocks": [
            {
                "id": "2026-05-04T12:00:00.000Z",
                "startTime": "2026-05-04T12:00:00.000Z",
                "endTime": "2026-05-04T17:00:00.000Z",
                "isActive": True,
                "isGap": False,
                "totalTokens": 285_853_018,
                "costUSD": 207.74,
            }
        ]
    }
)

_BLOCKS_WITH_LIMIT = json.dumps(
    {
        "blocks": [
            {
                "isActive": True,
                "endTime": "2026-05-04T17:00:00.000Z",
                "totalTokens": 100_000_000,
                "costUSD": 50.0,
                "tokenLimitStatus": {
                    "limit": 200_000_000,
                    "projectedUsage": 150_000_000,
                    "percentUsed": 50.0,
                    "status": "ok",
                },
            }
        ]
    }
)

_WEEKLY_OK = json.dumps(
    {
        "weekly": [
            {
                "week": "2026-04-27",
                "totalTokens": 1_000_000,
                "totalCost": 2.5,
            },
            {
                "week": "2026-05-04",
                "totalTokens": 345_439_865,
                "totalCost": 240.79,
            },
        ]
    }
)


def _runner_returning(blocks: tuple[str | None, str | None], weekly: tuple[str | None, str | None]):
    """Build a fake ccusage runner that returns canned outputs per subcommand."""

    def runner(args: list[str], *, timeout: float) -> tuple[str | None, str | None]:
        if "blocks" in args:
            return blocks
        if "weekly" in args:
            return weekly
        return None, "unexpected_args"

    return runner


# ---- _parse_blocks ---------------------------------------------------------


def test_parse_blocks_extracts_active_block() -> None:
    b = _parse_blocks(_BLOCKS_OK)
    assert b is not None
    assert b.tokens == 285_853_018
    assert b.cost_usd == 207.74
    assert b.end_time == datetime(2026, 5, 4, 17, 0, tzinfo=UTC)
    assert b.is_active is True


def test_parse_blocks_returns_none_when_no_active() -> None:
    """If --active is honored there's always one active block, but if a
    caller passes raw `ccusage blocks` output we shouldn't mis-pick a
    historical block."""
    payload = json.dumps(
        {
            "blocks": [
                {
                    "isActive": False,
                    "endTime": "2026-05-01T17:00:00.000Z",
                    "totalTokens": 1,
                    "costUSD": 0.0,
                }
            ]
        }
    )
    assert _parse_blocks(payload) is None


def test_parse_blocks_handles_malformed_json() -> None:
    assert _parse_blocks("not json") is None


def test_parse_blocks_rejects_missing_required_fields() -> None:
    payload = json.dumps({"blocks": [{"isActive": True}]})  # no tokens/cost/end
    assert _parse_blocks(payload) is None


def test_parse_blocks_captures_token_limit_status() -> None:
    b = _parse_blocks(_BLOCKS_WITH_LIMIT)
    assert b is not None
    assert b.limit_tokens == 200_000_000
    assert b.percent_used == 50.0


def test_parse_blocks_omits_limit_when_ccusage_did_not_emit_one() -> None:
    """Without --token-limit, ccusage skips tokenLimitStatus entirely."""
    b = _parse_blocks(_BLOCKS_OK)
    assert b is not None
    assert b.limit_tokens is None
    assert b.percent_used is None


# ---- _parse_weekly ---------------------------------------------------------


def test_parse_weekly_picks_latest_week() -> None:
    w = _parse_weekly(_WEEKLY_OK)
    assert w is not None
    assert w.week_start == "2026-05-04"
    assert w.tokens == 345_439_865
    assert w.cost_usd == 240.79


def test_parse_weekly_returns_none_when_empty_list() -> None:
    assert _parse_weekly(json.dumps({"weekly": []})) is None


def test_parse_weekly_handles_malformed_json() -> None:
    assert _parse_weekly("garbage") is None


# ---- fetch_usage -----------------------------------------------------------


def test_fetch_usage_success_uses_runner() -> None:
    runner = _runner_returning((_BLOCKS_OK, None), (_WEEKLY_OK, None))
    snap = fetch_usage(runner=runner)
    assert snap.error is None
    assert snap.five_hour is not None and snap.five_hour.tokens == 285_853_018
    assert snap.seven_day is not None and snap.seven_day.tokens == 345_439_865


def test_fetch_usage_propagates_ccusage_missing() -> None:
    runner = _runner_returning((None, "ccusage_missing"), (None, "ccusage_missing"))
    snap = fetch_usage(runner=runner)
    assert snap.error == "ccusage_missing"
    assert snap.five_hour is None
    assert snap.seven_day is None


def test_fetch_usage_propagates_timeout() -> None:
    runner = _runner_returning((None, "timeout"), (None, "timeout"))
    snap = fetch_usage(runner=runner)
    assert snap.error == "timeout"


def test_fetch_usage_partial_failure_returns_partial_data() -> None:
    """Blocks succeeded, weekly failed — surface what we have, plus the err."""
    runner = _runner_returning((_BLOCKS_OK, None), (None, "exit_1"))
    snap = fetch_usage(runner=runner)
    assert snap.five_hour is not None
    assert snap.seven_day is None
    assert snap.error == "exit_1"


def test_fetch_usage_passes_numeric_cap_to_ccusage() -> None:
    """Configured 5h cap is forwarded as `--token-limit <n>`."""
    captured: list[list[str]] = []

    def runner(args: list[str], *, timeout: float) -> tuple[str | None, str | None]:
        captured.append(list(args))
        if "blocks" in args:
            return _BLOCKS_WITH_LIMIT, None
        return _WEEKLY_OK, None

    snap = fetch_usage(runner=runner, five_hour_cap_tokens=200_000_000)
    blocks_call = next(a for a in captured if "blocks" in a)
    assert "--token-limit" in blocks_call
    assert blocks_call[blocks_call.index("--token-limit") + 1] == "200000000"
    assert snap.five_hour is not None
    assert snap.five_hour.percent_used == 50.0


def test_fetch_usage_falls_back_to_max_when_no_cap() -> None:
    """No cap → use ccusage's `max` so we still get a percent against your
    personal historical peak."""
    captured: list[list[str]] = []

    def runner(args: list[str], *, timeout: float) -> tuple[str | None, str | None]:
        captured.append(list(args))
        if "blocks" in args:
            return _BLOCKS_WITH_LIMIT, None
        return _WEEKLY_OK, None

    fetch_usage(runner=runner)
    blocks_call = next(a for a in captured if "blocks" in a)
    assert blocks_call[blocks_call.index("--token-limit") + 1] == "max"


def test_fetch_usage_no_data_when_both_succeed_but_empty() -> None:
    """Brand-new install: ccusage runs cleanly but finds nothing to report."""
    empty_blocks = json.dumps({"blocks": []})
    empty_weekly = json.dumps({"weekly": []})
    runner = _runner_returning((empty_blocks, None), (empty_weekly, None))
    snap = fetch_usage(runner=runner)
    assert snap.error == "no_data"


# ---- disk cache ------------------------------------------------------------


def _good_snapshot(now: datetime) -> UsageSnapshot:
    return UsageSnapshot(
        five_hour=FiveHourBlock(
            tokens=285_853_018,
            cost_usd=207.74,
            end_time=now.replace(microsecond=0) + timedelta(hours=1),
            is_active=True,
            limit_tokens=400_000_000,
            percent_used=71.5,
        ),
        seven_day=WeeklyTotal(tokens=345_439_865, cost_usd=240.79, week_start="2026-05-04"),
        fetched_at=now,
        error=None,
    )


def test_cache_round_trips_5h_limit_fields(tmp_path: Path) -> None:
    """A cached snapshot must remember the cap+percent so reopening the TUI
    doesn't lose the colored % until the next ccusage refresh."""
    cache = tmp_path / "usage.json"
    write_cached_snapshot(_good_snapshot(datetime.now(UTC)), path=cache)
    loaded = load_cached_snapshot(path=cache)
    assert loaded is not None
    assert loaded.five_hour is not None
    assert loaded.five_hour.limit_tokens == 400_000_000
    assert loaded.five_hour.percent_used == 71.5


def test_write_then_load_round_trips(tmp_path: Path) -> None:
    cache = tmp_path / "usage.json"
    now = datetime.now(UTC)
    snap = _good_snapshot(now)
    write_cached_snapshot(snap, path=cache)

    loaded = load_cached_snapshot(path=cache)
    assert loaded is not None
    assert loaded.error is None
    assert loaded.five_hour is not None and loaded.five_hour.tokens == 285_853_018
    assert loaded.seven_day is not None and loaded.seven_day.week_start == "2026-05-04"


def test_write_skipped_for_degraded_snapshot(tmp_path: Path) -> None:
    cache = tmp_path / "usage.json"
    good = _good_snapshot(datetime.now(UTC))
    write_cached_snapshot(good, path=cache)
    bad = UsageSnapshot(None, None, datetime.now(UTC), error="ccusage_missing")
    write_cached_snapshot(bad, path=cache)
    loaded = load_cached_snapshot(path=cache)
    assert loaded is not None
    assert loaded.five_hour is not None and loaded.five_hour.tokens == 285_853_018


def test_load_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_cached_snapshot(path=tmp_path / "nope.json") is None


def test_load_returns_none_when_stale(tmp_path: Path) -> None:
    cache = tmp_path / "usage.json"
    old = datetime(2020, 1, 1, tzinfo=UTC)
    write_cached_snapshot(_good_snapshot(old), path=cache)
    out = load_cached_snapshot(path=cache, max_age_sec=60.0, now=datetime.now(UTC))
    assert out is None


def test_load_handles_corrupt_json(tmp_path: Path) -> None:
    cache = tmp_path / "usage.json"
    cache.write_text("not json")
    assert load_cached_snapshot(path=cache) is None


def test_write_creates_parent_directory(tmp_path: Path) -> None:
    cache = tmp_path / "nested" / "deeper" / "usage.json"
    write_cached_snapshot(_good_snapshot(datetime.now(UTC)), path=cache)
    assert cache.is_file()


def test_write_uses_0600_and_parent_0700(tmp_path: Path) -> None:
    cache = tmp_path / "perm" / "usage.json"
    write_cached_snapshot(_good_snapshot(datetime.now(UTC)), path=cache)
    assert cache.is_file()
    assert (cache.stat().st_mode & 0o777) == 0o600
    assert (cache.parent.stat().st_mode & 0o777) == 0o700


def test_write_leaves_no_tempfile_on_failure(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "usage.json"

    real_replace = Path.replace

    def boom(self: Path, target: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    write_cached_snapshot(_good_snapshot(datetime.now(UTC)), path=cache)
    assert not cache.exists()
    # No leftover .tmp.* files in the parent directory.
    assert not any(p.name.startswith(".tmp.") for p in tmp_path.iterdir())
    monkeypatch.setattr(Path, "replace", real_replace)


# ---- merge_with_previous ---------------------------------------------------


def test_merge_keeps_good_over_error() -> None:
    now = datetime.now(UTC)
    good = _good_snapshot(now)
    bad = UsageSnapshot(None, None, now, error="timeout")
    merged = merge_with_previous(bad, previous=good)
    assert merged is good


def test_merge_uses_fresh_when_clean() -> None:
    now = datetime.now(UTC)
    older = _good_snapshot(now)
    newer = UsageSnapshot(
        five_hour=FiveHourBlock(1, 0.01, now + timedelta(hours=1), True),
        seven_day=None,
        fetched_at=now,
        error=None,
    )
    merged = merge_with_previous(newer, previous=older)
    assert merged is newer


def test_merge_uses_fresh_error_when_no_previous_good() -> None:
    now = datetime.now(UTC)
    bad = UsageSnapshot(None, None, now, error="ccusage_missing")
    assert merge_with_previous(bad, previous=None) is bad
    older_err = UsageSnapshot(None, None, now, error="exit_1")
    assert merge_with_previous(bad, previous=older_err) is bad


# ---- format_usage_segment --------------------------------------------------


def test_format_segment_none_is_empty() -> None:
    assert format_usage_segment(None) == ""


def test_format_segment_no_data_is_empty() -> None:
    """Brand-new install — silent, not noisy."""
    snap = UsageSnapshot(None, None, datetime.now(UTC), error="no_data")
    assert format_usage_segment(snap) == ""


def test_format_segment_missing_ccusage_hints_install() -> None:
    snap = UsageSnapshot(None, None, datetime.now(UTC), error="ccusage_missing")
    out = format_usage_segment(snap)
    assert "install ccusage" in out


def test_format_segment_timeout_distinct() -> None:
    snap = UsageSnapshot(None, None, datetime.now(UTC), error="timeout")
    assert "timed out" in format_usage_segment(snap)


def test_format_segment_renders_5h_and_7d_with_remaining_time() -> None:
    now = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)
    snap = UsageSnapshot(
        five_hour=FiveHourBlock(
            tokens=285_853_018,
            cost_usd=207.74,
            end_time=now + timedelta(hours=2, minutes=30),
            is_active=True,
        ),
        seven_day=WeeklyTotal(345_439_865, 240.79, "2026-05-04"),
        fetched_at=now,
    )
    out = format_usage_segment(snap, now=now)
    assert "5h:" in out
    assert "285.9M" in out
    assert "2h30m left" in out
    assert "7d:" in out
    assert "345.4M" in out


def test_format_segment_5h_renders_pct_and_color_when_limit_set() -> None:
    """Configured cap → ccusage emits percentUsed → strip shows colored %."""
    now = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)
    snap = UsageSnapshot(
        five_hour=FiveHourBlock(
            tokens=170_000_000,
            cost_usd=120.0,
            end_time=now + timedelta(hours=1),
            is_active=True,
            limit_tokens=200_000_000,
            percent_used=85.0,
        ),
        seven_day=None,
        fetched_at=now,
    )
    out = format_usage_segment(snap, now=now)
    assert "5h:" in out
    assert "/ 200.0M" in out
    assert "85%" in out
    # 85% lands in the orange bucket — same palette as the 7d/cap badge.
    assert "#ff8c00" in out


def test_format_segment_5h_color_thresholds_match_7d() -> None:
    """5h colors must match the existing weekly_cap palette (green/yellow/orange/red)."""
    now = datetime.now(UTC)

    def render(pct: float) -> str:
        snap = UsageSnapshot(
            five_hour=FiveHourBlock(
                tokens=1,
                cost_usd=0.0,
                end_time=now,
                is_active=True,
                limit_tokens=100,
                percent_used=pct,
            ),
            seven_day=None,
            fetched_at=now,
        )
        return format_usage_segment(snap, now=now)

    assert "#A3BE8C" in render(20.0)  # green
    assert "#EBCB8B" in render(70.0)  # yellow
    assert "#ff8c00" in render(85.0)  # orange
    assert "#f85149" in render(98.0)  # red


def test_format_segment_5h_clamps_runaway_percent_to_999() -> None:
    """When ccusage's `max` mode estimates >>100% on a heavy day, don't
    blow the strip's width budget."""
    now = datetime.now(UTC)
    snap = UsageSnapshot(
        five_hour=FiveHourBlock(
            tokens=1_000_000_000,
            cost_usd=0.0,
            end_time=now,
            is_active=True,
            limit_tokens=100_000,
            percent_used=99999.0,
        ),
        seven_day=None,
        fetched_at=now,
    )
    out = format_usage_segment(snap, now=now)
    assert "999%" in out
    assert "99999%" not in out


def test_format_segment_seven_day_with_cap_shows_pct() -> None:
    now = datetime.now(UTC)
    snap = UsageSnapshot(
        five_hour=None,
        seven_day=WeeklyTotal(800_000_000, 0.0, "2026-05-04"),
        fetched_at=now,
    )
    out = format_usage_segment(snap, weekly_cap=1_000_000_000, now=now)
    assert "7d:" in out
    assert "/ 1.0G" in out
    assert "80%" in out
    # 80% lands in the orange bucket.
    assert "#ff8c00" in out


def test_format_segment_seven_day_without_cap_omits_pct() -> None:
    now = datetime.now(UTC)
    snap = UsageSnapshot(
        five_hour=None,
        seven_day=WeeklyTotal(345_000_000, 0.0, "2026-05-04"),
        fetched_at=now,
    )
    out = format_usage_segment(snap, weekly_cap=None, now=now)
    assert "7d:" in out
    # The closing markup tag "[/]" contains a slash, so check for the
    # actual delimiter shape used in the with-cap branch instead.
    assert " / " not in out
    assert "%" not in out


def test_format_segment_partial_data_renders_what_we_have() -> None:
    """If only one of (5h, 7d) is populated, render that side and skip the other."""
    now = datetime.now(UTC)
    snap = UsageSnapshot(
        five_hour=FiveHourBlock(1_000_000, 0.5, now + timedelta(hours=1), True),
        seven_day=None,
        fetched_at=now,
        error="exit_1",  # partial failure — but five_hour survived
    )
    out = format_usage_segment(snap, now=now)
    assert "5h:" in out
    assert "7d:" not in out
    # Errors with surviving partial data shouldn't render the "unavailable" hint.
    assert "unavailable" not in out


# ---- OAuth anchor fetcher --------------------------------------------------


class _FakeOAuthResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeOAuthResp:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _write_oauth_creds(tmp_path: Path, token: str = "tok-anchor") -> Path:  # noqa: S107
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))
    return p


def test_fetch_oauth_usage_no_credentials(tmp_path: Path) -> None:
    snap = fetch_oauth_usage(credentials_path=tmp_path / "missing.json")
    assert snap.error == "no_oauth"
    assert snap.five_hour_pct is None


def test_fetch_oauth_usage_success_parses_percentages(tmp_path: Path) -> None:
    creds = _write_oauth_creds(tmp_path)
    payload = json.dumps(
        {
            "five_hour": {"utilization": 50.0, "resets_at": "2026-05-04T17:40:00+00:00"},
            "seven_day": {"utilization": 8.0, "resets_at": "2026-05-10T10:00:00+00:00"},
        }
    ).encode()

    captured: dict[str, object] = {}

    def opener(req: object, timeout: float = 5.0) -> _FakeOAuthResp:
        captured["url"] = getattr(req, "full_url", None)
        return _FakeOAuthResp(payload)

    snap = fetch_oauth_usage(credentials_path=creds, opener=opener)
    assert snap.error is None
    assert snap.five_hour_pct == 50.0
    assert snap.seven_day_pct == 8.0
    assert snap.five_hour_resets_at == datetime(2026, 5, 4, 17, 40, tzinfo=UTC)
    url = captured["url"]
    assert isinstance(url, str) and url.endswith("/api/oauth/usage")


def test_fetch_oauth_usage_429_marks_rate_limited(tmp_path: Path) -> None:
    import io
    import urllib.error

    creds = _write_oauth_creds(tmp_path)

    def opener(req: object, timeout: float = 5.0) -> object:
        raise urllib.error.HTTPError(
            getattr(req, "full_url", ""),
            429,
            "Too Many",
            {},
            io.BytesIO(b""),  # type: ignore[arg-type]
        )

    snap = fetch_oauth_usage(credentials_path=creds, opener=opener)
    assert snap.error == "rate_limited"


def test_fetch_oauth_usage_401_marks_auth_expired(tmp_path: Path) -> None:
    import io
    import urllib.error

    creds = _write_oauth_creds(tmp_path)

    def opener(req: object, timeout: float = 5.0) -> object:
        raise urllib.error.HTTPError(
            getattr(req, "full_url", ""),
            401,
            "Unauthorized",
            {},
            io.BytesIO(b""),  # type: ignore[arg-type]
        )

    snap = fetch_oauth_usage(credentials_path=creds, opener=opener)
    assert snap.error == "auth_expired"


# ---- formatting tail -------------------------------------------------------


def test_format_segment_remaining_in_past_omitted() -> None:
    now = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)
    snap = UsageSnapshot(
        five_hour=FiveHourBlock(1, 0.0, now - timedelta(hours=1), True),
        seven_day=None,
        fetched_at=now,
    )
    out = format_usage_segment(snap, now=now)
    assert "5h:" in out
    assert "left" not in out

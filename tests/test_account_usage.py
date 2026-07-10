"""Tests for per-account usage attribution (anchor + ccusage delta math)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ephor.account_usage import (
    AccountAnchor,
    AccountFingerprint,
    AccountState,
    AccountUsage,
    caps_for,
    compute_usage,
    format_account_usage_segment,
    load_store,
    needs_refresh,
    read_active_fingerprint,
    record_anchor,
    save_store,
)

# ---- fingerprint detection -------------------------------------------------


def _write_creds(
    tmp_path: Path,
    *,
    refresh: str = "rt-original",
    sub: str = "max",
    tier: str = "default_claude_max_20x",
    access: str = "at-1",
) -> Path:
    p = tmp_path / ".credentials.json"
    p.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": access,
                    "refreshToken": refresh,
                    "subscriptionType": sub,
                    "rateLimitTier": tier,
                }
            }
        )
    )
    return p


def test_fingerprint_returns_none_when_no_credentials_file(tmp_path: Path) -> None:
    assert read_active_fingerprint(credentials_path=tmp_path / "missing.json") is None


def test_fingerprint_returns_none_for_api_key_user(tmp_path: Path) -> None:
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"apiKey": "sk-xxx"}))
    assert read_active_fingerprint(credentials_path=p) is None


def test_fingerprint_stable_across_access_token_rotation(tmp_path: Path) -> None:
    """The accessToken refreshes hourly; fingerprint must NOT change for that."""
    p = _write_creds(tmp_path, access="at-1")
    fp1 = read_active_fingerprint(credentials_path=p)
    p.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "at-2-rotated",
                    "refreshToken": "rt-original",
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                }
            }
        )
    )
    fp2 = read_active_fingerprint(credentials_path=p)
    assert fp1 is not None and fp2 is not None
    assert fp1.fp == fp2.fp


def test_fingerprint_changes_on_login_to_different_org(tmp_path: Path) -> None:
    """`/login` to a different org rotates refreshToken + subscriptionType."""
    p = _write_creds(tmp_path, refresh="rt-max", sub="max", tier="default_claude_max_20x")
    fp_max = read_active_fingerprint(credentials_path=p)
    p.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "at-ent",
                    "refreshToken": "rt-enterprise",
                    "subscriptionType": "enterprise",
                    "rateLimitTier": "default_claude_enterprise",
                }
            }
        )
    )
    fp_ent = read_active_fingerprint(credentials_path=p)
    assert fp_max is not None and fp_ent is not None
    assert fp_max.fp != fp_ent.fp
    assert fp_max.subscription_type == "max"
    assert fp_ent.subscription_type == "enterprise"


def test_fingerprint_label_uses_subscription_type(tmp_path: Path) -> None:
    p = _write_creds(tmp_path, sub="enterprise")
    fp = read_active_fingerprint(credentials_path=p)
    assert fp is not None and fp.label == "enterprise"


# ---- store I/O -------------------------------------------------------------


def _fp(sub: str = "max") -> AccountFingerprint:
    return AccountFingerprint(fp=f"fp-{sub}", subscription_type=sub, rate_limit_tier=f"tier-{sub}")


def _anchor(
    *,
    at: datetime,
    p5: float = 50.0,
    p7: float = 8.0,
    t5: int = 100_000_000,
    t7: int = 200_000_000,
    r5: datetime | None = None,
    r7: datetime | None = None,
) -> AccountAnchor:
    return AccountAnchor(
        anchored_at=at,
        server_5h_pct=p5,
        server_7d_pct=p7,
        server_5h_resets_at=r5,
        server_7d_resets_at=r7,
        ccusage_5h_tokens=t5,
        ccusage_7d_tokens=t7,
    )


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    cache = tmp_path / "store.json"
    fp = _fp("max")
    now = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    state = AccountState(
        fingerprint=fp,
        anchors=[_anchor(at=now, r5=now + timedelta(hours=3), r7=now + timedelta(days=6))],
        five_hour_limit_tokens=200_000_000,
        seven_day_limit_tokens=3_000_000_000,
        limit_calibrated_at=now,
    )
    save_store({fp.fp: state}, path=cache)
    loaded = load_store(path=cache)
    assert fp.fp in loaded
    got = loaded[fp.fp]
    assert got.fingerprint.subscription_type == "max"
    assert got.five_hour_limit_tokens == 200_000_000
    assert got.seven_day_limit_tokens == 3_000_000_000
    assert len(got.anchors) == 1
    assert got.anchors[0].anchored_at == now


def test_load_returns_empty_for_missing_file(tmp_path: Path) -> None:
    assert load_store(path=tmp_path / "nope.json") == {}


def test_load_returns_empty_for_corrupt_json(tmp_path: Path) -> None:
    cache = tmp_path / "store.json"
    cache.write_text("garbage")
    assert load_store(path=cache) == {}


def test_save_creates_parent_directory(tmp_path: Path) -> None:
    cache = tmp_path / "deep" / "nested" / "store.json"
    save_store({_fp().fp: AccountState(fingerprint=_fp())}, path=cache)
    assert cache.is_file()


def test_save_uses_0600_and_parent_0700(tmp_path: Path) -> None:
    cache = tmp_path / "perm" / "store.json"
    save_store({_fp().fp: AccountState(fingerprint=_fp())}, path=cache)
    assert cache.is_file()
    assert (cache.stat().st_mode & 0o777) == 0o600
    assert (cache.parent.stat().st_mode & 0o777) == 0o700


def test_save_leaves_no_tempfile_on_failure(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "store.json"

    def boom(self: Path, target: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    save_store({_fp().fp: AccountState(fingerprint=_fp())}, path=cache)
    assert not cache.exists()
    assert not any(p.name.startswith(".tmp.") for p in tmp_path.iterdir())


# ---- record_anchor + history pruning --------------------------------------


def test_record_anchor_creates_state_for_new_account() -> None:
    store: dict[str, AccountState] = {}
    fp = _fp()
    now = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    state = record_anchor(
        store,
        fp,
        server_5h_pct=20.0,
        server_7d_pct=5.0,
        server_5h_resets_at=now + timedelta(hours=3),
        server_7d_resets_at=now + timedelta(days=6),
        ccusage_5h_tokens=10_000_000,
        ccusage_7d_tokens=50_000_000,
        now=now,
    )
    assert state.fingerprint.fp == fp.fp
    assert len(state.anchors) == 1
    assert state.anchors[0].server_5h_pct == 20.0
    assert store[fp.fp] is state


def test_record_anchor_appends_for_existing_account() -> None:
    store: dict[str, AccountState] = {}
    fp = _fp()
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    record_anchor(
        store,
        fp,
        server_5h_pct=10.0,
        server_7d_pct=2.0,
        server_5h_resets_at=base + timedelta(hours=3),
        server_7d_resets_at=base + timedelta(days=6),
        ccusage_5h_tokens=10_000_000,
        ccusage_7d_tokens=20_000_000,
        now=base,
    )
    record_anchor(
        store,
        fp,
        server_5h_pct=15.0,
        server_7d_pct=3.0,
        server_5h_resets_at=base + timedelta(hours=3),
        server_7d_resets_at=base + timedelta(days=6),
        ccusage_5h_tokens=11_000_000,
        ccusage_7d_tokens=21_000_000,
        now=base + timedelta(minutes=10),
    )
    assert len(store[fp.fp].anchors) == 2


def test_record_anchor_prunes_history_at_limit() -> None:
    """Don't grow the file unboundedly across many anchors."""
    from ephor.account_usage import ANCHOR_HISTORY_LIMIT

    store: dict[str, AccountState] = {}
    fp = _fp()
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    for i in range(ANCHOR_HISTORY_LIMIT + 5):
        record_anchor(
            store,
            fp,
            server_5h_pct=float(i),
            server_7d_pct=float(i) / 10,
            server_5h_resets_at=base + timedelta(hours=3),
            server_7d_resets_at=base + timedelta(days=6),
            ccusage_5h_tokens=1_000_000 * (i + 1),
            ccusage_7d_tokens=2_000_000 * (i + 1),
            now=base + timedelta(minutes=i),
        )
    state = store[fp.fp]
    assert len(state.anchors) == ANCHOR_HISTORY_LIMIT
    # The oldest few got dropped; the newest ones remain.
    assert state.anchors[-1].server_5h_pct == float(ANCHOR_HISTORY_LIMIT + 4)


# ---- calibration math ------------------------------------------------------


def test_calibration_derives_limits_from_two_same_window_anchors() -> None:
    store: dict[str, AccountState] = {}
    fp = _fp()
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    five_reset = base + timedelta(hours=3)
    seven_reset = base + timedelta(days=6)

    # Anchor 1: server says 50% / 8%, ccusage says 100M / 200M.
    record_anchor(
        store,
        fp,
        server_5h_pct=50.0,
        server_7d_pct=8.0,
        server_5h_resets_at=five_reset,
        server_7d_resets_at=seven_reset,
        ccusage_5h_tokens=100_000_000,
        ccusage_7d_tokens=200_000_000,
        now=base,
    )
    # Anchor 2 (1h later): server now 60% / 10%, ccusage now 130M / 240M.
    # 5h limit: (130M - 100M) / (60 - 50) * 100 = 300M
    # 7d limit: (240M - 200M) / (10 - 8) * 100 = 2.0G
    record_anchor(
        store,
        fp,
        server_5h_pct=60.0,
        server_7d_pct=10.0,
        server_5h_resets_at=five_reset,
        server_7d_resets_at=seven_reset,
        ccusage_5h_tokens=130_000_000,
        ccusage_7d_tokens=240_000_000,
        now=base + timedelta(hours=1),
    )
    state = store[fp.fp]
    assert state.five_hour_limit_tokens == 300_000_000
    assert state.seven_day_limit_tokens == 2_000_000_000
    assert state.limit_calibrated_at is not None


def test_calibration_skips_when_pct_delta_too_small() -> None:
    """A sub-1% delta is below the noise floor — refuse to calibrate."""
    store: dict[str, AccountState] = {}
    fp = _fp()
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    five_reset = base + timedelta(hours=3)
    seven_reset = base + timedelta(days=6)
    record_anchor(
        store,
        fp,
        server_5h_pct=50.0,
        server_7d_pct=8.0,
        server_5h_resets_at=five_reset,
        server_7d_resets_at=seven_reset,
        ccusage_5h_tokens=100_000_000,
        ccusage_7d_tokens=200_000_000,
        now=base,
    )
    record_anchor(
        store,
        fp,
        server_5h_pct=50.5,
        server_7d_pct=8.1,  # tiny pct deltas
        server_5h_resets_at=five_reset,
        server_7d_resets_at=seven_reset,
        ccusage_5h_tokens=100_010_000,
        ccusage_7d_tokens=200_010_000,
        now=base + timedelta(minutes=5),
    )
    state = store[fp.fp]
    assert state.five_hour_limit_tokens is None
    assert state.seven_day_limit_tokens is None


def test_calibration_skips_anchors_in_different_windows() -> None:
    """If the 5h block rolled over between the two anchors, can't calibrate
    against that pair — the resets_at differs."""
    store: dict[str, AccountState] = {}
    fp = _fp()
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    record_anchor(
        store,
        fp,
        server_5h_pct=50.0,
        server_7d_pct=8.0,
        server_5h_resets_at=base + timedelta(hours=3),
        server_7d_resets_at=base + timedelta(days=6),
        ccusage_5h_tokens=100_000_000,
        ccusage_7d_tokens=200_000_000,
        now=base,
    )
    # Anchor 2 lands AFTER the 5h block ended; new resets_at differs.
    record_anchor(
        store,
        fp,
        server_5h_pct=10.0,
        server_7d_pct=12.0,
        server_5h_resets_at=base + timedelta(hours=8),  # next block
        server_7d_resets_at=base + timedelta(days=6),
        ccusage_5h_tokens=20_000_000,
        ccusage_7d_tokens=260_000_000,
        now=base + timedelta(hours=4),
    )
    state = store[fp.fp]
    # 7d still calibrates (same window), but 5h doesn't.
    assert state.five_hour_limit_tokens is None
    assert state.seven_day_limit_tokens is not None


def test_calibration_uses_furthest_pair_in_window() -> None:
    """With three anchors in one window, span the largest pct delta to
    minimize sensitivity to small-interval noise."""
    store: dict[str, AccountState] = {}
    fp = _fp()
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    five_reset = base + timedelta(hours=3)
    seven_reset = base + timedelta(days=6)
    # Anchors at +0, +30m, +60m. First-to-last delta gives the cleanest signal.
    record_anchor(
        store,
        fp,
        server_5h_pct=10.0,
        server_7d_pct=2.0,
        server_5h_resets_at=five_reset,
        server_7d_resets_at=seven_reset,
        ccusage_5h_tokens=10_000_000,
        ccusage_7d_tokens=20_000_000,
        now=base,
    )
    record_anchor(
        store,
        fp,
        server_5h_pct=15.0,
        server_7d_pct=3.0,
        server_5h_resets_at=five_reset,
        server_7d_resets_at=seven_reset,
        ccusage_5h_tokens=15_000_000,
        ccusage_7d_tokens=25_000_000,
        now=base + timedelta(minutes=30),
    )
    record_anchor(
        store,
        fp,
        server_5h_pct=30.0,
        server_7d_pct=5.0,
        server_5h_resets_at=five_reset,
        server_7d_resets_at=seven_reset,
        ccusage_5h_tokens=30_000_000,
        ccusage_7d_tokens=40_000_000,
        now=base + timedelta(hours=1),
    )
    # Furthest pair: pct 10→30 = +20, tokens 10M→30M = +20M → limit = 100M.
    state = store[fp.fp]
    assert state.five_hour_limit_tokens == 100_000_000


# ---- needs_refresh ---------------------------------------------------------


def test_needs_refresh_when_no_anchor() -> None:
    assert needs_refresh(None, now=datetime.now(UTC)) is True
    assert needs_refresh(AccountState(fingerprint=_fp()), now=datetime.now(UTC)) is True


def test_needs_refresh_after_max_age() -> None:
    fp = _fp()
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    state = AccountState(
        fingerprint=fp,
        anchors=[_anchor(at=base, r5=base + timedelta(hours=3), r7=base + timedelta(days=6))],
    )
    # Just past the 1-hour TTL.
    assert needs_refresh(state, now=base + timedelta(hours=1, seconds=1)) is True
    # Within the TTL.
    assert needs_refresh(state, now=base + timedelta(minutes=30)) is False


def test_needs_refresh_when_5h_window_rolled_over() -> None:
    fp = _fp()
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    state = AccountState(
        fingerprint=fp,
        anchors=[_anchor(at=base, r5=base + timedelta(hours=3), r7=base + timedelta(days=6))],
    )
    # Past the 5h reset.
    assert needs_refresh(state, now=base + timedelta(hours=4)) is True


# ---- compute_usage ---------------------------------------------------------


def test_compute_usage_returns_none_when_no_anchor() -> None:
    out = compute_usage(None, ccusage_5h_tokens=1, ccusage_7d_tokens=1)
    assert out is None


def test_compute_usage_returns_anchor_pct_when_uncalibrated_and_unconfigured() -> None:
    fp = _fp()
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    state = AccountState(
        fingerprint=fp,
        anchors=[
            _anchor(
                at=base,
                p5=50.0,
                p7=8.0,
                t5=100_000_000,
                t7=200_000_000,
                r5=base + timedelta(hours=3),
                r7=base + timedelta(days=6),
            )
        ],
    )
    out = compute_usage(
        state,
        ccusage_5h_tokens=110_000_000,
        ccusage_7d_tokens=210_000_000,
        now=base + timedelta(minutes=15),
    )
    assert out is not None
    # No limit available → return anchor pct verbatim, flag NOT extrapolated.
    assert out.five_hour_pct == 50.0
    assert out.seven_day_pct == 8.0
    assert out.is_extrapolated is False


def test_compute_usage_extrapolates_with_calibrated_limits() -> None:
    fp = _fp()
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    state = AccountState(
        fingerprint=fp,
        anchors=[
            _anchor(
                at=base,
                p5=50.0,
                p7=8.0,
                t5=100_000_000,
                t7=200_000_000,
                r5=base + timedelta(hours=3),
                r7=base + timedelta(days=6),
            )
        ],
        five_hour_limit_tokens=200_000_000,  # 1% = 2M tokens
        seven_day_limit_tokens=2_000_000_000,  # 1% = 20M tokens
    )
    # ccusage shows +20M on 5h since anchor → +10% → display 60%
    # ccusage shows +40M on 7d since anchor → +2% → display 10%
    out = compute_usage(
        state,
        ccusage_5h_tokens=120_000_000,
        ccusage_7d_tokens=240_000_000,
        now=base + timedelta(minutes=30),
    )
    assert out is not None
    assert out.five_hour_pct == 60.0
    assert out.seven_day_pct == 10.0
    assert out.is_extrapolated is True


def test_compute_usage_config_caps_override_calibration() -> None:
    """User's `[profiles.X]` ceiling wins over derived calibration — intent
    beats inference."""
    fp = _fp()
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    state = AccountState(
        fingerprint=fp,
        anchors=[
            _anchor(
                at=base,
                p5=50.0,
                p7=8.0,
                t5=100_000_000,
                t7=200_000_000,
                r5=base + timedelta(hours=3),
                r7=base + timedelta(days=6),
            )
        ],
        # Calibration says 200M (each 1% = 2M); user config says 400M (1% = 4M).
        # +20M ccusage delta with config_5h_cap=400M → +5% → display 55%.
        five_hour_limit_tokens=200_000_000,
    )
    out = compute_usage(
        state,
        ccusage_5h_tokens=120_000_000,
        ccusage_7d_tokens=200_000_000,
        config_5h_cap=400_000_000,
        now=base + timedelta(minutes=30),
    )
    assert out is not None
    assert out.five_hour_pct == 55.0


def test_compute_usage_marks_stale_after_window_rollover() -> None:
    fp = _fp()
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    state = AccountState(
        fingerprint=fp,
        anchors=[
            _anchor(
                at=base,
                p5=50.0,
                p7=8.0,
                t5=100_000_000,
                t7=200_000_000,
                r5=base + timedelta(hours=3),
                r7=base + timedelta(days=6),
            )
        ],
        five_hour_limit_tokens=200_000_000,
    )
    # Past 5h reset — that side goes stale; 7d still valid.
    out = compute_usage(
        state,
        ccusage_5h_tokens=120_000_000,
        ccusage_7d_tokens=210_000_000,
        now=base + timedelta(hours=4),
    )
    assert out is not None
    assert out.five_hour_pct is None
    assert out.stale is True
    assert out.seven_day_pct == 8.0  # 7d uncalibrated → anchor pct


def test_compute_usage_negative_ccusage_delta_falls_back_to_anchor() -> None:
    """If ccusage rolled over locally between anchor and now (e.g., its
    block boundary differs from server's), don't display a nonsense
    negative pct — show the anchor value."""
    fp = _fp()
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    state = AccountState(
        fingerprint=fp,
        anchors=[
            _anchor(
                at=base,
                p5=50.0,
                p7=8.0,
                t5=100_000_000,
                t7=200_000_000,
                r5=base + timedelta(hours=3),
                r7=base + timedelta(days=6),
            )
        ],
        five_hour_limit_tokens=200_000_000,
    )
    out = compute_usage(
        state,
        ccusage_5h_tokens=10_000_000,  # smaller than anchor!
        ccusage_7d_tokens=200_000_000,
        now=base + timedelta(minutes=15),
    )
    assert out is not None
    assert out.five_hour_pct == 50.0  # anchor verbatim
    assert out.is_extrapolated is False


def test_compute_usage_clamps_runaway_extrapolation() -> None:
    fp = _fp()
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    state = AccountState(
        fingerprint=fp,
        anchors=[
            _anchor(
                at=base,
                p5=90.0,
                p7=80.0,
                t5=100_000_000,
                t7=200_000_000,
                r5=base + timedelta(hours=3),
                r7=base + timedelta(days=6),
            )
        ],
        five_hour_limit_tokens=10_000_000,  # tiny limit → big pct delta
    )
    out = compute_usage(
        state,
        ccusage_5h_tokens=1_100_000_000,
        ccusage_7d_tokens=200_000_000,
        now=base + timedelta(minutes=10),
    )
    assert out is not None
    assert out.five_hour_pct == 999.0  # clamped


def test_compute_usage_label_matches_subscription_type() -> None:
    fp = AccountFingerprint(fp="x", subscription_type="enterprise", rate_limit_tier="t")
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    state = AccountState(
        fingerprint=fp,
        anchors=[_anchor(at=base, r5=base + timedelta(hours=3), r7=base + timedelta(days=6))],
    )
    out = compute_usage(state, ccusage_5h_tokens=None, ccusage_7d_tokens=None, now=base)
    assert out is not None and out.label == "enterprise"


def test_compute_usage_handles_missing_ccusage_data() -> None:
    """ccusage failures shouldn't crash per-account compute — show anchor pct."""
    fp = _fp()
    base = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    state = AccountState(
        fingerprint=fp,
        anchors=[
            _anchor(at=base, p5=50.0, r5=base + timedelta(hours=3), r7=base + timedelta(days=6))
        ],
        five_hour_limit_tokens=200_000_000,
    )
    out = compute_usage(state, ccusage_5h_tokens=None, ccusage_7d_tokens=None, now=base)
    assert out is not None
    assert out.five_hour_pct == 50.0
    assert out.is_extrapolated is False


# ---- caps_for --------------------------------------------------------------


def test_caps_for_pulls_profile_caps() -> None:
    fp = _fp("max")
    profiles = {
        "max": {"five_hour_cap_tokens": 200_000_000, "weekly_cap_tokens": 3_000_000_000},
        "enterprise": {"five_hour_cap_tokens": 500_000_000, "weekly_cap_tokens": 10_000_000_000},
    }
    five, seven = caps_for(fp, profiles)
    assert five == 200_000_000
    assert seven == 3_000_000_000


def test_caps_for_returns_none_when_profile_absent() -> None:
    fp = _fp("max")
    five, seven = caps_for(fp, {"enterprise": {"five_hour_cap_tokens": 100}})
    assert five is None
    assert seven is None


# ---- formatting ------------------------------------------------------------


def test_format_segment_renders_label_and_both_windows() -> None:
    now = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    usage = AccountUsage(
        label="max",
        five_hour_pct=47.0,
        seven_day_pct=8.0,
        five_hour_resets_at=now + timedelta(minutes=47),
        seven_day_resets_at=now + timedelta(days=5),
        is_extrapolated=True,
        stale=False,
    )
    out = format_account_usage_segment(usage, now=now)
    assert "max" in out
    assert "5h:" in out
    assert "47%" in out
    assert "47m" in out
    assert "7d:" in out
    assert "8%" in out
    assert "5d" in out


def test_format_segment_color_thresholds() -> None:
    now = datetime.now(UTC)

    def render(p5h: float) -> str:
        u = AccountUsage(
            label="max",
            five_hour_pct=p5h,
            seven_day_pct=None,
            five_hour_resets_at=None,
            seven_day_resets_at=None,
            is_extrapolated=False,
            stale=False,
        )
        return format_account_usage_segment(u, now=now)

    assert "#A3BE8C" in render(20.0)  # green
    assert "#EBCB8B" in render(70.0)  # yellow
    assert "#ff8c00" in render(85.0)  # orange
    assert "#f85149" in render(98.0)  # red


def test_format_segment_returns_empty_for_none() -> None:
    assert format_account_usage_segment(None) == ""


def test_format_segment_shows_refreshing_after_window_rollover() -> None:
    now = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
    usage = AccountUsage(
        label="enterprise",
        five_hour_pct=None,  # signaled stale by compute_usage
        seven_day_pct=8.0,
        five_hour_resets_at=now - timedelta(minutes=10),  # in the past
        seven_day_resets_at=now + timedelta(days=5),
        is_extrapolated=False,
        stale=True,
    )
    out = format_account_usage_segment(usage, now=now)
    assert "5h: refreshing" in out
    assert "7d:" in out and "8%" in out


def test_format_segment_clamps_runaway_pct() -> None:
    now = datetime.now(UTC)
    usage = AccountUsage(
        label="max",
        five_hour_pct=999.0,
        seven_day_pct=None,
        five_hour_resets_at=None,
        seven_day_resets_at=None,
        is_extrapolated=True,
        stale=False,
    )
    out = format_account_usage_segment(usage, now=now)
    assert "999%" in out

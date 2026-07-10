"""Tests for account.toml loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from ephor import account


def test_returns_defaults_when_file_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPHOR_ACCOUNT_CONFIG", str(tmp_path / "missing.toml"))
    cfg = account.load_account_config()
    assert cfg.weekly_cap_tokens is None


def test_loads_weekly_cap_from_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "account.toml"
    p.write_text("weekly_cap_tokens = 3_000_000\n")
    monkeypatch.setenv("EPHOR_ACCOUNT_CONFIG", str(p))
    cfg = account.load_account_config()
    assert cfg.weekly_cap_tokens == 3_000_000


def test_rejects_non_positive_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "account.toml"
    p.write_text("weekly_cap_tokens = 0\n")
    monkeypatch.setenv("EPHOR_ACCOUNT_CONFIG", str(p))
    cfg = account.load_account_config()
    assert cfg.weekly_cap_tokens is None


def test_garbled_toml_logs_and_returns_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "account.toml"
    p.write_text("not valid = = toml\n")
    monkeypatch.setenv("EPHOR_ACCOUNT_CONFIG", str(p))
    cfg = account.load_account_config()
    assert cfg.weekly_cap_tokens is None


def test_explicit_path_argument_overrides_env(tmp_path: Path) -> None:
    p = tmp_path / "specific.toml"
    p.write_text("weekly_cap_tokens = 1234567\n")
    cfg = account.load_account_config(p)
    assert cfg.weekly_cap_tokens == 1234567


def test_loads_five_hour_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "account.toml"
    p.write_text("weekly_cap_tokens = 3_000_000_000\nfive_hour_cap_tokens = 200_000_000\n")
    monkeypatch.setenv("EPHOR_ACCOUNT_CONFIG", str(p))
    cfg = account.load_account_config()
    assert cfg.weekly_cap_tokens == 3_000_000_000
    assert cfg.five_hour_cap_tokens == 200_000_000


def test_five_hour_cap_defaults_to_none_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "account.toml"
    p.write_text("weekly_cap_tokens = 1\n")
    monkeypatch.setenv("EPHOR_ACCOUNT_CONFIG", str(p))
    cfg = account.load_account_config()
    assert cfg.five_hour_cap_tokens is None


def test_loads_per_account_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "account.toml"
    p.write_text(
        "[profiles.max]\n"
        "weekly_cap_tokens = 3_000_000_000\n"
        "five_hour_cap_tokens = 200_000_000\n"
        "\n"
        "[profiles.enterprise]\n"
        "weekly_cap_tokens = 10_000_000_000\n"
        "five_hour_cap_tokens = 500_000_000\n"
    )
    monkeypatch.setenv("EPHOR_ACCOUNT_CONFIG", str(p))
    cfg = account.load_account_config()
    assert cfg.profiles["max"]["weekly_cap_tokens"] == 3_000_000_000
    assert cfg.profiles["max"]["five_hour_cap_tokens"] == 200_000_000
    assert cfg.profiles["enterprise"]["weekly_cap_tokens"] == 10_000_000_000
    assert cfg.profiles["enterprise"]["five_hour_cap_tokens"] == 500_000_000


def test_profiles_drop_non_positive_or_unknown_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in account.toml shouldn't propagate junk into AccountConfig."""
    p = tmp_path / "account.toml"
    p.write_text(
        "[profiles.max]\n"
        "weekly_cap_tokens = 0\n"  # rejected: not > 0
        "five_hour_cap_tokens = 200\n"  # kept
        "weeky_cap_tokens = 999\n"  # rejected: typo, unknown key
    )
    monkeypatch.setenv("EPHOR_ACCOUNT_CONFIG", str(p))
    cfg = account.load_account_config()
    assert cfg.profiles["max"] == {"five_hour_cap_tokens": 200}


def test_profiles_default_to_empty_dict_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "account.toml"
    p.write_text("weekly_cap_tokens = 1\n")
    monkeypatch.setenv("EPHOR_ACCOUNT_CONFIG", str(p))
    cfg = account.load_account_config()
    assert cfg.profiles == {}

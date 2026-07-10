"""Unit tests for speech_settings: env > file > default resolution.

The persistence layer is small but the *resolution order* is exactly the
kind of thing that breaks silently in production — if the env var stops
winning, users press the mute hotkey and nothing happens at next launch.
Lock the precedence in tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ephor import speech_settings
from ephor.speech_settings import SettingsSource


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox EPHOR_CONFIG_DIR + clear the env override."""
    monkeypatch.setenv("EPHOR_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv(speech_settings.ENV_VAR, raising=False)
    return tmp_path / "speech.json"


# ---- precedence ----------------------------------------------------------


def test_env_var_overrides_persisted_file(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env wins over file. If we lose this, `EPHOR_TTS_ENABLED=0 ephor tui`
    silently plays audio and that's a meeting-disrupting bug."""
    speech_settings.save(enabled=True)
    monkeypatch.setenv(speech_settings.ENV_VAR, "0")
    s = speech_settings.load()
    assert s.enabled is False
    assert s.source == SettingsSource.ENV


def test_file_overrides_default(isolated_config: Path) -> None:
    speech_settings.save(enabled=False)
    s = speech_settings.load()
    assert s.enabled is False
    assert s.source == SettingsSource.FILE


def test_default_used_when_neither_env_nor_file_set(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force kokoro_available to a known value so the test isn't dependent
    # on the host machine.
    import ephor.speech_player as sp

    monkeypatch.setattr(sp, "kokoro_available", lambda: True)
    s = speech_settings.load()
    assert s.enabled is True
    assert s.source == SettingsSource.DEFAULT


def test_default_off_when_kokoro_missing(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user without TTS installed should never get audio attempts."""
    import ephor.speech_player as sp

    monkeypatch.setattr(sp, "kokoro_available", lambda: False)
    s = speech_settings.load()
    assert s.enabled is False
    assert s.source == SettingsSource.DEFAULT


# ---- env parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("0", False),
        ("true", True),
        ("FALSE", False),
        ("on", True),
        ("OFF", False),
        ("yes", True),
        ("no", False),
        ("enabled", True),
        ("disabled", False),
        (" 1 ", True),
    ],
)
def test_env_var_accepts_common_truthy_falsy_strings(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: bool,
) -> None:
    monkeypatch.setenv(speech_settings.ENV_VAR, raw)
    assert speech_settings.load().enabled is expected


def test_unparseable_env_var_falls_through_to_file(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    speech_settings.save(enabled=False)
    monkeypatch.setenv(speech_settings.ENV_VAR, "maybe")
    s = speech_settings.load()
    # Unparseable env → next layer wins (file says false).
    assert s.enabled is False
    assert s.source == SettingsSource.FILE


# ---- file persistence ----------------------------------------------------


def test_save_writes_atomically_and_chmods_0600(isolated_config: Path) -> None:
    speech_settings.save(enabled=True)
    assert isolated_config.is_file()
    data = json.loads(isolated_config.read_text())
    assert data["enabled"] is True
    # 0o600 — never world-readable.
    mode = isolated_config.stat().st_mode & 0o777
    assert mode == 0o600
    # No tempfile leftover.
    leftovers = list(isolated_config.parent.glob("*.tmp"))
    assert leftovers == []


def test_save_overwrites_existing_value(isolated_config: Path) -> None:
    speech_settings.save(enabled=True)
    speech_settings.save(enabled=False)
    assert speech_settings.load().enabled is False


def test_corrupt_file_falls_through_to_default(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A garbage settings file shouldn't crash the dashboard. We treat it
    as 'no setting' and rely on the default — next save fixes the file."""
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text("{ this is not valid json")
    import ephor.speech_player as sp

    monkeypatch.setattr(sp, "kokoro_available", lambda: True)
    s = speech_settings.load()
    assert s.source == SettingsSource.DEFAULT
    assert s.enabled is True


def test_settings_path_uses_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EPHOR_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    p = speech_settings.settings_path()
    assert p == tmp_path / "ephor" / "speech.json"


def test_settings_path_cco_config_dir_overrides_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPHOR_CONFIG_DIR", str(tmp_path / "custom"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "should-not-be-used"))
    p = speech_settings.settings_path()
    assert p == tmp_path / "custom" / "speech.json"


# ---- calibration persistence ---------------------------------------------


def test_save_calibration_then_load_roundtrips(isolated_config: Path) -> None:
    speech_settings.save(enabled=True, calibrated_chars_per_sec=17.5)
    s = speech_settings.load()
    assert s.calibrated_chars_per_sec == 17.5
    # enabled is still True from the same save.
    assert s.enabled is True


def test_save_calibration_preserves_existing_enabled(isolated_config: Path) -> None:
    """Calibration updates should never accidentally flip the mute state."""
    speech_settings.save(enabled=False)
    speech_settings.save(calibrated_chars_per_sec=20.0)
    s = speech_settings.load()
    assert s.enabled is False
    assert s.calibrated_chars_per_sec == 20.0


def test_save_enabled_preserves_existing_calibration(isolated_config: Path) -> None:
    """Toggling mute should never wipe a learned rate."""
    speech_settings.save(calibrated_chars_per_sec=18.5)
    speech_settings.save(enabled=False)
    s = speech_settings.load()
    assert s.calibrated_chars_per_sec == 18.5


def test_clear_calibration_removes_only_calibration(isolated_config: Path) -> None:
    speech_settings.save(enabled=True, calibrated_chars_per_sec=22.0)
    speech_settings.save(clear_calibration=True)
    s = speech_settings.load()
    assert s.enabled is True
    assert s.calibrated_chars_per_sec is None


@pytest.mark.parametrize("bad_value", [0, -1, 1000, "fast", None])
def test_load_rejects_implausible_calibration_values(
    isolated_config: Path, bad_value: object
) -> None:
    """An out-of-range value (e.g. corrupt config or dev typo) shouldn't
    silently make the bar advance at warp speed or stand still — fall
    back to None so the default rate kicks in."""
    import json

    payload = {"enabled": True, "calibrated_chars_per_sec": bad_value}
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text(json.dumps(payload))
    s = speech_settings.load()
    assert s.calibrated_chars_per_sec is None


def test_calibration_loads_even_when_env_overrides_enabled(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env var only governs SPEAKING. The calibrated rate should still
    load so the bar's progress estimate stays accurate even when audio
    is silenced via env."""
    speech_settings.save(enabled=True, calibrated_chars_per_sec=16.0)
    monkeypatch.setenv(speech_settings.ENV_VAR, "0")
    s = speech_settings.load()
    assert s.source == speech_settings.SettingsSource.ENV
    assert s.enabled is False
    assert s.calibrated_chars_per_sec == 16.0


# ---- speak_mode ----------------------------------------------------------


def test_default_speak_mode_is_full(isolated_config: Path) -> None:
    s = speech_settings.load()
    assert s.speak_mode == speech_settings.SPEAK_MODE_FULL
    assert s.mode_source == SettingsSource.DEFAULT


def test_save_speak_mode_persists_and_loads(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(speech_settings.MODE_ENV_VAR, raising=False)
    speech_settings.save(speak_mode=speech_settings.SPEAK_MODE_SUMMARY)
    s = speech_settings.load()
    assert s.speak_mode == speech_settings.SPEAK_MODE_SUMMARY
    assert s.mode_source == SettingsSource.FILE


def test_speak_mode_env_overrides_file(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    speech_settings.save(speak_mode=speech_settings.SPEAK_MODE_FULL)
    monkeypatch.setenv(speech_settings.MODE_ENV_VAR, "summary")
    s = speech_settings.load()
    assert s.speak_mode == speech_settings.SPEAK_MODE_SUMMARY
    assert s.mode_source == SettingsSource.ENV


def test_save_rejects_unknown_speak_mode(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(speech_settings.MODE_ENV_VAR, raising=False)
    with pytest.raises(ValueError):
        speech_settings.save(speak_mode="loud-please")


def test_unknown_env_value_falls_through_to_file_or_default(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in EPHOR_TTS_MODE must not lock the user into a broken mode."""
    speech_settings.save(speak_mode=speech_settings.SPEAK_MODE_SUMMARY)
    monkeypatch.setenv(speech_settings.MODE_ENV_VAR, "garbage")
    s = speech_settings.load()
    assert s.speak_mode == speech_settings.SPEAK_MODE_SUMMARY
    assert s.mode_source == SettingsSource.FILE


def test_save_speak_mode_preserves_enabled(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(speech_settings.MODE_ENV_VAR, raising=False)
    speech_settings.save(enabled=False)
    speech_settings.save(speak_mode=speech_settings.SPEAK_MODE_SUMMARY)
    s = speech_settings.load()
    # The earlier `enabled=False` write must survive the mode update.
    assert s.enabled is False
    assert s.speak_mode == speech_settings.SPEAK_MODE_SUMMARY

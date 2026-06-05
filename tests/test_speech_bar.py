"""Tests for the SpeechBar widget rendering + jump-to-speaking action."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from claude_orchestrator import speech
from claude_orchestrator.speech import SpeechState
from claude_orchestrator.state.manager import StateManager
from claude_orchestrator.state.models import AgentState
from claude_orchestrator.tui import app as tui_app
from claude_orchestrator.tui.app import CcoApp
from claude_orchestrator.tui.widgets.speech_bar import render_bar


def _write_state(directory: Path, sid: str, **overrides: Any) -> None:
    base = {
        "session_id": sid,
        "cwd": "/tmp/x",
        "started_at": "2026-04-29T10:00:00Z",
        "last_event_time": "2026-04-29T10:00:00Z",
    }
    base.update(overrides)
    (directory / f"{sid}.json").write_text(AgentState(**base).to_json())


@pytest.fixture
def speech_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "speech.jsonl"
    monkeypatch.setenv("CCO_SPEECH_LOG", str(log))
    return log


@pytest.fixture(autouse=True)
def _no_real_tts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure tests never try to invoke kokoro. Forcing default_tts_command
    to return None routes the player through its null-spawner code path,
    which preserves queue semantics without ever fork()-ing."""
    import claude_orchestrator.speech_player as sp

    monkeypatch.setattr(sp, "default_tts_command", lambda: None)


@pytest.fixture
def populated_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("CCO_STATE_DIR", str(sd))
    _write_state(sd, "alpha-id", project_name="alpha")
    _write_state(sd, "beta-id", project_name="beta")
    return sd


# ---- pure render_bar -----------------------------------------------------


def test_render_bar_when_idle_shows_no_speech_hint() -> None:
    out = render_bar(None)
    assert "no speech" in out


def test_render_bar_when_stopped_shows_no_speech_hint() -> None:
    state = SpeechState(
        session_id="x", text="Hi.", sentences=["Hi."], started_ms=0, speed=1.3, stopped=True
    )
    out = render_bar(state)
    assert "no speech" in out


def test_render_bar_includes_label_and_active_sentence() -> None:
    state = SpeechState(
        session_id="abc12345",
        text="Hello world.",
        sentences=["Hello world."],
        started_ms=int(__import__("time").time() * 1000),
        speed=1.3,
        stopped=False,
    )
    out = render_bar(state, label="myproject")
    assert "myproject" in out
    assert "Hello world." in out


def test_render_bar_escapes_brackets_in_assistant_text() -> None:
    """A stray [foo] in the response must not be parsed as a rich tag and
    eat the rest of the bar."""
    state = SpeechState(
        session_id="x",
        text="Use [Read] tool.",
        sentences=["Use [Read] tool."],
        started_ms=int(__import__("time").time() * 1000),
        speed=1.3,
        stopped=False,
    )
    out = render_bar(state, label="x")
    assert r"\[Read\]" in out


# ---- widget tick behaviour ----------------------------------------------


@pytest.mark.asyncio
async def test_speech_bar_picks_up_new_start_record(populated_dir: Path, speech_log: Path) -> None:
    """When cco owns playback, a new Stop event lands in the log → the
    player picks it up on tick → the bar reflects it on the next refresh."""
    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        assert app._speech_bar is not None
        app._speech_player.tick()
        app._speech_bar.refresh_now()
        assert app._speech_bar.speaking_session_id is None
        speech.append_start("alpha-id", "Streaming response.")
        app._speech_player.tick()
        app._speech_bar.refresh_now()
        assert app._speech_bar.speaking_session_id == "alpha-id"


# ---- action_jump_speaking -----------------------------------------------


@pytest.mark.asyncio
async def test_jump_speaking_routes_to_active_session(
    populated_dir: Path,
    speech_log: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_orchestrator.tmux import navigator

    captured: list[AgentState] = []

    def fake_jump(agent: AgentState) -> Any:
        captured.append(agent)

        class _Outcome:
            ok = True
            result = navigator.JumpResult.OK
            detail = ""

        return _Outcome()

    monkeypatch.setattr(tui_app, "jump_to", fake_jump)

    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        # Append AFTER app creation so the watcher (which starts at EOF
        # on init to avoid replaying history) sees this as a new event.
        speech.append_start("beta-id", "I am beta speaking.")
        app._speech_player.tick()
        app._speech_bar.refresh_now()  # type: ignore[union-attr]
        await app.action_jump_speaking()
        await pilot.pause()

    assert len(captured) == 1
    assert captured[0].session_id == "beta-id"


@pytest.mark.asyncio
async def test_jump_speaking_toasts_when_nothing_is_speaking(
    populated_dir: Path,
    speech_log: Path,
) -> None:
    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        await app.action_jump_speaking()
        await pilot.pause()
        # No speech recorded → toast should mention it. We just assert the
        # action did not raise; the visible text is rendered into _toast.
        assert app._toast is not None


# ---- mute hotkey --------------------------------------------------------


@pytest.mark.asyncio
async def test_action_toggle_mute_flips_player_and_persists(
    populated_dir: Path,
    speech_log: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`m` flips the player's mute state AND writes the new value to disk
    so the choice survives a cco restart."""
    monkeypatch.setenv("CCO_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("CCO_TTS_ENABLED", raising=False)
    from claude_orchestrator import speech_settings

    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        starting = app._speech_player.is_muted

        app.action_toggle_mute()
        await pilot.pause()
        assert app._speech_player.is_muted is not starting

        # Persistence: the file now reflects the toggled state.
        loaded = speech_settings.load()
        assert loaded.enabled is starting  # enabled inverts muted

        # Toggle again returns to the original.
        app.action_toggle_mute()
        await pilot.pause()
        assert app._speech_player.is_muted is starting


@pytest.mark.asyncio
async def test_bar_shows_muted_icon_when_muted(
    populated_dir: Path,
    speech_log: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CCO_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("CCO_TTS_ENABLED", raising=False)

    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        # Force muted from a known starting state.
        app._speech_player.set_muted(True)
        speech.append_start("alpha-id", "Some response.")
        app._speech_player.tick()
        app._speech_bar.refresh_now()  # type: ignore[union-attr]
        rendered = str(app._speech_bar.render())  # type: ignore[union-attr]
        # 🔇 muted glyph; no 🔊.
        assert "🔇" in rendered
        assert "🔊" not in rendered


# ---- per-row speaking indicator -----------------------------------------


@pytest.mark.asyncio
async def test_row_marker_appears_on_speaking_session(
    populated_dir: Path, speech_log: Path
) -> None:
    """The row whose session is speaking must render the 🔊 marker; others
    must not. Lets users spot the speaker at a glance without consulting
    the bar."""
    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        # Append AFTER app creation; the watcher starts at EOF on init.
        speech.append_start("alpha-id", "Hello from alpha.")
        # Tick player + bar so the speaker is observable, then refresh the
        # table so the marker is propagated into the rows.
        app._speech_player.tick()
        app._speech_bar.refresh_now()  # type: ignore[union-attr]
        await app._refresh_table()
        await pilot.pause()
        alpha_text = app._rows_by_sid["alpha-id"].render()  # type: ignore[union-attr]
        beta_text = app._rows_by_sid["beta-id"].render()  # type: ignore[union-attr]
        # Speaking row gets the ▌ accent prefix; non-speaking does not.
        assert "▌" in str(alpha_text)
        assert "▌" not in str(beta_text)


@pytest.mark.asyncio
async def test_row_marker_clears_when_speech_stops(populated_dir: Path, speech_log: Path) -> None:
    speech.append_start("alpha-id", "Hello.")
    speech.append_stop("alpha-id")

    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        app._speech_player.tick()
        app._speech_bar.refresh_now()  # type: ignore[union-attr]
        await app._refresh_table()
        await pilot.pause()
        alpha_text = str(app._rows_by_sid["alpha-id"].render())  # type: ignore[union-attr]
        assert "▌" not in alpha_text


@pytest.mark.asyncio
async def test_action_toggle_speak_mode_flips_player_and_persists(
    populated_dir: Path,
    speech_log: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`M` flips between full and summary modes, persists to disk, and
    keeps the player's runtime mode in sync."""
    monkeypatch.setenv("CCO_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("CCO_TTS_ENABLED", raising=False)
    monkeypatch.delenv("CCO_TTS_MODE", raising=False)
    from claude_orchestrator import speech_settings

    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        # Default is "full" — a clean install never starts in summary mode
        # (that would surprise existing users on first launch after upgrade).
        assert app._speech_player.speak_mode == speech_settings.SPEAK_MODE_FULL

        app.action_toggle_speak_mode()
        await pilot.pause()
        assert app._speech_player.speak_mode == speech_settings.SPEAK_MODE_SUMMARY
        # Persistence: the file reflects the new mode.
        assert speech_settings.load().speak_mode == speech_settings.SPEAK_MODE_SUMMARY

        # Toggle back.
        app.action_toggle_speak_mode()
        await pilot.pause()
        assert app._speech_player.speak_mode == speech_settings.SPEAK_MODE_FULL
        assert speech_settings.load().speak_mode == speech_settings.SPEAK_MODE_FULL

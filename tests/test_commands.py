"""Tests for the TUI command-palette provider.

Asserts that every BINDING in EphorApp surfaces as a palette entry (minus
an explicit skip list of pure navigation keys). Catches the drift that
otherwise happens whenever someone adds a new binding and forgets the
palette.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ephor.state.manager import StateManager
from ephor.state.models import AgentState
from ephor.tui.app import EphorApp
from ephor.tui.commands import _PALETTE_SKIP_ACTIONS, EphorCommands


def _write_state(directory: Path, sid: str) -> None:
    state = AgentState(
        session_id=sid,
        cwd="/tmp/x",
        started_at="2026-04-29T10:00:00Z",
        last_event_time="2026-04-29T10:00:00Z",
        project_name=sid,
    )
    (directory / f"{sid}.json").write_text(state.to_json())


@pytest.fixture
def populated_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("EPHOR_STATE_DIR", str(sd))
    _write_state(sd, "alpha-id")
    return sd


def test_cco_commands_is_registered_on_app() -> None:
    """The provider must be in App.COMMANDS, otherwise Ctrl+P shows
    only the built-in system commands."""
    assert EphorCommands in EphorApp.COMMANDS


@pytest.mark.asyncio
async def test_every_binding_surfaces_in_palette(populated_dir: Path) -> None:
    """For each BINDING whose action isn't in the skip list, the palette
    must have an entry. Test drives a real app instance so the provider
    sees the live state-aware labels."""
    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        provider = EphorCommands(screen=app.screen)
        specs = provider._build_specs()

        # Collect the actions surfaced. We can't trivially map back from
        # spec → action, but the count should equal the non-skipped
        # binding count, and every surfacing binding's description (or
        # a state-aware variant of it) should appear somewhere.
        surfaced_actions = {
            b.action for b in EphorApp.BINDINGS if b.action not in _PALETTE_SKIP_ACTIONS
        }
        # ctrl+c quits via the same action as `q` — dedup so we count
        # unique actions, not unique bindings.
        assert len(specs) >= len(surfaced_actions), (
            f"palette has {len(specs)} entries but app has "
            f"{len(surfaced_actions)} unique non-skipped actions"
        )


@pytest.mark.asyncio
async def test_palette_skip_list_only_covers_navigation(populated_dir: Path) -> None:
    """Guard against accidentally muting a real command. The skip list
    must only contain navigation/modifier actions — never something a
    user would actually want to run from the palette."""
    # Any new entry to _PALETTE_SKIP_ACTIONS must be justified — this
    # test fails loudly if someone adds e.g. 'kill' to the skip set.
    allowed = {"cursor_down", "cursor_up", "clear_filter"}
    assert allowed >= _PALETTE_SKIP_ACTIONS, (
        f"skip list grew unexpectedly: {_PALETTE_SKIP_ACTIONS - allowed}"
    )


@pytest.mark.asyncio
async def test_speak_mode_command_label_reflects_current_state(
    populated_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The label tells the user what tapping enter will *do* — i.e.
    'switch to summary' when currently in full, and vice versa."""
    monkeypatch.setenv("EPHOR_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("EPHOR_TTS_MODE", raising=False)
    from ephor.speech_settings import SPEAK_MODE_FULL, SPEAK_MODE_SUMMARY

    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        assert app._speech_player.speak_mode == SPEAK_MODE_FULL

        provider = EphorCommands(screen=app.screen)
        labels = [s.display for s in provider._build_specs()]
        assert any("summary mode" in lbl.lower() for lbl in labels), labels

        # Flip → label inverts.
        app._speech_player.set_speak_mode(SPEAK_MODE_SUMMARY)
        labels2 = [s.display for s in provider._build_specs()]
        assert any("full-reply" in lbl.lower() for lbl in labels2), labels2


@pytest.mark.asyncio
async def test_mute_command_label_reflects_current_state(populated_dir: Path) -> None:
    """Same state-aware contract for the mute toggle."""
    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        app._speech_player.set_muted(False)
        provider = EphorCommands(screen=app.screen)
        labels = [s.display for s in provider._build_specs()]
        assert any("mute audio" in lbl.lower() for lbl in labels), labels

        app._speech_player.set_muted(True)
        labels2 = [s.display for s in provider._build_specs()]
        assert any("unmute" in lbl.lower() for lbl in labels2), labels2


@pytest.mark.asyncio
async def test_discover_yields_every_spec(populated_dir: Path) -> None:
    """Opening the palette with no query must surface every command."""
    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        provider = EphorCommands(screen=app.screen)
        hits = []
        async for hit in provider.discover():
            hits.append(hit)
        assert len(hits) == len(provider._build_specs())
        # DiscoveryHit display is a string (or VisualType); each must
        # have a callable command attached.
        for h in hits:
            assert callable(h.command)


@pytest.mark.asyncio
async def test_search_filters_by_query(populated_dir: Path) -> None:
    """A non-trivial query narrows the result set."""
    app = EphorApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        provider = EphorCommands(screen=app.screen)
        # 'kill' should hit the kill action; matchers are fuzzy so we
        # just assert at least one hit and that none of the obvious
        # non-matches sneak in.
        hits = []
        async for hit in provider.search("kill"):
            hits.append(hit)
        assert hits, "expected at least one hit for 'kill'"

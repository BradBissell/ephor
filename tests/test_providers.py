"""Tests for the coding-agent provider registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from ephor import providers


def test_all_providers_present_and_ordered() -> None:
    names = [p.name for p in providers.all_providers()]
    assert names == ["claude", "gemini", "codex", "grok", "opencode"]
    assert providers.PROVIDER_ORDER == ("claude", "gemini", "codex", "grok", "opencode")


@pytest.mark.parametrize("name", ["claude", "gemini", "codex", "grok", "opencode"])
def test_provider_is_well_formed(name: str) -> None:
    p = providers.get_provider(name)
    assert p.name == name
    assert p.display_name
    assert p.binary
    assert p.events, "every provider must register at least one event"
    # No accidental duplicate event registrations.
    assert len(set(p.events)) == len(p.events)


def test_get_provider_unknown_raises() -> None:
    with pytest.raises(KeyError):
        providers.get_provider("copilot")


def test_resolve_providers_default_is_claude() -> None:
    assert [p.name for p in providers.resolve_providers(None)] == ["claude"]


def test_resolve_providers_all() -> None:
    assert [p.name for p in providers.resolve_providers("all")] == list(providers.PROVIDER_ORDER)


def test_resolve_providers_single() -> None:
    assert [p.name for p in providers.resolve_providers("codex")] == ["codex"]


def test_known_binaries_match_providers() -> None:
    assert providers.KNOWN_BINARIES == ("claude", "gemini", "codex", "grok", "opencode")


def test_settings_path_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROK_HOOKS_PATH", "/tmp/custom/grok.json")
    assert providers.get_provider("grok").settings_path() == Path("/tmp/custom/grok.json")


def test_settings_path_default_expands_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_SETTINGS_PATH", raising=False)
    p = providers.get_provider("gemini").settings_path()
    assert p.name == "settings.json"
    assert ".gemini" in str(p)


def test_gemini_uses_before_after_event_dialect() -> None:
    events = providers.get_provider("gemini").events
    assert "BeforeTool" in events and "AfterAgent" in events
    # Gemini does NOT use Claude's PreToolUse name.
    assert "PreToolUse" not in events


def test_grok_carries_resume_flags() -> None:
    assert providers.get_provider("grok").resume_flags == ("--resume", "-r")

"""Tests for the hook installer.

Critical property: install → uninstall round-trip must be byte-identical when
the user has no other hooks, AND must preserve all non-ephor hook entries when
they coexist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ephor.hooks import installer


@pytest.fixture
def fake_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Bundle a fake handler script so installer.handler_path resolves cleanly."""
    handler = tmp_path / "fake_event_handler.sh"
    handler.write_text("#!/bin/sh\nexit 0\n")
    handler.chmod(0o755)
    monkeypatch.setattr(installer, "hook_handler_path", lambda: handler)
    return handler


@pytest.fixture
def settings_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "settings.json"
    monkeypatch.setattr(installer, "claude_settings_path", lambda: p)
    return p


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_into_empty_settings(settings_path: Path, fake_handler: Path) -> None:
    plan = installer.install(dry_run=False)
    assert plan.events_to_add == list(installer.EPHOR_EVENTS)
    assert plan.events_already_installed == []
    assert plan.backup_path is not None  # backup of "empty" file (sentinel)

    data = json.loads(settings_path.read_text())
    assert "hooks" in data
    for event in installer.EPHOR_EVENTS:
        assert event in data["hooks"]
        entries = data["hooks"][event]
        assert any(str(fake_handler) in h["hooks"][0]["command"] for h in entries)


def test_install_dry_run_does_not_write(settings_path: Path, fake_handler: Path) -> None:
    plan = installer.install(dry_run=True)
    assert plan.events_to_add == list(installer.EPHOR_EVENTS)
    assert not settings_path.exists()


def test_install_idempotent(settings_path: Path, fake_handler: Path) -> None:
    installer.install()
    plan = installer.install()  # second time should be a no-op
    assert plan.events_to_add == []
    assert sorted(plan.events_already_installed) == sorted(installer.EPHOR_EVENTS)


def test_install_preserves_existing_hooks(settings_path: Path, fake_handler: Path) -> None:
    # Simulate the user's actual ~/.claude/settings.json with gsd-* hooks.
    pre = {
        "permissions": {"defaultMode": "auto"},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Grep|Glob|Read|Search",
                    "hooks": [{"type": "command", "command": "/path/to/gsd-guard.js"}],
                }
            ],
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "/path/to/gsd-stop.sh"}],
                }
            ],
        },
        "statusLine": {"type": "command", "command": "/path/to/statusline.js"},
    }
    settings_path.write_text(json.dumps(pre, indent=2))
    installer.install()

    data = json.loads(settings_path.read_text())
    # Existing entries remain (first in list is the user's gsd entry).
    assert data["permissions"] == {"defaultMode": "auto"}
    assert data["statusLine"]["command"] == "/path/to/statusline.js"
    pre_tool_cmds = [h["hooks"][0]["command"] for h in data["hooks"]["PreToolUse"]]
    assert "/path/to/gsd-guard.js" in pre_tool_cmds
    assert any(str(fake_handler) in c for c in pre_tool_cmds)


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def test_uninstall_removes_all_cco_entries(settings_path: Path, fake_handler: Path) -> None:
    installer.install()
    plan = installer.uninstall()
    assert sorted(plan.events_with_ephor_hook) == sorted(installer.EPHOR_EVENTS)

    data = json.loads(settings_path.read_text()) if settings_path.read_text() else {}
    # No "hooks" key when nothing else was using it.
    assert "hooks" not in data


def test_uninstall_dry_run_does_not_write(settings_path: Path, fake_handler: Path) -> None:
    installer.install()
    before = settings_path.read_bytes()
    plan = installer.uninstall(dry_run=True)
    assert sorted(plan.events_with_ephor_hook) == sorted(installer.EPHOR_EVENTS)
    assert settings_path.read_bytes() == before


def test_uninstall_preserves_other_hooks(settings_path: Path, fake_handler: Path) -> None:
    pre = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Grep",
                    "hooks": [{"type": "command", "command": "/x/gsd.js"}],
                }
            ]
        }
    }
    settings_path.write_text(json.dumps(pre, indent=2))
    installer.install()
    installer.uninstall()
    data = json.loads(settings_path.read_text())
    pre_tool = data["hooks"]["PreToolUse"]
    assert len(pre_tool) == 1
    assert pre_tool[0]["hooks"][0]["command"] == "/x/gsd.js"


# ---------------------------------------------------------------------------
# property: byte-identical roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    [
        # No prior hooks at all — empty file.
        {},
        # Only unrelated keys.
        {
            "permissions": {"defaultMode": "auto"},
            "statusLine": {"type": "command", "command": "/x"},
        },
        # Existing gsd-style hooks, no ephor.
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Grep", "hooks": [{"type": "command", "command": "/x/gsd.js"}]}
                ],
                "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "/x/stop.sh"}]}],
            }
        },
        # Mixed: existing hooks + extra config keys.
        {
            "permissions": {"defaultMode": "auto"},
            "hooks": {
                "PreToolUse": [
                    {"matcher": "", "hooks": [{"type": "command", "command": "/a/b.sh"}]}
                ]
            },
            "statusLine": {"type": "command", "command": "/x"},
        },
    ],
)
def test_install_uninstall_roundtrip_preserves_user_hooks(
    settings_path: Path,
    fake_handler: Path,
    scenario: dict,
) -> None:
    """install → uninstall must restore the user's hooks dict exactly."""
    if scenario:
        settings_path.write_text(json.dumps(scenario, indent=2) + "\n")
    before_user_hooks = scenario.get("hooks", {})
    before_other = {k: v for k, v in scenario.items() if k != "hooks"}

    installer.install()
    installer.uninstall()

    if not settings_path.exists() or not settings_path.read_text().strip():
        # File only ever contained ephor hooks → uninstall correctly removed it
        # OR the file remained empty.
        assert not before_user_hooks, "cleared file but user had hooks"
        assert not before_other, "cleared file but user had other config"
        return

    after = json.loads(settings_path.read_text())
    after_hooks = after.get("hooks", {})
    after_other = {k: v for k, v in after.items() if k != "hooks"}

    assert after_other == before_other, "non-hook config keys must be preserved"
    assert after_hooks == before_user_hooks, "user hooks must be preserved exactly"


# ---------------------------------------------------------------------------
# atomic write + backup behavior
# ---------------------------------------------------------------------------


def test_atomic_write_creates_file_with_0600(tmp_path: Path) -> None:
    p = tmp_path / "x"
    installer._atomic_write(p, "hello\n")
    assert p.read_text() == "hello\n"
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600


def test_atomic_write_no_tempfile_left_on_disk(tmp_path: Path) -> None:
    p = tmp_path / "x"
    installer._atomic_write(p, "hi\n")
    leftovers = [q for q in tmp_path.iterdir() if q.name.startswith(".x.")]
    assert leftovers == []


def test_install_creates_backup(settings_path: Path, fake_handler: Path) -> None:
    settings_path.write_text("{}\n")
    plan = installer.install()
    assert plan.backup_path is not None
    assert plan.backup_path.exists()
    assert plan.backup_path.read_text() == "{}\n"


def test_backups_rotate_to_keep_last_three(settings_path: Path, fake_handler: Path) -> None:
    settings_path.write_text("{}\n")
    # Run install/uninstall cycles to accumulate backups.
    for _ in range(5):
        installer.install()
        installer.uninstall()

    backups = sorted(settings_path.parent.glob(f"{settings_path.name}.bak.*"))
    assert len(backups) <= installer.BACKUP_KEEP


def test_restore_backup(settings_path: Path, fake_handler: Path) -> None:
    settings_path.write_text('{"original": true}\n')
    installer.install()
    # Sanity: ephor hooks are now in the file.
    assert "hooks" in json.loads(settings_path.read_text())

    restored = installer.restore_backup()
    assert restored is not None
    data = json.loads(settings_path.read_text())
    assert data == {"original": True}


def test_restore_backup_with_no_backup_returns_none(
    settings_path: Path, fake_handler: Path
) -> None:
    # No prior install → no backups exist.
    assert installer.restore_backup() is None


# ---------------------------------------------------------------------------
# speech install (handing TTS playback to ephor)
# ---------------------------------------------------------------------------


def _settings_with_tts_and_cco_hooks(handler: Path) -> dict:
    """Mimic a user's settings.json that has both ephor's event_handler.sh
    AND tts-speak-response wired to the Stop hook."""
    return {
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f'EPHOR_EVENT=Stop "{handler}"',
                            "async": True,
                        },
                        {
                            "type": "command",
                            "command": "~/.claude/hooks/tts-speak-response",
                            "async": True,
                        },
                    ],
                },
            ],
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "tts-stop 2>/dev/null; exit 0",
                            "async": True,
                        }
                    ],
                }
            ],
        }
    }


def test_speech_install_removes_only_tts_entries(settings_path: Path, fake_handler: Path) -> None:
    settings_path.write_text(json.dumps(_settings_with_tts_and_cco_hooks(fake_handler)))
    plan = installer.install_speech(dry_run=False)

    assert plan.affected_events == ["Stop"]
    assert any("tts-speak-response" in c for c in plan.affected_commands)

    after = json.loads(settings_path.read_text())
    stop_inner = after["hooks"]["Stop"][0]["hooks"]
    # ephor's event_handler stays.
    assert any("event_handler" in str(h.get("command", "")) for h in stop_inner)
    # tts-speak-response is gone.
    assert not any("tts-speak-response" in str(h.get("command", "")) for h in stop_inner)
    # tts-stop on UserPromptSubmit is unrelated to tts-speak-response and
    # must NOT be touched by `speech install`.
    ups_inner = after["hooks"]["UserPromptSubmit"][0]["hooks"]
    assert any("tts-stop" in str(h.get("command", "")) for h in ups_inner)


def test_speech_install_dry_run_writes_nothing(settings_path: Path, fake_handler: Path) -> None:
    raw = json.dumps(_settings_with_tts_and_cco_hooks(fake_handler))
    settings_path.write_text(raw)
    plan = installer.install_speech(dry_run=True)
    assert plan.affected_events == ["Stop"]
    assert plan.backup_path is None
    assert settings_path.read_text() == raw


def test_speech_install_no_op_when_no_tts_hook(settings_path: Path, fake_handler: Path) -> None:
    """User who never set up tts-speak-response should get a clean no-op."""
    installer.install(dry_run=False)  # only ephor's hooks present
    plan = installer.install_speech(dry_run=False)
    assert plan.affected_events == []
    assert plan.backup_path is None


def test_speech_install_drops_event_when_tts_was_only_hook(
    settings_path: Path, fake_handler: Path
) -> None:
    """A user with ONLY tts-speak-response on Stop: removing it should
    delete the empty Stop list AND drop hooks entirely if it's the only
    event left, so the resulting JSON is minimal."""
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "~/.claude/hooks/tts-speak-response",
                                    "async": True,
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )
    installer.install_speech(dry_run=False)
    after = json.loads(settings_path.read_text())
    assert "hooks" not in after, "Stop should be dropped, then empty hooks dict removed"


# ---------------------------------------------------------------------------
# multi-provider install (gemini / codex / grok)
# ---------------------------------------------------------------------------


@pytest.fixture
def provider_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point every provider's settings file at an isolated tmp path via its
    documented env override, and bundle a fake handler."""
    handler = tmp_path / "fake_event_handler.sh"
    handler.write_text("#!/bin/sh\nexit 0\n")
    handler.chmod(0o755)
    monkeypatch.setattr(installer, "hook_handler_path", lambda: handler)

    paths = {
        "claude": tmp_path / "claude.json",
        "gemini": tmp_path / "gemini.json",
        "codex": tmp_path / "codex-hooks.json",
        "grok": tmp_path / "grok.json",
    }
    monkeypatch.setattr(installer, "claude_settings_path", lambda: paths["claude"])
    monkeypatch.setenv("GEMINI_SETTINGS_PATH", str(paths["gemini"]))
    monkeypatch.setenv("CODEX_HOOKS_PATH", str(paths["codex"]))
    monkeypatch.setenv("GROK_SETTINGS_PATH", str(paths["grok"]))
    return paths


@pytest.mark.parametrize("provider", ["gemini", "codex", "grok"])
def test_install_registers_provider_events_and_tag(
    provider: str, provider_paths: dict[str, Path]
) -> None:
    from ephor.providers import get_provider

    prov = get_provider(provider)
    plan = installer.install(provider)
    assert sorted(plan.events_to_add) == sorted(prov.events)

    data = json.loads(provider_paths[provider].read_text())
    for event in prov.events:
        cmd = data["hooks"][event][0]["hooks"][0]["command"]
        # Every registered command tags the handler with its provider.
        assert f"EPHOR_PROVIDER={provider}" in cmd


def test_install_all_providers_are_isolated(provider_paths: dict[str, Path]) -> None:
    for name in ("claude", "gemini", "codex", "grok"):
        installer.install(name)
    # Each provider's events landed only in its own file.
    from ephor.providers import get_provider

    for name in ("claude", "gemini", "codex", "grok"):
        data = json.loads(provider_paths[name].read_text())
        assert sorted(data["hooks"].keys()) == sorted(get_provider(name).events)


@pytest.mark.parametrize("provider", ["gemini", "codex", "grok"])
def test_install_uninstall_roundtrip_per_provider(
    provider: str, provider_paths: dict[str, Path]
) -> None:
    installer.install(provider)
    plan = installer.uninstall(provider)
    from ephor.providers import get_provider

    assert sorted(plan.events_with_ephor_hook) == sorted(get_provider(provider).events)
    # File emptied of hooks after uninstall.
    data = json.loads(provider_paths[provider].read_text())
    assert "hooks" not in data


def test_codex_writes_dedicated_hooks_json(provider_paths: dict[str, Path]) -> None:
    installer.install("codex")
    # Codex keeps hooks in its own file, not ~/.claude/settings.json.
    assert provider_paths["codex"].exists()
    assert not provider_paths["claude"].exists()


# ---------------------------------------------------------------------------
# opencode plugin strategy
# ---------------------------------------------------------------------------


@pytest.fixture
def opencode_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake handler (with the real template beside it) + tmp plugin path."""
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    handler = hooks_dir / "event_handler.sh"
    handler.write_text("#!/bin/sh\nexit 0\n")
    handler.chmod(0o755)
    # The installer reads opencode_plugin.js next to the handler.
    (hooks_dir / "opencode_plugin.js").write_text(
        'const HANDLER = "__EPHOR_HANDLER_PATH__";\nexport const ephor = async () => ({});\n'
    )
    monkeypatch.setattr(installer, "hook_handler_path", lambda: handler)
    plugin = tmp_path / "opencode" / "plugins" / "ephor.js"
    monkeypatch.setenv("EPHOR_OPENCODE_PLUGIN", str(plugin))
    return plugin


def test_opencode_install_writes_plugin_with_handler_path(opencode_paths: Path) -> None:
    plan = installer.install("opencode")
    assert plan.events_to_add  # something to do on a fresh install
    assert opencode_paths.exists()
    content = opencode_paths.read_text()
    # Placeholder replaced with the (fake) handler's absolute path.
    assert "__EPHOR_HANDLER_PATH__" not in content
    assert "event_handler.sh" in content


def test_opencode_install_is_idempotent(opencode_paths: Path) -> None:
    installer.install("opencode")
    plan = installer.install("opencode")  # already current
    assert plan.events_to_add == []
    assert plan.events_already_installed


def test_opencode_install_rewrites_stale_plugin(opencode_paths: Path) -> None:
    opencode_paths.parent.mkdir(parents=True, exist_ok=True)
    opencode_paths.write_text('const HANDLER = "/old/stale/path";\n')  # pretend prior install
    plan = installer.install("opencode")
    assert plan.events_to_add  # detected stale → rewrites
    assert "/old/stale/path" not in opencode_paths.read_text()


def test_opencode_uninstall_removes_plugin(opencode_paths: Path) -> None:
    installer.install("opencode")
    assert opencode_paths.exists()
    plan = installer.uninstall("opencode")
    assert plan.events_with_ephor_hook
    assert not opencode_paths.exists()


def test_opencode_uninstall_noop_when_absent(opencode_paths: Path) -> None:
    plan = installer.uninstall("opencode")
    assert plan.events_with_ephor_hook == []

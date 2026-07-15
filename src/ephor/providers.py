"""Coding-agent CLI providers.

ephor started as a Claude-Code-only dashboard. Four terminal coding agents now
expose the same fundamental hook contract Claude Code pioneered — a shell
command invoked with event JSON on **stdin**, carrying at least a session id, a
working directory, and an event name. This module describes each one so the
rest of ephor (installer, discovery, doctor) can stay provider-agnostic.

The five supported/known agents and how they differ:

  claude  ~/.claude/settings.json          JSON `hooks` obj   stdin JSON
  gemini  ~/.gemini/settings.json           JSON `hooks` obj   stdin JSON  (diff event names)
  codex   ~/.codex/hooks.json               JSON hooks file    stdin JSON
  grok    ~/.grok/user-settings.json        JSON `hooks` obj   stdin JSON  (superagent-ai/grok-cli)

Claude, Gemini and Grok share the *identical* settings-file `hooks` shape, so
they use the same installer strategy (`SETTINGS_JSON_HOOKS`); Codex keeps its
hooks in a dedicated `hooks.json` (`CODEX_HOOKS_JSON`).

OpenCode is different: it has no shell hooks, only JS/TS plugins. So its
strategy (`OPENCODE_PLUGIN`) installs a small generated plugin into
`~/.config/opencode/plugins/`. That plugin's `event` hook translates OpenCode's
bus events (session.status/idle, permission.*, tool.execute.*) into ephor's
canonical event JSON and pipes it — via Bun's `$` shell — to the SAME
`hooks/event_handler.sh`. So every agent, hook-based or plugin-based, funnels
through one state writer; the handler is told which agent fired it via
`EPHOR_PROVIDER`, and understands each one's event-name/field-name dialect.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Installer strategies — how a provider's config is mutated to register a hook.
SETTINGS_JSON_HOOKS = (
    "settings_json_hooks"  # claude/gemini/grok: `hooks` obj in a JSON settings file
)
CODEX_HOOKS_JSON = "codex_hooks_json"  # codex: a dedicated ~/.codex/hooks.json
OPENCODE_PLUGIN = "opencode_plugin"  # opencode: a JS plugin file that shells out to the handler


@dataclass(frozen=True)
class Provider:
    """Static description of one coding-agent CLI ephor can monitor."""

    name: str  # canonical short id, e.g. "claude" — also the EPHOR_PROVIDER value
    display_name: str  # human label, e.g. "Claude Code"
    binary: str  # process/command name, used for `pgrep -x <binary>` and PATH checks
    install_kind: str  # one of the *_HOOKS constants above
    # Native event names to register the handler for. The handler maps each to a
    # status; unknown events are recorded but don't fabricate a status.
    events: tuple[str, ...]
    # Path to the config/settings file the installer mutates. `_settings`
    # holds the default; `settings_env` names an env var that overrides it
    # (used by tests and non-default installs).
    _settings: str = ""
    settings_env: str = ""
    # cmdline flags that immediately precede a session id (best-effort argv
    # parse in discover.py; the @ephor_sid pane tag is the robust path).
    resume_flags: tuple[str, ...] = ()
    # Command prefix used by summary-mode TTS to condense a reply, e.g.
    # ("claude", "-p"). None → summary mode falls back to the full reply.
    summarize_cmd: tuple[str, ...] | None = None
    # Extra key/value pairs added to each inner hook object. Claude (and the
    # agents that inherit its `hooks` shape) mark the hook non-blocking with
    # `async: true`. Codex, despite sharing the JSON shape, does NOT support
    # async hooks — it *silently skips* any entry carrying `async`, so ephor's
    # hooks never run. Codex therefore overrides this to drop `async` (a
    # `timeout` guard instead).
    hook_entry_extra: tuple[tuple[str, object], ...] = (("async", True),)

    def settings_path(self) -> Path:
        """Resolve the config file the installer reads/writes for this provider."""
        raw = os.environ.get(self.settings_env) if self.settings_env else None
        if raw:
            return Path(raw).expanduser()
        return Path(os.path.expanduser(self._settings))


# Event registration lists. Kept close to each CLI's real lifecycle so we don't
# clutter a user's settings file with events the agent never emits.

# Claude Code — preserves ephor's original registration set (a superset; Claude
# simply never fires the events it doesn't emit).
_CLAUDE_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Notification",
    "PermissionRequest",
    "PermissionDenied",
    "Stop",
    "StopFailure",
    "SessionEnd",
    "SubagentStart",
    "SubagentStop",
)

# Gemini CLI — distinct vocabulary (Before*/After*), permission surfaced via
# Notification(notification_type="ToolPermission").
_GEMINI_EVENTS = (
    "SessionStart",
    "SessionEnd",
    "BeforeAgent",
    "AfterAgent",
    "BeforeTool",
    "AfterTool",
    "Notification",
)

# Codex CLI — real hook events (no SessionEnd); permission via PermissionRequest.
_CODEX_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
)

# Grok CLI (superagent-ai/grok-cli) — near-identical to Claude's set.
_GROK_EVENTS = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Notification",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
)

# OpenCode — canonical events our bundled plugin maps its bus events onto (not
# registered per-event like the others; listed for doctor/dry-run display).
_OPENCODE_EVENTS = (
    "SessionStart",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
    "SessionEnd",
)


PROVIDERS: dict[str, Provider] = {
    "claude": Provider(
        name="claude",
        display_name="Claude Code",
        binary="claude",
        install_kind=SETTINGS_JSON_HOOKS,
        events=_CLAUDE_EVENTS,
        _settings="~/.claude/settings.json",
        settings_env="CLAUDE_SETTINGS_PATH",
        resume_flags=("--resume", "-r"),
        summarize_cmd=("claude", "-p"),
    ),
    "gemini": Provider(
        name="gemini",
        display_name="Gemini CLI",
        binary="gemini",
        install_kind=SETTINGS_JSON_HOOKS,
        events=_GEMINI_EVENTS,
        _settings="~/.gemini/settings.json",
        settings_env="GEMINI_SETTINGS_PATH",
        resume_flags=(),
        summarize_cmd=("gemini", "-p"),
    ),
    "codex": Provider(
        name="codex",
        display_name="Codex CLI",
        binary="codex",
        install_kind=CODEX_HOOKS_JSON,
        events=_CODEX_EVENTS,
        _settings="~/.codex/hooks.json",
        settings_env="CODEX_HOOKS_PATH",
        resume_flags=(),
        summarize_cmd=("codex", "exec"),
        # Codex skips async hooks ("async hooks are not supported yet"), so use
        # a plain synchronous entry with a timeout guard. The handler is <15ms
        # and fails open, so running synchronously is safe.
        hook_entry_extra=(("timeout", 30),),
    ),
    "grok": Provider(
        name="grok",
        display_name="Grok (Build) CLI",
        binary="grok",
        install_kind=SETTINGS_JSON_HOOKS,
        events=_GROK_EVENTS,
        # xAI's Grok Build reads hook JSON files from ~/.grok/hooks/ (its native
        # location) — NOT ~/.grok/user-settings.json. It also scans
        # ~/.claude/settings.json by default (Claude compat), and sends a
        # camelCase/snake_case payload the handler normalizes; grok sessions are
        # detected at runtime via the GROK_SESSION_ID env var it sets.
        _settings="~/.grok/hooks/ephor.json",
        settings_env="GROK_HOOKS_PATH",
        resume_flags=("--resume", "-r"),
        summarize_cmd=None,
    ),
    "opencode": Provider(
        name="opencode",
        display_name="OpenCode",
        binary="opencode",
        install_kind=OPENCODE_PLUGIN,
        events=_OPENCODE_EVENTS,
        _settings="~/.config/opencode/plugins/ephor.js",
        settings_env="EPHOR_OPENCODE_PLUGIN",
        resume_flags=(),
        summarize_cmd=None,
    ),
}

# Stable display order for CLI output (`ephor init --provider all`, doctor).
PROVIDER_ORDER: tuple[str, ...] = ("claude", "gemini", "codex", "grok", "opencode")

# Binaries we scan for in process discovery, in preference order.
KNOWN_BINARIES: tuple[str, ...] = tuple(PROVIDERS[n].binary for n in PROVIDER_ORDER)


def get_provider(name: str) -> Provider:
    """Look up a provider by canonical name. Raises KeyError on unknown name."""
    return PROVIDERS[name]


def all_providers() -> list[Provider]:
    """Every provider in stable display order."""
    return [PROVIDERS[n] for n in PROVIDER_ORDER]


def resolve_providers(selector: str | None) -> list[Provider]:
    """Map a CLI `--provider` selector to Provider objects.

    None → default (`claude`, preserving pre-multi-CLI behaviour).
    "all" → every provider. Otherwise a single provider name.
    Raises KeyError for an unknown name so the CLI can report it.
    """
    if selector is None:
        return [PROVIDERS["claude"]]
    if selector == "all":
        return all_providers()
    return [PROVIDERS[selector]]

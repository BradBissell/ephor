"""Hook installer: mutates ~/.claude/settings.json to register ephor's hooks.

Discipline:
- Atomic writes (tempfile + fsync + os.replace) — never leave a half-written file.
- Backup before any mutation. Keep last 3 rotations.
- Coexists with the user's other hooks (gsd-*, cbm-*, …) by matching on our
  command path; we only ever add or remove entries that point at *our*
  event_handler.sh. Other entries are untouched.
- `--dry-run` prints the planned diff without writing.
- `--restore-backup` rescue mode: replace settings.json with the latest backup.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ephor.config import claude_settings_path, hook_handler_path
from ephor.providers import OPENCODE_PLUGIN, Provider, get_provider

# Placeholder in opencode_plugin.js that the installer replaces with the
# absolute event_handler.sh path at install time.
_PLUGIN_HANDLER_PLACEHOLDER = "__EPHOR_HANDLER_PATH__"

# Default provider when a caller doesn't specify one — preserves ephor's
# original Claude-Code-only behaviour.
_DEFAULT_PROVIDER = "claude"

# Hook events ephor subscribes to for Claude Code. Kept as a module constant
# (identical to the claude provider's event list) for backward compatibility;
# other providers carry their own event lists on the Provider object.
EPHOR_EVENTS = get_provider(_DEFAULT_PROVIDER).events

# Maximum number of rotated backups to retain.
BACKUP_KEEP = 3


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallPlan:
    """What `init` *would* do, separated from doing it (for --dry-run)."""

    settings_path: Path
    handler_path: Path
    events_to_add: list[str]
    events_already_installed: list[str]
    backup_path: Path | None  # None on dry-run

    def summary(self) -> str:
        lines = [
            f"settings file: {self.settings_path}",
            f"handler:       {self.handler_path}",
            f"backup:        {self.backup_path or '(dry-run, none written)'}",
            "",
        ]
        if self.events_already_installed:
            lines.append(f"already installed for: {', '.join(self.events_already_installed)}")
        if self.events_to_add:
            lines.append(f"will add hook for:    {', '.join(self.events_to_add)}")
        else:
            lines.append("nothing to do — hooks already installed for every event.")
        return "\n".join(lines)


@dataclass(frozen=True)
class UninstallPlan:
    """What `uninstall` *would* remove (for --dry-run)."""

    settings_path: Path
    handler_path: Path
    events_with_ephor_hook: list[str]
    backup_path: Path | None

    def summary(self) -> str:
        lines = [
            f"settings file: {self.settings_path}",
            f"handler match: {self.handler_path}",
            f"backup:        {self.backup_path or '(dry-run, none written)'}",
            "",
        ]
        if self.events_with_ephor_hook:
            lines.append(f"will remove ephor hook from: {', '.join(self.events_with_ephor_hook)}")
        else:
            lines.append("nothing to do — no ephor hooks present.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# atomic write + backup
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Write content to path atomically (tempfile + fsync + os.replace).

    A power loss or kill during write leaves either the old file intact or
    the new file fully written — never a half-state.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmpname, mode)
        os.replace(tmpname, path)
    except Exception:
        with _suppress_oserror():
            os.unlink(tmpname)
        raise


class _suppress_oserror:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, OSError)


def _make_backup(settings_path: Path) -> Path:
    """Copy settings.json to settings.json.bak.<unixtime>; rotate to BACKUP_KEEP."""
    if not settings_path.is_file():
        # Nothing to back up.
        return settings_path.with_suffix(settings_path.suffix + ".bak.empty")

    import time

    stamp = int(time.time())
    backup = settings_path.with_name(f"{settings_path.name}.bak.{stamp}")
    backup.write_bytes(settings_path.read_bytes())
    backup.chmod(0o600)
    _rotate_backups(settings_path)
    return backup


def _rotate_backups(settings_path: Path) -> None:
    backups = sorted(
        settings_path.parent.glob(f"{settings_path.name}.bak.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in backups[BACKUP_KEEP:]:
        with _suppress_oserror():
            stale.unlink()


def _latest_backup(settings_path: Path) -> Path | None:
    backups = sorted(
        settings_path.parent.glob(f"{settings_path.name}.bak.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return backups[0] if backups else None


# ---------------------------------------------------------------------------
# settings.json mutation
# ---------------------------------------------------------------------------


def _load_settings(path: Path) -> dict[str, Any]:
    """Load settings.json. Returns {} if file is missing."""
    if not path.is_file():
        return {}
    raw = path.read_text()
    return json.loads(raw) if raw.strip() else {}


def _resolve_provider(provider: Provider | str | None) -> Provider:
    """Normalise the provider argument to a Provider (default: claude)."""
    if provider is None:
        return get_provider(_DEFAULT_PROVIDER)
    if isinstance(provider, str):
        return get_provider(provider)
    return provider


def _settings_path_for(provider: Provider) -> Path:
    """Config file the installer reads/writes for this provider.

    Claude keeps routing through the module-level ``claude_settings_path`` so
    the legacy ``CLAUDE_SETTINGS_PATH`` env override (and tests that monkeypatch
    it) keep working; every other provider resolves via its own descriptor.
    """
    if provider.name == _DEFAULT_PROVIDER:
        return claude_settings_path()
    return provider.settings_path()


def _is_ephor_entry(entry: dict[str, Any], handler_path: Path) -> bool:
    """True iff this hook entry refers to OUR event_handler.sh."""
    hooks = entry.get("hooks") or []
    handler_str = str(handler_path)
    for h in hooks:
        cmd = h.get("command", "")
        if isinstance(cmd, str) and handler_str in cmd:
            return True
    return False


def _build_hook_entry(handler_path: Path, prov: Provider) -> dict[str, Any]:
    """Build the hooks-array entry inserted under each event.

    The command tags the handler with EPHOR_PROVIDER so the shell handler knows
    which agent fired it (for the recorded ``provider`` field and event-name
    dialect handling). The event name itself travels in the stdin JSON, so we
    don't need it on the command line.

    The inner hook object carries the provider's ``hook_entry_extra`` — ``async:
    true`` for Claude-shaped agents, but a ``timeout`` (no async) for Codex,
    which silently skips async hooks.
    """
    extra = dict(prov.hook_entry_extra)
    # hook_entry_extra is provider-authored; never let it silently clobber the
    # fixed fields (a stray "command" key would hijack the hook).
    assert not (extra.keys() & {"type", "command"}), (
        f"{prov.name}: hook_entry_extra must not override type/command"
    )
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": (f'EPHOR_PROVIDER={prov.name} "{handler_path}"'),
                **extra,
            }
        ],
    }


# ---------------------------------------------------------------------------
# opencode plugin strategy (no shell hooks — a generated JS plugin file)
# ---------------------------------------------------------------------------


def _opencode_plugin_source() -> str:
    """The plugin JS with the absolute handler path injected."""
    template = hook_handler_path().parent / "opencode_plugin.js"
    return template.read_text().replace(_PLUGIN_HANDLER_PLACEHOLDER, str(hook_handler_path()))


def _plan_install_plugin(prov: Provider) -> InstallPlan:
    path = _settings_path_for(prov)
    handler = hook_handler_path()
    try:
        current: str | None = path.read_text()
    except OSError:
        current = None
    # Compare against the desired content so a stale plugin (e.g. handler path
    # changed after a move/reinstall) is treated as "to install" and rewritten.
    up_to_date = current == _opencode_plugin_source()
    return InstallPlan(
        settings_path=path,
        handler_path=handler,
        events_to_add=[] if up_to_date else list(prov.events),
        events_already_installed=list(prov.events) if up_to_date else [],
        backup_path=None,
    )


def _install_plugin(prov: Provider, *, dry_run: bool) -> InstallPlan:
    plan = _plan_install_plugin(prov)
    if dry_run or not plan.events_to_add:
        return plan
    backup = _make_backup(plan.settings_path)
    _atomic_write(plan.settings_path, _opencode_plugin_source(), mode=0o600)
    return InstallPlan(
        settings_path=plan.settings_path,
        handler_path=plan.handler_path,
        events_to_add=plan.events_to_add,
        events_already_installed=plan.events_already_installed,
        backup_path=backup,
    )


def _plan_uninstall_plugin(prov: Provider) -> UninstallPlan:
    path = _settings_path_for(prov)
    # We own the file at this path (plugins/ephor.js), so its presence == installed.
    installed = list(prov.events) if path.is_file() else []
    return UninstallPlan(
        settings_path=path,
        handler_path=hook_handler_path(),
        events_with_ephor_hook=installed,
        backup_path=None,
    )


def _uninstall_plugin(prov: Provider, *, dry_run: bool) -> UninstallPlan:
    plan = _plan_uninstall_plugin(prov)
    if dry_run or not plan.events_with_ephor_hook:
        return plan
    backup = _make_backup(plan.settings_path)
    with _suppress_oserror():
        plan.settings_path.unlink()
    return UninstallPlan(
        settings_path=plan.settings_path,
        handler_path=plan.handler_path,
        events_with_ephor_hook=plan.events_with_ephor_hook,
        backup_path=backup,
    )


# ---------------------------------------------------------------------------
# public install / uninstall (dispatch on the provider's strategy)
# ---------------------------------------------------------------------------


def plan_install(provider: Provider | str | None = None) -> InstallPlan:
    """Compute (without applying) what `ephor init` would do for `provider`."""
    prov = _resolve_provider(provider)
    if prov.install_kind == OPENCODE_PLUGIN:
        return _plan_install_plugin(prov)
    settings_path = _settings_path_for(prov)
    handler = hook_handler_path()
    settings = _load_settings(settings_path)
    hooks_root = settings.get("hooks") or {}

    # An event is "already installed" only if OUR entry matches the current
    # desired shape. A stale ephor entry (e.g. a pre-fix codex hook still
    # carrying `async: true`, which codex silently skips) counts as needing
    # reinstall — otherwise re-running `ephor init` after an upgrade is a no-op
    # and the fix never lands.
    desired = _build_hook_entry(handler, prov)
    already: list[str] = []
    to_add: list[str] = []
    for event in prov.events:
        entries = hooks_root.get(event) or []
        ephor_entries = [e for e in entries if _is_ephor_entry(e, handler)]
        if ephor_entries and all(e == desired for e in ephor_entries):
            already.append(event)
        else:
            to_add.append(event)  # absent OR stale

    return InstallPlan(
        settings_path=settings_path,
        handler_path=handler,
        events_to_add=to_add,
        events_already_installed=already,
        backup_path=None,  # filled in by install()
    )


def install(provider: Provider | str | None = None, *, dry_run: bool = False) -> InstallPlan:
    """Add ephor's hook entries to `provider`'s settings file. Returns the plan."""
    prov = _resolve_provider(provider)
    if prov.install_kind == OPENCODE_PLUGIN:
        return _install_plugin(prov, dry_run=dry_run)
    plan = plan_install(prov)
    if dry_run or not plan.events_to_add:
        return plan

    settings_path = plan.settings_path
    handler = plan.handler_path

    backup = _make_backup(settings_path)
    settings = _load_settings(settings_path)
    hooks_root = settings.setdefault("hooks", {})
    desired = _build_hook_entry(handler, prov)
    for event in plan.events_to_add:
        entries = hooks_root.setdefault(event, [])
        # Drop any pre-existing ephor entries (possibly stale, e.g. old
        # async:true) before appending the current desired one, so re-running
        # init self-heals in place instead of leaving the stale entry or
        # duplicating. Non-ephor entries are preserved untouched.
        kept = [e for e in entries if not _is_ephor_entry(e, handler)]
        kept.append(desired)
        hooks_root[event] = kept

    _atomic_write(
        settings_path,
        json.dumps(settings, indent=2, sort_keys=False) + "\n",
        mode=0o600,
    )

    return InstallPlan(
        settings_path=plan.settings_path,
        handler_path=plan.handler_path,
        events_to_add=plan.events_to_add,
        events_already_installed=plan.events_already_installed,
        backup_path=backup,
    )


def plan_uninstall(provider: Provider | str | None = None) -> UninstallPlan:
    """Compute (without applying) what `ephor uninstall` would do for `provider`."""
    prov = _resolve_provider(provider)
    if prov.install_kind == OPENCODE_PLUGIN:
        return _plan_uninstall_plugin(prov)
    settings_path = _settings_path_for(prov)
    handler = hook_handler_path()
    settings = _load_settings(settings_path)
    hooks_root = settings.get("hooks") or {}

    affected: list[str] = []
    for event, entries in hooks_root.items():
        if not isinstance(entries, list):
            continue
        if any(_is_ephor_entry(e, handler) for e in entries):
            affected.append(event)

    return UninstallPlan(
        settings_path=settings_path,
        handler_path=handler,
        events_with_ephor_hook=sorted(affected),
        backup_path=None,
    )


def uninstall(provider: Provider | str | None = None, *, dry_run: bool = False) -> UninstallPlan:
    """Remove ephor's hook entries from `provider`'s settings file."""
    prov = _resolve_provider(provider)
    if prov.install_kind == OPENCODE_PLUGIN:
        return _uninstall_plugin(prov, dry_run=dry_run)
    plan = plan_uninstall(prov)
    if dry_run or not plan.events_with_ephor_hook:
        return plan

    settings_path = plan.settings_path
    handler = plan.handler_path

    backup = _make_backup(settings_path)
    settings = _load_settings(settings_path)
    hooks_root = settings.get("hooks", {})

    for event in list(hooks_root.keys()):
        entries = hooks_root.get(event)
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not _is_ephor_entry(e, handler)]
        if kept:
            hooks_root[event] = kept
        else:
            # Empty array left after removal — drop the key entirely so
            # uninstall is a clean inverse of install for previously-empty
            # events (preserves byte-identical round-trip when possible).
            del hooks_root[event]

    if not hooks_root:
        # If we just emptied hooks, drop the key too.
        settings.pop("hooks", None)

    _atomic_write(
        settings_path,
        json.dumps(settings, indent=2, sort_keys=False) + "\n",
        mode=0o600,
    )

    return UninstallPlan(
        settings_path=plan.settings_path,
        handler_path=plan.handler_path,
        events_with_ephor_hook=plan.events_with_ephor_hook,
        backup_path=backup,
    )


def restore_backup(provider: Provider | str | None = None) -> Path | None:
    """Rescue: replace `provider`'s settings file with its most-recent backup.
    Returns the backup path that was restored, or None if no backups exist."""
    settings_path = _settings_path_for(_resolve_provider(provider))
    backup = _latest_backup(settings_path)
    if backup is None:
        return None
    _atomic_write(
        settings_path,
        backup.read_text(),
        mode=0o600,
    )
    return backup


def latest_backup_path(provider: Provider | str | None = None) -> Path | None:
    """Public helper for the CLI's --restore-backup --dry-run path."""
    return _latest_backup(_settings_path_for(_resolve_provider(provider)))


# ---------------------------------------------------------------------------
# Speech-ownership: hand TTS playback over to ephor.
# ---------------------------------------------------------------------------

# What we recognise as the user's existing TTS hook. Substring match against
# the hook entry's command string — captures both `tts-speak-response`
# and any `.kokoro` / `.piper` siblings the user might invoke.
TTS_HOOK_MARKER = "tts-speak-response"


@dataclass(frozen=True)
class SpeechInstallPlan:
    """What `ephor speech install` *would* remove from settings.json."""

    settings_path: Path
    affected_events: list[str]
    affected_commands: list[str]
    backup_path: Path | None

    def summary(self) -> str:
        lines = [
            f"settings.json: {self.settings_path}",
            f"backup:        {self.backup_path or '(dry-run, none written)'}",
            "",
        ]
        if self.affected_events:
            lines.append(
                "will remove tts-speak-response hooks from: " + ", ".join(self.affected_events)
            )
            for cmd in self.affected_commands:
                lines.append(f"  - {cmd}")
        else:
            lines.append("nothing to do — no tts-speak-response hooks present.")
        return "\n".join(lines)


def _command_invokes_tts(cmd: object) -> bool:
    return isinstance(cmd, str) and TTS_HOOK_MARKER in cmd


def plan_speech_install() -> SpeechInstallPlan:
    """Compute (without applying) what `ephor speech install` would do."""
    settings_path = claude_settings_path()
    settings = _load_settings(settings_path)
    hooks_root = settings.get("hooks") or {}

    affected_events: list[str] = []
    affected_cmds: list[str] = []
    for event, entries in hooks_root.items():
        if not isinstance(entries, list):
            continue
        event_matched = False
        for e in entries:
            if not isinstance(e, dict):
                continue
            for h in e.get("hooks") or []:
                if isinstance(h, dict) and _command_invokes_tts(h.get("command")):
                    affected_cmds.append(h["command"])
                    event_matched = True
        if event_matched:
            affected_events.append(event)
    return SpeechInstallPlan(
        settings_path=settings_path,
        affected_events=sorted(set(affected_events)),
        affected_commands=affected_cmds,
        backup_path=None,
    )


def install_speech(*, dry_run: bool = False) -> SpeechInstallPlan:
    """Remove tts-speak-response from every hook in settings.json so ephor's
    SpeechPlayer is the single TTS path. Atomic write + backup, same
    discipline as `ephor init` / `ephor uninstall`."""
    plan = plan_speech_install()
    if dry_run or not plan.affected_events:
        return plan

    settings_path = plan.settings_path
    backup = _make_backup(settings_path)
    settings = _load_settings(settings_path)
    hooks_root = settings.get("hooks", {})

    for event in list(hooks_root.keys()):
        entries = hooks_root.get(event)
        if not isinstance(entries, list):
            continue
        kept: list[dict[str, Any]] = []
        for e in entries:
            if not isinstance(e, dict):
                kept.append(e)
                continue
            # Filter the inner hooks list — keep siblings (e.g. ephor's own
            # event_handler.sh stays put when both hooks share an entry),
            # drop only commands matching TTS_HOOK_MARKER.
            inner = e.get("hooks") or []
            kept_inner = [
                h
                for h in inner
                if not (isinstance(h, dict) and _command_invokes_tts(h.get("command")))
            ]
            if kept_inner:
                new_entry = dict(e)
                new_entry["hooks"] = kept_inner
                kept.append(new_entry)
            # else: entry empty after removal — drop entirely.
        if kept:
            hooks_root[event] = kept
        else:
            del hooks_root[event]

    if not hooks_root:
        settings.pop("hooks", None)

    _atomic_write(
        settings_path,
        json.dumps(settings, indent=2, sort_keys=False) + "\n",
        mode=0o600,
    )

    return SpeechInstallPlan(
        settings_path=plan.settings_path,
        affected_events=plan.affected_events,
        affected_commands=plan.affected_commands,
        backup_path=backup,
    )

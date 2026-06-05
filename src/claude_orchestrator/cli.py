"""CLI entrypoint for `cco`.

Subcommand surface (P2 wires list/status/tmux-widget; init/uninstall/tui
remain stubbed for P3/P6).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from claude_orchestrator import __version__
from claude_orchestrator.constants import STATUS_DISPLAY, AgentStatus
from claude_orchestrator.state.manager import StateManager

# tmux-widget tags for nord-ish palette so it pops in tmux status-right.
TMUX_COLOR = {
    AgentStatus.WORKING: "#A3BE8C",
    AgentStatus.IDLE: "#616E88",
    AgentStatus.WAITING_PERMISSION: "#BF616A",
    AgentStatus.WAITING_ANSWER: "#EBCB8B",
    AgentStatus.ERROR: "#B48EAD",
    AgentStatus.DEAD: "#3B4252",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cco",
        description="claude-orchestrator — Linux-native dashboard for Claude Code sessions.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"cco {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    subparsers.add_parser("list", help="List all known Claude Code sessions")
    subparsers.add_parser("status", help="One-line summary for scripts")
    subparsers.add_parser("tmux-widget", help="Output for tmux status-right")

    kill_p = subparsers.add_parser(
        "kill", help="Kill a session (signals claude_pid, kills tmux window, removes state)"
    )
    kill_p.add_argument(
        "sid",
        help="Session id or unique prefix (8+ chars usually enough)",
    )

    subparsers.add_parser(
        "doctor", help="Check that hooks, paths, and dependencies are configured correctly"
    )

    init_p = subparsers.add_parser("init", help="Install hooks into ~/.claude/settings.json")
    init_p.add_argument("--dry-run", action="store_true", help="Print plan without writing")

    uninstall_p = subparsers.add_parser("uninstall", help="Remove cco hooks from settings.json")
    uninstall_p.add_argument("--dry-run", action="store_true", help="Print plan without writing")
    uninstall_p.add_argument(
        "--restore-backup",
        action="store_true",
        help="Rescue mode: replace settings.json with the most recent backup",
    )

    subparsers.add_parser(
        "refresh-tmux",
        help="Scan running claude processes and update each session's tmux pane mapping",
    )
    subparsers.add_parser("tui", help="Launch the Textual TUI dashboard (P6)")

    speech_p = subparsers.add_parser(
        "speech",
        help="Manage TTS playback ownership (cco vs. ~/.claude/hooks/tts-speak-response)",
    )
    speech_sub = speech_p.add_subparsers(dest="speech_command", metavar="<command>")
    speech_install_p = speech_sub.add_parser(
        "install",
        help=(
            "Hand TTS playback to cco: removes the tts-speak-response Stop hook "
            "from settings.json so cco's queue is the single source of audio."
        ),
    )
    speech_install_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print which hook entries would be removed without writing.",
    )
    speech_sub.add_parser(
        "enable",
        help="Persistently enable TTS playback in cco (overrides default).",
    )
    speech_sub.add_parser(
        "disable",
        help="Persistently disable TTS playback in cco. The bar still shows speech, but no audio.",
    )
    speech_sub.add_parser(
        "status",
        help="Show whether TTS is currently enabled and which layer (env/file/default) decided.",
    )
    speech_sub.add_parser(
        "reset-calibration",
        help=(
            "Forget the learned chars/sec rate. Next message starts fresh; "
            "useful after changing KOKORO_VOICE / KOKORO_SPEED."
        ),
    )
    speech_mode_p = speech_sub.add_parser(
        "mode",
        help=(
            "Switch between speaking the full reply ('full') or a one-sentence "
            "summary ('summary'). Summary mode uses the same Claude Code login "
            "as the dashboard summarizer — no API key required."
        ),
    )
    speech_mode_p.add_argument(
        "mode",
        choices=["full", "summary"],
        help="full = read the whole reply; summary = one-sentence brief.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        # Bare `cco` launches the dashboard — the common case. Explicit
        # subcommands (list/status/speech/…) and `cco --help` still work.
        return _cmd_tui()

    if args.command == "list":
        return _cmd_list()
    if args.command == "status":
        return _cmd_status()
    if args.command == "tmux-widget":
        return _cmd_tmux_widget()
    if args.command == "init":
        return _cmd_init(dry_run=bool(args.dry_run))
    if args.command == "uninstall":
        return _cmd_uninstall(
            dry_run=bool(args.dry_run),
            restore_backup=bool(getattr(args, "restore_backup", False)),
        )
    if args.command == "refresh-tmux":
        return _cmd_refresh_tmux()
    if args.command == "kill":
        return _cmd_kill(args.sid)
    if args.command == "doctor":
        return _cmd_doctor()
    if args.command == "tui":
        return _cmd_tui()
    if args.command == "speech":
        sub = getattr(args, "speech_command", None)
        if sub == "install":
            return _cmd_speech_install(dry_run=bool(args.dry_run))
        if sub == "enable":
            return _cmd_speech_set(enabled=True)
        if sub == "disable":
            return _cmd_speech_set(enabled=False)
        if sub == "status":
            return _cmd_speech_status()
        if sub == "reset-calibration":
            return _cmd_speech_reset_calibration()
        if sub == "mode":
            return _cmd_speech_mode(args.mode)
        # No subcommand → show help.
        parser.parse_args(["speech", "--help"])
        return 0

    print(
        f"cco: subcommand '{args.command}' is not implemented yet "
        f"(see docs/project-brief.md for phasing)",
        file=sys.stderr,
    )
    return 2


# --- list ------------------------------------------------------------------


def _cmd_list() -> int:
    """Print a rich table of all known sessions."""
    from rich.console import Console
    from rich.table import Table

    console = Console(highlight=False)
    agents = StateManager().scan()

    if not agents:
        console.print("[dim]No active sessions.[/dim]")
        return 0

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("STATUS", justify="left", no_wrap=True)
    table.add_column("PROJECT", overflow="ellipsis", max_width=22)
    table.add_column("AGE", justify="right", no_wrap=True)
    table.add_column("LAST EVENT", overflow="ellipsis", max_width=20)
    table.add_column("TOOLS", justify="right", no_wrap=True)
    table.add_column("ERR", justify="right", no_wrap=True)
    table.add_column("CWD", overflow="ellipsis")
    table.add_column("SID", no_wrap=True)

    for a in agents:
        symbol, label, color = STATUS_DISPLAY[a.status]
        status_cell = f"[{color}]{symbol} {label}[/]"
        age = _human_age(a.last_event_time)
        err_cell = f"[red]{a.error_count}[/]" if a.error_count else "0"
        table.add_row(
            status_cell,
            a.project_name or "-",
            age,
            a.last_event,
            str(a.tool_count),
            err_cell,
            a.cwd,
            a.session_id[:8],
        )

    console.print(table)
    return 0


# --- status ---------------------------------------------------------------


def _cmd_status() -> int:
    """One-line summary suitable for shell-script consumption."""
    summary = StateManager().get_summary()
    line = summary.status_line()
    print(f"{line}  | total:{summary.total}")
    return 0


# --- tmux-widget ---------------------------------------------------------


def _cmd_tmux_widget() -> int:
    """Compact, colorised summary for `tmux status-right`.

    Output uses tmux format-string color escapes so it integrates cleanly
    with `set -g status-right '#(cco tmux-widget)'`.
    """
    summary = StateManager().get_summary()
    parts: list[str] = ["#[fg=#88C0D0]cco#[default]"]
    if summary.attention:
        if summary.waiting_permission:
            parts.append(
                _tmux_tag("PERM", summary.waiting_permission, AgentStatus.WAITING_PERMISSION)
            )
        if summary.waiting_answer:
            parts.append(_tmux_tag("WAIT", summary.waiting_answer, AgentStatus.WAITING_ANSWER))
        if summary.error:
            parts.append(_tmux_tag("ERR", summary.error, AgentStatus.ERROR))
    if summary.working:
        parts.append(_tmux_tag("W", summary.working, AgentStatus.WORKING))
    if summary.idle:
        parts.append(_tmux_tag("I", summary.idle, AgentStatus.IDLE))
    if summary.total == 0:
        parts.append("#[fg=#3B4252]·#[default]")
    print(" ".join(parts))
    return 0


def _tmux_tag(label: str, count: int, status: AgentStatus) -> str:
    color = TMUX_COLOR[status]
    return f"#[fg={color}]{label}:{count}#[default]"


# --- init / uninstall -----------------------------------------------------


def _cmd_init(*, dry_run: bool) -> int:
    """Install cco hooks into ~/.claude/settings.json."""
    if os.geteuid() == 0:
        print("cco: refusing to run as root.", file=sys.stderr)
        return 3
    from claude_orchestrator.hooks import installer

    plan = installer.install(dry_run=dry_run)
    print(plan.summary())
    if dry_run:
        print("\n[dry-run] no changes written.")
    elif plan.events_to_add:
        print("\nDone. Restart any active Claude Code sessions to pick up the new hooks.")
    return 0


def _cmd_speech_set(*, enabled: bool) -> int:
    """Persist the TTS-enabled choice to ~/.config/claude-orchestrator/speech.json.

    Note: an env-var override (CCO_TTS_ENABLED) wins over the persisted
    file. We surface that fact when relevant so the user isn't surprised
    that their `cco speech disable` "didn't take."
    """
    from claude_orchestrator import speech_settings

    try:
        path = speech_settings.save(enabled=enabled)
    except OSError as exc:
        print(f"cco: failed to save settings: {exc}", file=sys.stderr)
        return 1
    state = "enabled" if enabled else "disabled"
    print(f"TTS playback in cco: {state}")
    print(f"Saved to: {path}")
    if os.environ.get(speech_settings.ENV_VAR) is not None:
        print(
            f"\nNote: {speech_settings.ENV_VAR} is set in your shell — "
            f"that env override will win until you `unset {speech_settings.ENV_VAR}`."
        )
    return 0


def _cmd_speech_status() -> int:
    """Print the resolved TTS state and which layer set it."""
    from claude_orchestrator import speech, speech_player, speech_settings

    s = speech_settings.load()
    state = "ENABLED" if s.enabled else "DISABLED"
    print(f"TTS playback: {state}  (decided by: {s.source.value})")
    print(f"Settings file: {speech_settings.settings_path()}")
    env_raw = os.environ.get(speech_settings.ENV_VAR)
    if env_raw is not None:
        print(f"Env override: {speech_settings.ENV_VAR}={env_raw!r}")
    cmd = speech_player.default_tts_command()
    if cmd is None:
        print("Kokoro pipeline: NOT FOUND (cco can't actually play audio yet)")
    else:
        print(f"Kokoro pipeline: {cmd[0]}")

    # Speak mode: which slice of the reply gets read aloud. Summary mode
    # reuses the dashboard summarizer (claude -p, no API key).
    print(f"Speak mode: {s.speak_mode}  (decided by: {s.mode_source.value})")
    mode_env_raw = os.environ.get(speech_settings.MODE_ENV_VAR)
    if mode_env_raw is not None:
        print(f"Mode env override: {speech_settings.MODE_ENV_VAR}={mode_env_raw!r}")

    # Calibration line: surface what rate the bar will use AND why.
    if s.calibrated_chars_per_sec:
        print(
            f"Calibrated rate: {s.calibrated_chars_per_sec:.1f} chars/sec "
            f"at speed=1  (learned from observed playback)"
        )
    else:
        print(
            f"Calibrated rate: not yet learned  (using default "
            f"{speech.CHARS_PER_SEC_AT_SPEED_1:.1f} chars/sec at speed=1)"
        )
    rate_env = os.environ.get("CCO_SPEECH_CHARS_PER_SEC")
    if rate_env is not None:
        print(f"Rate override: CCO_SPEECH_CHARS_PER_SEC={rate_env!r} (wins over calibration)")
    return 0


def _cmd_speech_reset_calibration() -> int:
    """Forget the learned chars/sec rate so next playback starts fresh."""
    from claude_orchestrator import speech_settings

    try:
        speech_settings.save(clear_calibration=True)
    except OSError as exc:
        print(f"cco: failed to save settings: {exc}", file=sys.stderr)
        return 1
    print("Calibration cleared. Next playback will recalibrate from scratch.")
    return 0


def _cmd_speech_mode(mode: str) -> int:
    """Persist the speak-mode (full vs. summary) for future Stop events."""
    from claude_orchestrator import speech_settings

    try:
        path = speech_settings.save(speak_mode=mode)
    except (OSError, ValueError) as exc:
        print(f"cco: failed to save settings: {exc}", file=sys.stderr)
        return 1
    if mode == speech_settings.SPEAK_MODE_SUMMARY:
        blurb = "one-sentence summary (uses your Claude Code login — no API key)"
    else:
        blurb = "full assistant reply"
    print(f"TTS speak mode: {mode}  — {blurb}")
    print(f"Saved to: {path}")
    if os.environ.get(speech_settings.MODE_ENV_VAR) is not None:
        print(
            f"\nNote: {speech_settings.MODE_ENV_VAR} is set in your shell — "
            f"that env override will win until you `unset {speech_settings.MODE_ENV_VAR}`."
        )
    return 0


def _cmd_speech_install(*, dry_run: bool) -> int:
    """Hand TTS playback to cco by removing the user's tts-speak-response
    Stop hook from settings.json. cco's SpeechPlayer becomes the sole TTS
    path while the dashboard is running."""
    if os.geteuid() == 0:
        print("cco: refusing to run as root.", file=sys.stderr)
        return 3
    from claude_orchestrator.hooks import installer

    plan = installer.install_speech(dry_run=dry_run)
    print(plan.summary())
    if dry_run:
        print("\n[dry-run] no changes written.")
        return 0
    if plan.affected_events:
        print(
            "\nDone. cco now owns TTS playback. While the cco TUI is open, "
            "the speech queue plays one session at a time (same-session "
            "follow-ups preempt the in-flight reply)."
        )
        print(
            "Heads-up: closing the cco TUI also stops audio. Re-add "
            "tts-speak-response to your Stop hooks (e.g. via the backup "
            "above) if you want playback when cco isn't running."
        )
    return 0


def _cmd_uninstall(*, dry_run: bool, restore_backup: bool) -> int:
    """Remove cco hooks (or restore from latest backup)."""
    if os.geteuid() == 0:
        print("cco: refusing to run as root.", file=sys.stderr)
        return 3
    from claude_orchestrator.hooks import installer

    if restore_backup:
        if dry_run:
            backup = installer.latest_backup_path()
            print(f"would restore: {backup or '(no backup found)'}")
            return 0
        backup = installer.restore_backup()
        if backup is None:
            print("cco: no backup found to restore.", file=sys.stderr)
            return 4
        print(f"restored: {backup}")
        return 0

    plan = installer.uninstall(dry_run=dry_run)
    print(plan.summary())
    if dry_run:
        print("\n[dry-run] no changes written.")
    return 0


def _cmd_refresh_tmux() -> int:
    """Scan tmux + /proc to backfill tmux mappings on existing state files."""
    from claude_orchestrator.config import state_dir
    from claude_orchestrator.tmux.discover import discover_panes, enrich_state_files

    panes = discover_panes()
    if not panes:
        print("cco: no claude processes found inside tmux panes.", file=sys.stderr)
        return 0
    updated = enrich_state_files(state_dir())
    print(f"discovered {len(panes)} pane(s); updated {updated} state file(s).")
    return 0


# --- kill -----------------------------------------------------------------


def _cmd_kill(sid_or_prefix: str) -> int:
    """Kill a session by id (or unique prefix). Mirrors the TUI 'x' action."""
    from claude_orchestrator.config import state_dir
    from claude_orchestrator.tmux.navigator import kill_session

    manager = StateManager()
    matches = [a for a in manager.scan() if a.session_id.startswith(sid_or_prefix)]

    if not matches:
        print(f"cco: no session matches '{sid_or_prefix}'", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(
            f"cco: '{sid_or_prefix}' is ambiguous "
            f"({len(matches)} matches: "
            f"{', '.join(a.session_id[:8] for a in matches)}). "
            "Use a longer prefix.",
            file=sys.stderr,
        )
        return 1

    agent = matches[0]
    label = agent.project_name or agent.session_id[:8]
    outcome = kill_session(agent, state_dir())
    if outcome.ok:
        suffix = f" ({outcome.detail})" if outcome.detail else ""
        print(f"killed {label}{suffix}")
        return 0
    print(f"cco: kill failed: {outcome.detail}", file=sys.stderr)
    return 1


# --- doctor ---------------------------------------------------------------


def _cmd_doctor() -> int:
    """Diagnose hook installation, paths, and dependencies. Returns 0 if all
    checks pass, 1 on warnings, 2 on hard failures."""
    import shutil

    from claude_orchestrator.config import (
        claude_settings_path,
        hook_handler_path,
        pending_dir,
        state_dir,
    )

    checks: list[tuple[str, str, str]] = []  # (level, label, detail)

    def ok(label: str, detail: str = "") -> None:
        checks.append(("ok", label, detail))

    def warn(label: str, detail: str = "") -> None:
        checks.append(("warn", label, detail))

    def fail(label: str, detail: str = "") -> None:
        checks.append(("fail", label, detail))

    # 1. Required CLI tools.
    for tool in ("bash", "jq", "flock"):
        if shutil.which(tool):
            ok(f"{tool} on PATH")
        else:
            fail(f"{tool} not on PATH", "hook handler will fail-open silently")

    # 2. tmux (warn-only — tmux isn't strictly required, just for jump-to-pane).
    if shutil.which("tmux"):
        ok("tmux on PATH")
    else:
        warn("tmux not on PATH", "Enter-to-jump will be disabled")

    # 3. State / pending / lock dirs are writable with 0700.
    sd = state_dir()
    pd = pending_dir()
    for d in (sd, pd):
        if not d.exists():
            warn(f"{d} not created yet", "will be created on first hook fire")
            continue
        try:
            mode = d.stat().st_mode & 0o777
        except OSError as exc:
            fail(f"{d} unreadable", str(exc))
            continue
        if mode == 0o700:
            ok(f"{d} mode 0700")
        else:
            warn(f"{d} mode {oct(mode)}", "expected 0700; rerun `cco init`")

    # 4. Hook handler exists at the path config will hand to settings.json.
    handler = hook_handler_path()
    if handler.is_file():
        ok(f"hook handler at {handler}")
    else:
        fail(
            f"hook handler missing at {handler}",
            "package install is broken; reinstall claude-orchestrator",
        )

    # 5. Hooks installed in claude settings.json.
    settings = claude_settings_path()
    handler_str = str(handler)
    if settings.is_file():
        try:
            import json

            data = json.loads(settings.read_text())
            hooks = data.get("hooks", {})
            if not isinstance(hooks, dict):
                fail("settings.json hooks malformed", "rerun `cco init`")
            else:
                installed_events = [
                    ev
                    for ev, defs in hooks.items()
                    if isinstance(defs, list) and any(handler_str in json.dumps(d) for d in defs)
                ]
                if installed_events:
                    ok(
                        "cco hooks installed",
                        f"{len(installed_events)} event(s): {', '.join(installed_events[:3])}"
                        + ("…" if len(installed_events) > 3 else ""),
                    )
                else:
                    fail(
                        "cco hooks NOT installed in settings.json",
                        "run `cco init`",
                    )
        except (OSError, ValueError) as exc:
            fail(f"can't read {settings}", str(exc))
    else:
        warn(f"{settings} does not exist", "Claude Code may not be configured yet")

    # Render results.
    icons = {"ok": "[ ok ]", "warn": "[warn]", "fail": "[FAIL]"}
    fails = sum(1 for level, _, _ in checks if level == "fail")
    warns = sum(1 for level, _, _ in checks if level == "warn")
    for level, label, detail in checks:
        line = f"{icons[level]} {label}"
        if detail:
            line += f"  — {detail}"
        print(line)
    print()
    print(f"summary: {fails} fail / {warns} warn / {len(checks) - fails - warns} ok")

    if fails:
        return 2
    if warns:
        return 1
    return 0


# --- tui ------------------------------------------------------------------


def _cmd_tui() -> int:
    """Launch the Textual dashboard."""
    try:
        from claude_orchestrator.tui.app import run as run_tui
    except ImportError as exc:
        print(
            "cco: TUI extras not installed. Reinstall with `pipx install -e '.[tui]'` "
            f"or `pip install textual`. ({exc})",
            file=sys.stderr,
        )
        return 5
    return run_tui()


# --- helpers --------------------------------------------------------------


def _human_age(iso_ts: str) -> str:
    """Render an ISO-8601 timestamp as a compact age string ('3m', '2h', '1d')."""
    if not iso_ts:
        return "-"
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    now = datetime.now(UTC)
    delta = max(0, int((now - ts).total_seconds()))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


if __name__ == "__main__":
    raise SystemExit(main())

"""Command-palette provider for the ephor TUI.

Surfaces every keybinding in :class:`EphorApp` as a searchable command so a
user can run any action (jump, kill, summarize, toggle TTS, …) without
memorising the hotkey. The palette opens with Ctrl+P; this provider
populates it.

Design notes:

* The command list is built from the App's ``BINDINGS`` at runtime so
  adding a new keybinding automatically shows up in the palette — no
  duplicate list to keep in sync. A test (test_commands.py) guards this
  invariant.
* A handful of bindings are *navigation primitives* (``j``/``k`` cursor
  movement, ``escape`` clear filter) — surfacing those in a palette is
  noise, so they're skipped. The skip list is small and explicit.
* Commands that flip a state read the current value at search time so
  the label reflects what the action will do next (``Mute TTS`` vs.
  ``Unmute TTS``). Keeps the palette honest.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider

if TYPE_CHECKING:
    from ephor.tui.app import EphorApp

# Textual's Hit / DiscoveryHit accept either a sync or async zero-arg
# callable in their `command` slot. Spell it out so mypy doesn't widen
# our spec's runner to `object`.
_CommandRunner = Callable[[], Awaitable[Any]] | Callable[[], Any]

# Bindings that are pure navigation / modifier keys — useless as palette
# entries. Anything not in this set gets surfaced.
_PALETTE_SKIP_ACTIONS: frozenset[str] = frozenset(
    {
        "cursor_down",
        "cursor_up",
        "clear_filter",
    }
)


class EphorCommands(Provider):
    """Expose every EphorApp action as a command-palette entry."""

    @property
    def ephor_app(self) -> EphorApp:
        # ``self.app`` is typed as ``App`` upstream — narrow it for callers.
        from ephor.tui.app import EphorApp

        app = self.app
        assert isinstance(app, EphorApp)
        return app

    async def discover(self) -> Hits:
        """Yield every command when the palette opens with no query."""
        for spec in self._build_specs():
            yield DiscoveryHit(
                display=spec.display,
                command=spec.runner,
                text=spec.text,
                help=spec.help,
            )

    async def search(self, query: str) -> Hits:
        """Fuzzy-match each command's searchable text against the query."""
        matcher = self.matcher(query)
        for spec in self._build_specs():
            score = matcher.match(spec.text)
            if score > 0:
                yield Hit(
                    score=score,
                    match_display=matcher.highlight(spec.text),
                    command=spec.runner,
                    help=spec.help,
                )

    def _build_specs(self) -> list[_CommandSpec]:
        """One spec per surfacing-worthy Binding, plus state-aware
        labels for TTS toggles so the palette reads naturally."""
        app = self.ephor_app
        specs: list[_CommandSpec] = []
        for binding in app.BINDINGS:
            action = binding.action
            if action in _PALETTE_SKIP_ACTIONS:
                continue

            display, text, help_text = _label_for(app, binding)
            specs.append(
                _CommandSpec(
                    display=display,
                    text=text,
                    help=help_text,
                    runner=_action_runner(app, action),
                )
            )
        return specs


class _CommandSpec:
    """One row in the palette. Trivial container — not a dataclass to
    avoid pulling dataclasses into hot path startup."""

    __slots__ = ("display", "help", "runner", "text")

    def __init__(
        self,
        display: str,
        text: str,
        help: str | None,
        runner: _CommandRunner,
    ) -> None:
        self.display = display
        self.text = text
        self.help = help
        self.runner = runner


def _action_runner(app: EphorApp, action: str) -> _CommandRunner:
    """Return an async zero-arg callable that runs the action on the app.

    ``run_action`` is itself awaitable and handles both sync and async
    action methods — it's the same path the keybinding takes. Returning
    an async callable lets the palette properly await completion (sync
    actions resolve immediately; async ones get scheduled correctly).
    """

    async def runner() -> None:
        await app.run_action(action)

    return runner


def _label_for(app: EphorApp, binding: Binding) -> tuple[str, str, str | None]:
    """Build (display, searchable text, help) for a binding.

    State-dependent labels (mute, speak mode) read the current value so
    the palette tells the user what tapping enter will *do*, not what
    the binding generically *is*.
    """
    action = binding.action
    description = binding.description or action
    key = binding.key

    display, help_text = _state_aware_label(app, action, description)
    key_hint = f"  [{key}]" if key and not _is_modifier_only(key) else ""
    return f"{display}{key_hint}", display, help_text


def _state_aware_label(
    app: EphorApp, action: str, fallback_description: str
) -> tuple[str, str | None]:
    """For toggle actions, surface what enter will do *next*."""
    if action == "toggle_mute":
        if app._speech_player.is_muted:
            return ("TTS: Unmute audio", "Resume reading assistant replies aloud.")
        return ("TTS: Mute audio", "Silence TTS audio (bar still updates).")
    if action == "toggle_speak_mode":
        from ephor.speech_settings import SPEAK_MODE_FULL

        current = app._speech_player.speak_mode
        if current == SPEAK_MODE_FULL:
            return (
                "TTS: Switch to summary mode",
                "Speak a one-sentence summary instead of the full reply. "
                "Uses your Claude Code login (no API key).",
            )
        return (
            "TTS: Switch to full-reply mode",
            "Speak the entire assistant reply on each Stop event.",
        )

    # Plain bindings: capitalize the description and let it stand.
    pretty = fallback_description.strip()
    if pretty:
        pretty = pretty[0].upper() + pretty[1:]
    return (pretty or action, None)


def _is_modifier_only(key: str) -> bool:
    """Hide the key chip for descriptive-only key names like 'ctrl+c'
    where the chip would just be visual noise alongside 'q' which does
    the same thing."""
    return key.startswith("ctrl+") or key == "escape"

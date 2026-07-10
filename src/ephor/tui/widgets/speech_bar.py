"""Bottom-of-screen bar that mirrors the TTS engine.

Tails $EPHOR_SPEECH_LOG and shows what's currently being read aloud, with
karaoke-style highlighting of the active sentence. Pressing the bound key
(`t` by default) jumps the user's tmux client to the speaking session.

The widget is purely presentational — speech-state computation lives in
ephor.speech so it can be unit-tested without a Textual
event loop.
"""

from __future__ import annotations

import textwrap
import time

from textual.widgets import Static

from ephor.speech import (
    SpeechState,
    estimated_duration_ms,
    read_current,
)
from ephor.speech_player import SpeechPlayer
from ephor.state.manager import StateManager

_TICK_SECONDS = 0.2  # 200ms — fast enough that sentence advances feel live.

# 1 header row + 4 message rows. The active sentence wraps to fill;
# upcoming sentences fill any space the active didn't use.
_MESSAGE_LINES = 4

_PRIMARY = "#00ffff"
_INK = "#dbe4e3"  # bright — chars not yet spoken
_INK_MID = "#b9cac9"  # medium — chars already spoken (teleprompter trail)
_INK_DIM = "#839493"  # dim — upcoming sentences, metadata, idle copy

_PROGRESS_WIDTH = 8
_PROGRESS_FILLED = "▰"
_PROGRESS_EMPTY = "▱"

# Default wrap width used when the widget hasn't been mounted yet (tests).
# Real renders pass the live widget width.
_DEFAULT_WIDTH = 100


def _escape(text: str) -> str:
    """Escape rich-markup brackets so a stray `[` in the assistant text
    doesn't get parsed as a tag and corrupt the rest of the bar."""
    return text.replace("[", r"\[").replace("]", r"\]")


def _format_header(
    *,
    icon: str,
    label_text: str,
    position: str,
    queue_suffix: str,
    progress_bar: str,
) -> str:
    """Compose: 🔊 alpha · 2/5 ▰▰▰▱▱▱▱▱ · 2 queued: beta, gamma

    Each segment uses a `·` middle-dot separator with dim styling so the
    speaker name dominates and the metadata reads as supporting detail.
    The progress bar lives next to the position so the eye groups them
    as "where am I in this response."
    """
    parts = [f"[bold {_PRIMARY}]{icon} {_escape(label_text)}[/]"]
    meta_bits: list[str] = []
    if position and progress_bar:
        meta_bits.append(f"{position} {progress_bar}")
    elif position:
        meta_bits.append(position)
    elif progress_bar:
        meta_bits.append(progress_bar)
    if queue_suffix:
        meta_bits.append(queue_suffix)
    if meta_bits:
        parts.append(f"[{_INK_DIM}]·  {'  ·  '.join(meta_bits)}[/]")
    return "  ".join(parts)


def _format_queue_suffix(queued_labels: list[str]) -> str:
    if not queued_labels:
        return ""
    head = ", ".join(queued_labels[:3])
    extra = len(queued_labels) - 3
    suffix = f" +{extra}" if extra > 0 else ""
    return f"{len(queued_labels)} queued: {_escape(head)}{suffix}"


def _format_position(idx: int, total: int) -> str:
    if total <= 0:
        return ""
    return f"{idx + 1}/{total}"


def _format_progress_bar(elapsed_ms: int, total_ms: int) -> str:
    """8-cell horizontal bar showing overall elapsed / total. Returns "" if
    we don't have enough info to compute. Always changes character-by-character
    over time, so the bar visibly advances every render tick — important
    visual signal that the system is alive even when the active sentence
    hasn't changed yet."""
    if total_ms <= 0:
        return ""
    frac = max(0.0, min(1.0, elapsed_ms / total_ms))
    filled = round(frac * _PROGRESS_WIDTH)
    return _PROGRESS_FILLED * filled + _PROGRESS_EMPTY * (_PROGRESS_WIDTH - filled)


def _spoken_chars_in_active_sentence(state: SpeechState, idx: int, *, now_ms: int) -> int:
    """Estimate how many characters of the active sentence have already
    been "spoken" (passed by the audio cursor). Used to render the
    teleprompter dim/bright split — chars at index < this value are
    rendered in `_INK_MID`, chars after in `_INK` (full-bright)."""
    if not state.sentences or not (0 <= idx < len(state.sentences)):
        return 0
    # Reproduce active_sentence_index's accumulator to find when THIS
    # sentence began, then turn elapsed-since-sentence-start into chars.
    from ephor.speech import (
        _chars_per_sec_speed1,
        _inter_chunk_ms,
        _startup_latency_ms,
    )

    rate = _chars_per_sec_speed1() * max(0.5, state.speed)
    gap = _inter_chunk_ms()
    elapsed = (now_ms - state.started_ms) - _startup_latency_ms()
    if elapsed <= 0:
        return 0
    cumulative = 0.0
    for i, s in enumerate(state.sentences):
        if i == idx:
            in_sentence_ms = elapsed - cumulative
            if in_sentence_ms <= 0:
                return 0
            chars = int(in_sentence_ms / 1000 * rate)
            return max(0, min(len(s), chars))
        cumulative += (len(s) / rate * 1000) + gap
    return len(state.sentences[idx])


def _split_wrapped_at(rows: list[str], spoken_chars: int) -> list[str]:
    """Walk the wrapped lines, splitting at `spoken_chars` characters of
    visible text. Returns markup strings with the spoken portion in
    `_INK_MID` and the unspoken portion in `_INK`, both bold.

    `spoken_chars` is approximate — textwrap drops the spaces between
    rows, so consecutive row boundaries are off by 1 char. Visually
    indistinguishable; we don't try to exactly reconstruct.
    """
    out: list[str] = []
    consumed = 0
    for row in rows:
        row_len = len(row)
        # how many chars of this row are "already spoken"
        spoken_here = max(0, min(row_len, spoken_chars - consumed))
        if spoken_here >= row_len:
            out.append(f"[bold {_INK_MID}]{_escape(row)}[/]")
        elif spoken_here > 0:
            spoken_part = row[:spoken_here]
            rest_part = row[spoken_here:]
            out.append(
                f"[bold {_INK_MID}]{_escape(spoken_part)}[/][bold {_INK}]{_escape(rest_part)}[/]"
            )
        else:
            out.append(f"[bold {_INK}]{_escape(row)}[/]")
        # +1 accounts for the space textwrap dropped between rows.
        consumed += row_len + 1
    return out


def _build_message_lines(
    state: SpeechState,
    idx: int,
    *,
    width: int,
    max_lines: int,
    now_ms: int,
) -> list[str]:
    """Render the message body within `max_lines`.

    1. Wrap the active sentence to ``width``.
    2. Apply the teleprompter split: spoken chars in `_INK_MID`, the rest
       in `_INK` (both bold). The split point moves every tick so the
       user sees the highlight wave through the text.
    3. If lines remain after the active sentence, fill with upcoming
       sentences (one per row, dim, truncated with ellipsis if needed).
    4. Pad to exactly ``max_lines`` so the bar's height stays stable.
    """
    sentences = state.sentences
    if not sentences or not (0 <= idx < len(sentences)):
        return [""] * max_lines

    active = sentences[idx]
    inner = max(20, width)
    wrapped = textwrap.wrap(active, width=inner) or [""]

    spoken = _spoken_chars_in_active_sentence(state, idx, now_ms=now_ms)
    lines = _split_wrapped_at(wrapped[:max_lines], spoken)

    if len(wrapped) > max_lines:
        # Indicator that the active sentence got truncated to fit.
        lines[-1] = lines[-1].rstrip() + " [dim]…[/]"

    if len(lines) < max_lines:
        for upcoming in sentences[idx + 1 :]:
            if len(lines) >= max_lines:
                break
            single = upcoming.strip()
            if len(single) > inner:
                single = single[: inner - 1] + "…"
            lines.append(f"[{_INK_DIM}]{_escape(single)}[/]")

    while len(lines) < max_lines:
        lines.append("")
    return lines


def render_bar(
    state: SpeechState | None,
    *,
    label: str = "",
    queued_labels: list[str] | None = None,
    muted: bool = False,
    width: int = _DEFAULT_WIDTH,
) -> str:
    """Pure formatter — produces a 5-line string (header + 4 message rows).

    Layout:
      Line 1: 🔊/🔇  speaker  ·  N/M  ·  K queued: a, b
      Lines 2-5: active sentence (wrapped, bold) + upcoming sentences
                 (single-line, dim) filling any remaining space.
    """
    queued = queued_labels or []
    queue_suffix = _format_queue_suffix(queued)

    if state is None or not state.speaking or not state.session_id:
        # Idle state — header is dim + minimal; message region stays blank.
        if muted:
            header = (
                f"[{_INK_DIM}]\U0001f507 muted[/]  [{_INK_DIM}]·  press [bold]m[/] to unmute[/]"
            )
        else:
            header = (
                f"[{_INK_DIM}]\U0001f507 no speech[/]  "
                f"[{_INK_DIM}]·  press [bold]t[/] to jump when active[/]"
            )
        if queue_suffix:
            header += f"  [{_INK_DIM}]·  {queue_suffix}[/]"
        return "\n".join([header, *[""] * _MESSAGE_LINES])

    sid = state.session_id
    label_text = label or (sid[:8] if sid else "?")
    icon = "\U0001f507" if muted else "\U0001f50a"
    sentences = state.sentences
    now_ms = int(time.time() * 1000)
    idx = state.active_sentence_index(now_ms=now_ms) if sentences else 0
    position = _format_position(idx, len(sentences))

    # Overall-message progress bar — visible movement every 200ms tick,
    # even when the active sentence (idx) hasn't changed yet.
    elapsed_ms = max(0, now_ms - state.started_ms)
    total_ms = estimated_duration_ms(state.text, sentences, speed=state.speed)
    progress_bar = _format_progress_bar(elapsed_ms, total_ms)

    header = _format_header(
        icon=icon,
        label_text=label_text,
        position=position,
        queue_suffix=queue_suffix,
        progress_bar=progress_bar,
    )

    if not sentences:
        # Defensive: a start record arrived with no sentence split.
        body = [f"[{_INK_DIM}](speaking…)[/]"] + [""] * (_MESSAGE_LINES - 1)
        return "\n".join([header, *body])

    body = _build_message_lines(
        state, idx, width=width - 2, max_lines=_MESSAGE_LINES, now_ms=now_ms
    )
    return "\n".join([header, *body])


class SpeechBar(Static):
    """Bottom-of-dashboard widget showing the currently-playing speech +
    the FIFO queue behind it.

    Source-of-truth resolution order:
      1. SpeechPlayer (when ephor owns playback) — has the authoritative
         "what's audible right now" + queue.
      2. speech.read_current() (legacy mode, no player) — best guess
         from the log alone, no queue visibility.
    """

    def __init__(
        self,
        manager: StateManager | None = None,
        player: SpeechPlayer | None = None,
        *,
        id: str = "speech-bar",
    ) -> None:
        super().__init__("", id=id)
        self._manager = manager
        self._player = player
        self._state: SpeechState | None = None
        self._queued_sids: list[str] = []

    @property
    def speaking_session_id(self) -> str | None:
        """The session whose response is currently being read aloud, or None."""
        if self._state is None or not self._state.speaking:
            return None
        return self._state.session_id

    def on_mount(self) -> None:
        # Use the timer interval, not a thread — Textual schedules ticks on
        # the same event loop as message dispatch, keeping repaints atomic.
        self.set_interval(_TICK_SECONDS, self.refresh_now)
        self.refresh_now()

    def refresh_now(self) -> None:
        try:
            self._state, self._queued_sids = self._read_state()
        except Exception:  # noqa: BLE001
            # Reading the speech log must never crash the dashboard. If
            # the file is mid-truncation or has a corrupt tail, just keep
            # the previous render and try again next tick.
            return
        muted = bool(self._player and self._player.is_muted)
        # Use live widget width so wrapping matches the terminal. Falls
        # back to a sane default until the widget has been laid out (e.g.
        # in headless tests rendering before the first paint).
        width = max(40, self.size.width or _DEFAULT_WIDTH)
        self.update(
            render_bar(
                self._state,
                label=self._label_for_state(),
                queued_labels=[self._label_for_sid(sid) for sid in self._queued_sids],
                muted=muted,
                width=width,
            )
        )

    def _read_state(self) -> tuple[SpeechState | None, list[str]]:
        if self._player is None:
            return read_current(), []
        playing = self._player.now_playing
        if playing is None:
            return None, [q.session_id for q in self._player.queue_snapshot]
        # Build a SpeechState from the player's QueueItem so render_bar
        # can stay agnostic of the player module.
        state = SpeechState(
            session_id=playing.session_id,
            text=playing.text,
            sentences=list(playing.sentences),
            started_ms=playing.enqueued_ms or playing.started_ms,
            speed=playing.speed,
            stopped=False,
        )
        return state, [q.session_id for q in self._player.queue_snapshot]

    def _label_for_state(self) -> str:
        return self._label_for_sid(self.speaking_session_id) if self.speaking_session_id else ""

    def _label_for_sid(self, sid: str | None) -> str:
        if sid is None:
            return ""
        if self._manager is None:
            return sid[:8]
        try:
            for agent in self._manager.scan():
                if agent.session_id == sid:
                    return agent.project_name or sid[:8]
        except Exception:  # noqa: BLE001
            return sid[:8]
        return sid[:8]

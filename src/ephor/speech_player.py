"""TTS playback queue + subprocess manager owned by the ephor TUI.

When ephor is the speech owner (see CLI `ephor speech install`), this module
runs the kokoro pipeline directly instead of relying on the user's
~/.claude/hooks/tts-speak-response. The benefit: a real FIFO queue with
same-session dedup — overlapping responses from multiple Claude sessions
play one at a time instead of stomping on each other.

Behavioural rules (FAQ):
  * **Different sessions**: enqueue, FIFO. The bar shows the current
    speaker and the waiting queue.
  * **Same session, queued**: the new entry replaces the old one in the
    queue (we never want to read the stale reply once a fresher one
    exists for the same conversation).
  * **Same session, currently playing**: PREEMPT. Kill the in-flight
    subprocess and start the new entry. The user explicitly chose this
    semantic — old reply is stale, new content supersedes.
  * **`stop` event** (e.g. UPS fired in that session): drop matching sid
    from queue + terminate playback if it's the current item.
  * **Queue cap**: MAX_QUEUE entries. When 30 sessions all Stop in a
    burst, oldest gets dropped — better than letting the queue grow
    unbounded.

Design discipline:
  * Subprocess management is isolated behind a `Spawner` callable so the
    queue logic is unit-testable without spawning kokoro.
  * `tick()` is the single entry point — it polls the SpeechWatcher,
    routes events into the queue, and reaps finished playback. Designed
    to be driven from a Textual interval; cheap on every tick when idle.
  * No threads, no asyncio events. Subprocess lifecycle uses Popen +
    poll() so Textual's event loop owns scheduling.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ephor.speech import SpeechWatcher, estimated_duration_ms

log = logging.getLogger(__name__)

MAX_QUEUE = 5

# Speak modes (mirrors speech_settings — duplicated here so this module
# stays importable without dragging in the settings module on the hot
# path. The values must stay in sync.)
SPEAK_MODE_FULL = "full"
SPEAK_MODE_SUMMARY = "summary"

# Type alias for an injectable summarizer — keeps the player testable
# without shelling out to `claude -p`. Matches summarize_transcript's
# (path, cwd) signature so the default binding is a one-liner.
SummarizeFn = Callable[[Path, str | None], str]

# Default playback command: pipe `text` on stdin, ducks other audio, plays
# via kokoro+paplay. Mirrors the user's existing tts-speak-response wiring
# so behaviour is identical when ephor is the owner.
_DEFAULT_TTS_COMMAND = str(
    Path(os.environ.get("HOME", "")) / ".local/share/kokoro-tts/play-ducked.sh"
)


def default_tts_command() -> list[str] | None:
    """Resolve the playback command. None if unconfigured / missing on disk."""
    raw = os.environ.get("EPHOR_TTS_COMMAND")
    if raw:
        return [raw]
    if Path(_DEFAULT_TTS_COMMAND).is_file():
        return [_DEFAULT_TTS_COMMAND]
    return None


@dataclass
class QueueItem:
    """One queued (or currently-playing) speech request."""

    session_id: str
    text: str
    sentences: list[str] = field(default_factory=list)
    speed: float = 1.3
    started_ms: int = 0  # original Stop hook timestamp
    enqueued_ms: int = 0  # when ephor accepted it (used for queue ordering)


@dataclass
class _PlaybackHandle:
    item: QueueItem
    proc: subprocess.Popen[bytes] | None  # None when playback couldn't start
    # Wall-clock time when `_start` spawned the subprocess. Used to
    # calibrate chars/sec on natural completion. 0.0 for items started
    # under the null spawner — those don't contribute to calibration.
    started_at: float = 0.0


# Spawner signature: takes a QueueItem, returns a Popen (or None if it
# couldn't start). Injected so tests can use a fake.
Spawner = Callable[[QueueItem], "subprocess.Popen[bytes] | None"]


def _real_spawner(cmd: list[str]) -> Spawner:
    """Returns a Spawner that runs `cmd` with the item's text on stdin."""

    def spawn(item: QueueItem) -> subprocess.Popen[bytes] | None:
        try:
            return subprocess.Popen(  # noqa: S603 - command is operator-controlled
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # New session so a SIGTERM hits the whole pipeline (kokoro
                # speak.py + paplay), not just the wrapper script. Without
                # this, killing the wrapper leaves paplay holding the audio
                # device for the next sentence and we get audible overlap
                # on preempt.
                start_new_session=True,
            )
        except (OSError, ValueError):
            return None

    def spawn_and_feed(item: QueueItem) -> subprocess.Popen[bytes] | None:
        proc = spawn(item)
        if proc is None or proc.stdin is None:
            return proc
        try:
            proc.stdin.write(item.text.encode("utf-8"))
            proc.stdin.close()
        except (OSError, BrokenPipeError):
            # Subprocess died before we could finish writing — Popen will
            # surface the failure on poll(), so just return.
            pass
        return proc

    return spawn_and_feed


class SpeechPlayer:
    """FIFO queue with same-session dedup and preempt-on-collision."""

    def __init__(
        self,
        spawner: Spawner | None = None,
        watcher: SpeechWatcher | None = None,
        max_queue: int = MAX_QUEUE,
        muted: bool = False,
        speak_mode: str = SPEAK_MODE_FULL,
        summarizer: SummarizeFn | None = None,
    ) -> None:
        if spawner is None:
            cmd = default_tts_command()
            spawner = _real_spawner(cmd) if cmd else _null_spawner
        self._spawn = spawner
        self._watcher = watcher  # optional — set by tick() integration
        self._max_queue = max_queue
        self._queue: list[QueueItem] = []
        self._current: _PlaybackHandle | None = None
        # When muted, the queue still tracks items (so the bar mirrors
        # what WOULD play and the user can spot active sessions) but the
        # spawner is bypassed — no audio. Toggling mute terminates any
        # in-flight playback.
        self._muted = muted
        # Speak mode governs WHAT we read: full assistant reply vs. a
        # one-sentence summary. Summary mode runs the existing
        # subscription-auth summarizer (no API key required) on a
        # background thread; results land back on the main thread via
        # `_pending` and get enqueued during the next tick().
        self._speak_mode = (
            speak_mode if speak_mode in (SPEAK_MODE_FULL, SPEAK_MODE_SUMMARY) else SPEAK_MODE_FULL
        )
        self._summarizer = summarizer
        self._pending: queue.Queue[QueueItem] = queue.Queue()

    # ---- introspection (used by SpeechBar / tests) -----------------------

    @property
    def now_playing(self) -> QueueItem | None:
        return self._current.item if self._current else None

    @property
    def queue_snapshot(self) -> list[QueueItem]:
        return list(self._queue)

    @property
    def is_muted(self) -> bool:
        return self._muted

    @property
    def speak_mode(self) -> str:
        return self._speak_mode

    def set_speak_mode(self, mode: str) -> None:
        """Switch between "full" and "summary" at runtime.

        Mode change affects only NEW start events — items already
        playing or queued keep whatever text they were enqueued with.
        Unknown values are ignored (keeps a typo from breaking audio).
        """
        if mode in (SPEAK_MODE_FULL, SPEAK_MODE_SUMMARY):
            self._speak_mode = mode

    def set_muted(self, muted: bool) -> None:
        """Flip the audio gate.

        When transitioning unmuted→muted with a live subprocess, kill
        the audio process group but **keep the item as "current"** with
        proc=None — the bar continues to show what would be playing,
        and the natural reap-on-estimated-duration logic will advance
        the queue at the right time.
        """
        was_muted = self._muted
        self._muted = bool(muted)
        if was_muted == self._muted:
            return
        if self._muted and self._current is not None and self._current.proc is not None:
            self._silence_current()

    # ---- public API ------------------------------------------------------

    def enqueue(self, item: QueueItem) -> None:
        """Apply the queue rules described in this module's docstring.

        Behaviour summary:
          - Same sid currently playing → preempt (kill + start new).
          - Same sid in queue → replace that entry (no new audio yet).
          - Otherwise → append; if nothing is playing, start now.
        """
        item.enqueued_ms = item.enqueued_ms or _now_ms()

        if self._current and self._current.item.session_id == item.session_id:
            self._terminate_current()
            self._start(item)
            return

        # Same-session dedup in the waiting queue.
        self._queue = [q for q in self._queue if q.session_id != item.session_id]

        if self._current is None:
            self._start(item)
            return

        self._queue.append(item)
        # Cap from the front so the most-recent N stay (oldest drop first).
        if len(self._queue) > self._max_queue:
            self._queue = self._queue[-self._max_queue :]

    def stop(self, session_id: str) -> None:
        """A `stop` event arrived (UPS fired). Drop from queue + kill if
        currently playing."""
        self._queue = [q for q in self._queue if q.session_id != session_id]
        if self._current and self._current.item.session_id == session_id:
            self._terminate_current()
            self._advance()

    def stop_all(self) -> None:
        """Tear everything down (called on app shutdown)."""
        self._queue.clear()
        self._terminate_current()

    def tick(self) -> None:
        """Poll the watcher and reap finished playback. Driven by Textual's
        interval timer (≈200ms cadence is plenty)."""
        if self._watcher is not None:
            for ev in self._watcher.poll():
                self._route_event(ev)
        # Drain any summary-mode work that completed since the last tick.
        # Each background worker pushes one finished QueueItem here; the
        # tick is where it joins the regular FIFO so all the
        # preempt/dedup rules apply uniformly.
        while True:
            try:
                pending = self._pending.get_nowait()
            except queue.Empty:
                break
            self.enqueue(pending)
        self._reap_if_finished()

    # ---- internals -------------------------------------------------------

    def _route_event(self, ev: dict[str, Any]) -> None:
        sid = ev.get("session_id")
        if not isinstance(sid, str):
            return
        kind = ev.get("event")
        if kind == "start":
            text = ev.get("text") or ""
            sentences_raw = ev.get("sentences") or []
            sentences = [s for s in sentences_raw if isinstance(s, str)]
            try:
                speed = float(ev.get("speed") or 1.3)
            except (TypeError, ValueError):
                speed = 1.3
            item = QueueItem(
                session_id=sid,
                text=text,
                sentences=sentences,
                speed=speed,
            )
            transcript_raw = ev.get("transcript_path")
            cwd_raw = ev.get("cwd")
            transcript = transcript_raw if isinstance(transcript_raw, str) else ""
            # Summarize in summary mode whenever there's material to condense —
            # a Claude-format transcript (richer) OR the reply text captured at
            # turn-end for any other provider (gemini/codex/opencode).
            if self._speak_mode == SPEAK_MODE_SUMMARY and (transcript or item.text):
                cwd = cwd_raw if isinstance(cwd_raw, str) and cwd_raw else None
                self._spawn_summary_worker(item, transcript, cwd)
            else:
                self.enqueue(item)
        elif kind == "stop":
            self.stop(sid)

    def _spawn_summary_worker(self, item: QueueItem, transcript_path: str, cwd: str | None) -> None:
        """Run the summarizer in a daemon thread and post the resulting
        QueueItem back via ``self._pending``.

        Summarizing can take 2-3 seconds (an LLM call — ``claude -p`` or a
        configured HTTP endpoint). Doing it on the main tick would freeze the
        TUI; doing it sync would break the FIFO ordering of fast-following
        responses.

        Two input sources: a Claude-format transcript (``transcript_path``,
        richer recent-turn context) when available, else the reply text already
        captured at turn-end (``item.text``) for other providers. On any failure
        (timeout, missing binary, empty result) we fall back to a truncated
        version of the original reply so the user still gets *some* audible
        signal that a response arrived — the whole point of summary mode.
        """

        def worker() -> None:
            summary = ""
            try:
                # Lazy import: pulling summarizer eagerly would drag
                # subprocess+json into every ephor process at startup.
                from ephor.summarizer import summarize_text, summarize_transcript

                if transcript_path:
                    fn = self._summarizer or summarize_transcript
                    summary = fn(Path(transcript_path), cwd)
                if not summary and item.text:
                    summary = summarize_text(item.text, cwd)
            except Exception:  # noqa: BLE001 - background work must not crash the TUI
                log.debug("summary worker failed", exc_info=True)
                summary = ""
            if not summary:
                # Fallback: a short preamble so the user is still
                # notified audibly that something completed. 200 chars
                # is roughly 15 seconds at speed=1.3.
                summary = (item.text[:200] + "…") if len(item.text) > 200 else item.text
            if not summary:
                return
            self._pending.put(
                QueueItem(
                    session_id=item.session_id,
                    text=summary,
                    sentences=[summary],
                    speed=item.speed,
                )
            )

        threading.Thread(target=worker, daemon=True).start()

    def _start(self, item: QueueItem) -> None:
        # When muted, deliberately route through the null spawner so the
        # bar still ticks but no audio plays. Queue advances on the
        # estimated-duration timer (see _reap_if_finished).
        proc = _null_spawner(item) if self._muted else self._spawn(item)
        # started_at = 0 for null-spawner items so calibration ignores
        # them (no real audio played, observed duration is meaningless).
        started_at = time.time() if proc is not None else 0.0
        self._current = _PlaybackHandle(item=item, proc=proc, started_at=started_at)

    def _silence_current(self) -> None:
        """Kill the in-flight subprocess but keep ``_current`` populated
        with ``proc=None``. Used when muting mid-playback: the bar still
        shows the speaker, the natural reap-on-estimated-duration timer
        decides when to advance to whatever's queued behind it."""
        if self._current is None or self._current.proc is None:
            return
        proc = self._current.proc
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                with _suppress_lookup():
                    proc.terminate()
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                with _suppress_lookup():
                    os.killpg(proc.pid, signal.SIGKILL)
        self._current = _PlaybackHandle(item=self._current.item, proc=None)

    def _terminate_current(self) -> None:
        if self._current is None:
            return
        proc = self._current.proc
        if proc is not None and proc.poll() is None:
            try:
                # Kill the whole process group — kokoro spawns paplay and
                # without start_new_session above we'd leave the audio
                # subprocess running.
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                with _suppress_lookup():
                    proc.terminate()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                with _suppress_lookup():
                    os.killpg(proc.pid, signal.SIGKILL)
        self._current = None

    def _reap_if_finished(self) -> None:
        if self._current is None:
            return
        proc = self._current.proc
        item = self._current.item
        if proc is None:
            # Spawner returned None (kokoro missing or in headless tests).
            # Keep the item "playing" for its estimated duration so the
            # bar still mirrors what WOULD be audible — instant-reap would
            # make new starts disappear before the user can read them.
            est_end = (item.enqueued_ms or _now_ms()) + estimated_duration_ms(
                item.text, item.sentences, speed=item.speed
            )
            if _now_ms() >= est_end:
                self._current = None
                self._advance()
            return
        if proc.poll() is not None:
            # Natural completion: clean exit, not preempted/muted/stopped.
            # Feed observed duration into the rate calibrator so the next
            # playback's progress bar matches actual kokoro speed.
            if proc.returncode == 0 and self._current.started_at > 0:
                duration = time.time() - self._current.started_at
                _calibrate_rate(item, duration)
            self._current = None
            self._advance()

    def _advance(self) -> None:
        """Pop the next queued item and start it."""
        if not self._queue:
            return
        nxt = self._queue.pop(0)
        self._start(nxt)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _null_spawner(item: QueueItem) -> subprocess.Popen[bytes] | None:
    """Used when no kokoro command is available — playback silently
    completes, preserving queue semantics for testing/headless CI."""
    return None


class _suppress_lookup:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, (ProcessLookupError, PermissionError, OSError))


def kokoro_available() -> bool:
    """Public helper for the CLI to surface a friendly error when the user
    asks ephor to own playback but the kokoro pipeline isn't installed."""
    cmd = default_tts_command()
    if cmd is None:
        return False
    return shutil.which(cmd[0]) is not None or Path(cmd[0]).is_file()


# Calibration: weight on each new sample. 0.3 gives a few-message decay
# (older samples fade out over ~5-10 messages). Higher = faster reaction
# to changes (e.g. user changed KOKORO_SPEED), lower = more stability.
_CALIBRATION_WEIGHT = 0.3
# Lower bound on text length for a sample to be admitted. Tiny messages
# are dominated by startup latency and produce noisy estimates.
_CALIBRATION_MIN_CHARS = 100
# Plausibility window for an observed rate (chars/sec at speed=1).
# Anything outside this is almost certainly a measurement artifact
# (network hiccup, kokoro crash) and gets ignored.
_CALIBRATION_RATE_MIN = 5.0
_CALIBRATION_RATE_MAX = 50.0


def _calibrate_rate(item: QueueItem, duration_seconds: float) -> None:
    """Update the persisted chars/sec rolling average from one observed
    playback. Caller must guarantee:

      * Subprocess exited cleanly (returncode == 0)
      * Playback was NOT preempted/muted/stopped (otherwise the duration
        is truncated and would skew the average low)
      * started_at was a real wall-clock time (not the null-spawner case)

    Failure modes (corrupt settings file, write errors) are swallowed —
    calibration is a nice-to-have, never crashes the dashboard.
    """
    chars = len(item.text)
    if chars < _CALIBRATION_MIN_CHARS or duration_seconds < 2.0:
        return

    # Subtract the same startup latency the bar's estimator uses, so the
    # calibrated rate is comparable to the un-calibrated default.
    from ephor.speech import _startup_latency_ms

    audio_seconds = max(1.0, duration_seconds - _startup_latency_ms() / 1000.0)
    rate_at_this_speed = chars / audio_seconds
    rate_at_speed_1 = rate_at_this_speed / max(0.5, item.speed)

    if rate_at_speed_1 < _CALIBRATION_RATE_MIN or rate_at_speed_1 > _CALIBRATION_RATE_MAX:
        return

    try:
        from ephor.speech_settings import load, save

        current = load().calibrated_chars_per_sec
        if current is None:
            new_rate = rate_at_speed_1
        else:
            # Exponentially-weighted moving average: heavy on history
            # for stability, but new samples nudge it toward the truth.
            new_rate = current * (1 - _CALIBRATION_WEIGHT) + rate_at_speed_1 * _CALIBRATION_WEIGHT
        save(calibrated_chars_per_sec=new_rate)
    except (ImportError, OSError):
        # Calibration is opportunistic — don't disturb playback if disk
        # is unavailable.
        pass

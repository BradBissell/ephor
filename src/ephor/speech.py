"""Speech-event log: append-only NDJSON of TTS start/stop events.

The Stop hook writes a `start` record (assistant text + sentence-split for
karaoke). UserPromptSubmit writes a `stop` record (the user typed something,
which cancels in-flight TTS). The TUI's SpeechBar widget tails this log to
mirror the TTS engine and offer a one-key jump to the speaking session.

Schema (one JSON object per line):

    {"event":"start","ts":"<iso>","session_id":"<sid>",
     "text":"<full assistant text>","sentences":[...],"speed":1.3}
    {"event":"stop","ts":"<iso>","session_id":"<sid>"}

Path resolution:
  $EPHOR_SPEECH_LOG  (override)
  → otherwise: $XDG_STATE_HOME/ephor/speech.jsonl

Bounded size: writers truncate to the last MAX_EVENTS lines once the file
grows past MAX_BYTES. Readers tolerate partial / malformed lines.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ephor.config import state_dir

MAX_BYTES = 256 * 1024
MAX_EVENTS = 200

# Karaoke timing model (all overridable via env for easy tuning without
# rebuilding ephor — kokoro's actual rate varies with voice + system load):
#
#   t=0                                                       last sentence end
#   ├─[ STARTUP_MS ]─┤───[ chars/rate ]───┤ GAP ┤───[ ... ]───┤
#   ↑                ↑                          ↑
#   hook fired       audio first heard          inter-chunk synth pauses
#
# Defaults tuned for kokoro-onnx + paplay on a warm system. If the bar
# consistently runs ahead of audio, raise EPHOR_SPEECH_STARTUP_MS or lower
# EPHOR_SPEECH_CHARS_PER_SEC. If it runs behind, do the inverse.

# Empirical: kokoro at speed=1 reads ~14 chars/sec audible; at speed=1.3, ~18.
CHARS_PER_SEC_AT_SPEED_1 = 14.0
# First-chunk synth + paplay startup before any sound is heard.
STARTUP_LATENCY_MS = 1500
# Per-sentence synthesis pause once the previous chunk finishes playing.
INTER_CHUNK_GAP_MS = 150


def speech_log_path() -> Path:
    raw = os.environ.get("EPHOR_SPEECH_LOG")
    if raw:
        return Path(raw).expanduser()
    return state_dir().parent / "speech.jsonl"


def default_speed() -> float:
    """Resolved at call time so tests can monkeypatch KOKORO_SPEED."""
    try:
        return float(os.environ.get("KOKORO_SPEED", "1.3"))
    except (TypeError, ValueError):
        return 1.3


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _startup_latency_ms() -> int:
    return max(0, _env_int("EPHOR_SPEECH_STARTUP_MS", STARTUP_LATENCY_MS))


def _inter_chunk_ms() -> int:
    return max(0, _env_int("EPHOR_SPEECH_INTER_CHUNK_MS", INTER_CHUNK_GAP_MS))


def _chars_per_sec_speed1() -> float:
    """Resolve effective chars/sec at speed=1, in priority order:

    1. EPHOR_SPEECH_CHARS_PER_SEC env var (manual override).
    2. Calibrated rate persisted in speech_settings (learned from
       observed playbacks — the bar matches the user's actual kokoro
       reading speed once a few messages have completed).
    3. Hardcoded default (CHARS_PER_SEC_AT_SPEED_1).
    """
    env = os.environ.get("EPHOR_SPEECH_CHARS_PER_SEC")
    if env is not None:
        try:
            v = float(env)
            if v > 0:
                return max(1.0, v)
        except (TypeError, ValueError):
            pass
    # Lazy import — speech_settings imports speech_player which imports
    # this module's helpers. Top-level import would form a cycle.
    try:
        from ephor.speech_settings import load as _load_settings

        s = _load_settings()
        if s.calibrated_chars_per_sec and s.calibrated_chars_per_sec > 0:
            return max(1.0, s.calibrated_chars_per_sec)
    except (ImportError, OSError):
        pass
    return CHARS_PER_SEC_AT_SPEED_1


def split_sentences(text: str) -> list[str]:
    """Split text into sentence-ish chunks, mirroring kokoro speak.py.

    Sentences are merged when concatenation stays under 240 chars — kokoro
    re-merges in chunks for synthesis efficiency, and our karaoke boundaries
    must match the boundaries kokoro will actually pause on.
    """
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    out: list[str] = []
    buf = ""
    for p in parts:
        if not p:
            continue
        if buf and len(buf) + len(p) + 1 < 240:
            buf = (buf + " " + p).strip()
        else:
            if buf:
                out.append(buf)
            buf = p
    if buf:
        out.append(buf)
    return out


def estimated_duration_ms(
    text: str,
    sentences: list[str] | None = None,
    *,
    speed: float | None = None,
) -> int:
    """Total wall-clock duration: startup + audio + inter-chunk gaps.

    `sentences` is optional for backward compatibility — without it we lose
    the inter-chunk gap term, which is fine for the auto-stop guard but
    makes the karaoke run ~150ms-per-sentence ahead. Callers that have
    sentences should pass them.
    """
    sp = speed if speed is not None else default_speed()
    rate = _chars_per_sec_speed1() * max(0.5, sp)
    chars = max(1, len(text))
    audio_ms = chars / rate * 1000
    gap_count = max(0, len(sentences or []) - 1)
    gaps_ms = gap_count * _inter_chunk_ms()
    return int(_startup_latency_ms() + audio_ms + gaps_ms)


@dataclass
class SpeechState:
    """What the TUI needs to render the speech bar in one tick."""

    session_id: str | None
    text: str
    sentences: list[str]
    started_ms: int
    speed: float
    stopped: bool

    @property
    def speaking(self) -> bool:
        return bool(self.session_id) and not self.stopped

    def estimated_end_ms(self) -> int:
        return self.started_ms + estimated_duration_ms(self.text, self.sentences, speed=self.speed)

    def active_sentence_index(self, *, now_ms: int | None = None) -> int:
        """Which sentence is most likely playing right now.

        Models kokoro's actual cadence: a startup latency before the first
        sound (model load + first-chunk synth + paplay startup), then each
        chunk takes char-count / rate seconds plus a synthesis gap before
        the next chunk's audio begins. Caller can tune via env vars
        (EPHOR_SPEECH_STARTUP_MS, EPHOR_SPEECH_INTER_CHUNK_MS,
        EPHOR_SPEECH_CHARS_PER_SEC).
        """
        if not self.sentences:
            return 0
        n = now_ms if now_ms is not None else int(time.time() * 1000)
        elapsed = (n - self.started_ms) - _startup_latency_ms()
        if elapsed <= 0:
            return 0
        rate = _chars_per_sec_speed1() * max(0.5, self.speed)
        gap = _inter_chunk_ms()
        cumulative = 0.0
        for i, s in enumerate(self.sentences):
            cumulative += (len(s) / rate * 1000) + gap
            if elapsed < cumulative:
                return i
        return len(self.sentences) - 1


_NULL_STATE = SpeechState(None, "", [], 0, 1.0, True)


def append_start(
    session_id: str,
    text: str,
    *,
    speed: float | None = None,
    path: Path | None = None,
) -> None:
    sp = speed if speed is not None else default_speed()
    record = {
        "event": "start",
        "ts": _iso_now(),
        "session_id": session_id,
        "text": text,
        "sentences": split_sentences(text),
        "speed": sp,
    }
    _atomic_append(path or speech_log_path(), record)


def append_stop(session_id: str, *, path: Path | None = None) -> None:
    record = {"event": "stop", "ts": _iso_now(), "session_id": session_id}
    _atomic_append(path or speech_log_path(), record)


def read_current(path: Path | None = None, *, now_ms: int | None = None) -> SpeechState:
    """Compute current speech state from the log.

    Walking forward, we model the TTS engine: a `start` from any session
    pre-empts whatever was playing (the existing tts-speak-response calls
    `tts-stop` before each new response), and a `stop` clears the active
    session's playback. The "now speaking" session is the most-recent
    start that has not been pre-empted, stopped, or expired.
    """
    p = path or speech_log_path()
    if not p.is_file():
        return _NULL_STATE
    try:
        raw = p.read_text(errors="ignore")
    except OSError:
        return _NULL_STATE

    active: dict[str, Any] | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        ev = rec.get("event")
        sid = rec.get("session_id")
        if not isinstance(sid, str):
            continue
        if ev == "start":
            # Globally pre-empts any prior start (mirrors tts-stop semantics).
            active = rec
        elif ev == "stop" and active is not None and active.get("session_id") == sid:
            active = None
    if active is None:
        return _NULL_STATE

    started_ms = _parse_iso_ms(active.get("ts", ""))
    text = active.get("text") or ""
    sentences_raw = active.get("sentences") or []
    sentences = [s for s in sentences_raw if isinstance(s, str)]
    try:
        speed = float(active.get("speed") or default_speed())
    except (TypeError, ValueError):
        speed = default_speed()
    state = SpeechState(
        session_id=active.get("session_id"),
        text=text,
        sentences=sentences,
        started_ms=started_ms,
        speed=speed,
        stopped=False,
    )

    # Auto-stop once the estimated end has passed — guards against a
    # missing stop record from a crashed UPS hook.
    n = now_ms if now_ms is not None else int(time.time() * 1000)
    if n > state.estimated_end_ms():
        state.stopped = True
    return state


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso_ms(ts: str) -> int:
    if not ts:
        return 0
    try:
        ts2 = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts2)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


def _atomic_append(path: Path, record: dict[str, Any]) -> None:
    """flock + append. Truncates head if file grows past MAX_BYTES.

    Multiple Stop hooks firing in parallel must not interleave half-lines —
    fcntl.LOCK_EX serialises writes across processes. We close-on-finish
    rather than holding the fd, so the flock release is fire-and-forget.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    try:
        if path.stat().st_size > MAX_BYTES:
            _truncate_head(path)
    except OSError:
        pass


class SpeechWatcher:
    """Incremental reader of the speech log.

    Tracks the byte offset we've consumed up to so each `poll()` returns
    only events appended since the previous call. Survives MAX_BYTES
    head-truncation by detecting size shrinkage and rewinding to 0
    (we'll re-emit recent records in that case, and the player's dedup
    keeps the duplicates from triggering double playback).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or speech_log_path()
        self._pos = 0
        # Initialise the position to the file's CURRENT end-of-file so
        # opening ephor doesn't replay every start record from this morning.
        # If the user genuinely wants the latest message played back on
        # ephor-startup, they can re-trigger it manually.
        try:
            self._pos = self._path.stat().st_size
        except OSError:
            self._pos = 0

    @property
    def path(self) -> Path:
        return self._path

    def poll(self) -> list[dict[str, Any]]:
        try:
            size = self._path.stat().st_size
        except OSError:
            return []
        if size < self._pos:
            # File was head-truncated. Restart from the top.
            self._pos = 0
        if size == self._pos:
            return []
        out: list[dict[str, Any]] = []
        try:
            with self._path.open() as f:
                f.seek(self._pos)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except (ValueError, TypeError):
                        continue
                self._pos = f.tell()
        except OSError:
            return []
        return out


def _truncate_head(path: Path) -> None:
    """Keep only the last MAX_EVENTS lines."""
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with os.fdopen(os.dup(fd), "r", closefd=True) as f:
                lines = f.readlines()
            keep = lines[-MAX_EVENTS:]
            content = "".join(keep).encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, content)
            os.ftruncate(fd, len(content))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

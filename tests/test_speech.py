"""Tests for the speech-event log module.

Covers the pure-Python read/write side. Hook integration (event_handler.sh
emitting the actual records) is exercised separately in test_hook_handler.py.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ephor import speech


@pytest.fixture
def speech_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "speech.jsonl"
    monkeypatch.setenv("EPHOR_SPEECH_LOG", str(log))
    return log


# ---- pure helpers ---------------------------------------------------------


def test_split_sentences_short_text_stays_one_chunk() -> None:
    out = speech.split_sentences("Hello world. How are you?")
    # Both sentences fit under 240 chars, so kokoro merges them into one chunk.
    assert out == ["Hello world. How are you?"]


def test_split_sentences_emits_separate_chunks_when_total_exceeds_threshold() -> None:
    long = "A" * 200
    out = speech.split_sentences(f"{long}. {long}!")
    assert len(out) == 2


def test_split_sentences_handles_empty() -> None:
    assert speech.split_sentences("") == []
    assert speech.split_sentences("   ") == []


def test_estimated_duration_grows_with_length() -> None:
    short = speech.estimated_duration_ms("Hi.")
    long = speech.estimated_duration_ms("Hi. " * 100)
    assert long > short > 0


def test_estimated_duration_shrinks_at_higher_speed() -> None:
    base = speech.estimated_duration_ms("Hello world. How are you?", speed=1.0)
    fast = speech.estimated_duration_ms("Hello world. How are you?", speed=2.0)
    assert fast < base


# ---- round-trip through the log ------------------------------------------


def test_append_start_then_read_current_returns_speaking_state(speech_log: Path) -> None:
    speech.append_start("sess-A", "First. Second!")
    state = speech.read_current()
    assert state.speaking is True
    assert state.session_id == "sess-A"
    assert state.text.startswith("First")
    assert len(state.sentences) >= 1


def test_append_stop_clears_active_state(speech_log: Path) -> None:
    speech.append_start("sess-A", "Hello there.")
    speech.append_stop("sess-A")
    state = speech.read_current()
    assert state.speaking is False
    assert state.session_id is None


def test_newer_start_preempts_older_start(speech_log: Path) -> None:
    """tts-stop fires before each new TTS run, so any new start globally
    pre-empts the previous one — the bar must follow."""
    speech.append_start("sess-A", "Older message.")
    speech.append_start("sess-B", "Newer message.")
    state = speech.read_current()
    assert state.session_id == "sess-B"
    assert state.speaking is True


def test_state_auto_stops_after_estimated_duration(speech_log: Path) -> None:
    """A missing UPS-stop record (e.g. user exits ephor mid-playback) must not
    keep the bar lit forever. Once estimated_end has passed, we mark the
    state stopped even though no explicit stop was logged."""
    speech.append_start("sess-A", "Tiny.")
    state_now = speech.read_current(now_ms=int(time.time() * 1000))
    assert state_now.speaking is True
    far_future = state_now.started_ms + 60_000_000  # 60,000 seconds later
    state_later = speech.read_current(now_ms=far_future)
    assert state_later.speaking is False


def test_active_sentence_stays_at_zero_during_startup_grace(
    speech_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Until kokoro produces sound, the karaoke must not advance — otherwise
    the bar runs ahead of the audio on every long response."""
    monkeypatch.setenv("EPHOR_SPEECH_STARTUP_MS", "2000")
    s1 = ("alpha " * 50).strip() + "."
    s2 = ("beta " * 50).strip() + "!"
    speech.append_start("sess-A", f"{s1} {s2}")
    state = speech.read_current()
    # Mid-startup: idx must still be 0.
    assert state.active_sentence_index(now_ms=state.started_ms + 500) == 0
    assert state.active_sentence_index(now_ms=state.started_ms + 1900) == 0


def test_inter_chunk_gap_delays_sentence_advance(
    speech_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A larger inter-chunk gap pushes later sentences further out in time."""
    s1 = ("alpha " * 50).strip() + "."
    s2 = ("beta " * 50).strip() + "!"
    s3 = ("gamma " * 50).strip() + "?"
    monkeypatch.setenv("EPHOR_SPEECH_STARTUP_MS", "0")
    monkeypatch.setenv("EPHOR_SPEECH_INTER_CHUNK_MS", "0")
    speech.append_start("sess-A", f"{s1} {s2} {s3}")
    state = speech.read_current()
    end_no_gap = state.estimated_end_ms()

    monkeypatch.setenv("EPHOR_SPEECH_INTER_CHUNK_MS", "500")
    state2 = speech.read_current()
    end_with_gap = state2.estimated_end_ms()
    assert end_with_gap > end_no_gap


def test_active_sentence_advances_with_time(speech_log: Path) -> None:
    # Each sentence must be long enough that the splitter doesn't merge them
    # into a single chunk (kokoro merges below 240 chars).
    s1 = ("alpha " * 50).strip() + "."
    s2 = ("beta " * 50).strip() + "!"
    s3 = ("gamma " * 50).strip() + "?"
    speech.append_start("sess-A", f"{s1} {s2} {s3}")
    state = speech.read_current()
    assert len(state.sentences) >= 2

    at_start = state.active_sentence_index(now_ms=state.started_ms + 10)
    near_end = state.active_sentence_index(now_ms=state.estimated_end_ms() - 10)
    assert at_start == 0
    assert near_end == len(state.sentences) - 1


def test_read_current_when_log_missing_returns_null_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EPHOR_SPEECH_LOG", str(tmp_path / "does-not-exist.jsonl"))
    state = speech.read_current()
    assert state.speaking is False
    assert state.session_id is None


def test_read_current_tolerates_corrupt_lines(speech_log: Path) -> None:
    speech_log.write_text(
        "{not json at all\n"
        '{"event":"start","ts":"2026-04-29T10:00:00.000000Z","session_id":"sess-A",'
        '"text":"Hello.","sentences":["Hello."],"speed":1.0}\n'
        "another garbage line\n"
    )
    state = speech.read_current()
    assert state.session_id == "sess-A"


def test_atomic_append_writes_one_line_per_record(speech_log: Path) -> None:
    speech.append_start("a", "One.")
    speech.append_start("b", "Two.")
    speech.append_stop("b")
    lines = [json.loads(line) for line in speech_log.read_text().strip().splitlines()]
    assert [r["event"] for r in lines] == ["start", "start", "stop"]
    assert [r["session_id"] for r in lines] == ["a", "b", "b"]


def test_truncation_keeps_only_recent_events(
    speech_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the log grows past MAX_BYTES, head-truncation must keep the last
    MAX_EVENTS records — not lose them entirely."""
    monkeypatch.setattr(speech, "MAX_BYTES", 1024)
    monkeypatch.setattr(speech, "MAX_EVENTS", 5)
    big_text = "Sentence. " * 50
    for i in range(40):
        speech.append_start(f"sid-{i}", big_text)
    final_lines = speech_log.read_text().strip().splitlines()
    assert len(final_lines) <= 5
    last = json.loads(final_lines[-1])
    assert last["session_id"] == "sid-39"


def test_default_speed_falls_back_when_env_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KOKORO_SPEED", "not-a-number")
    assert speech.default_speed() == 1.3
    monkeypatch.delenv("KOKORO_SPEED", raising=False)
    assert isinstance(speech.default_speed(), float)


def test_speech_log_path_uses_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "custom.jsonl"
    monkeypatch.setenv("EPHOR_SPEECH_LOG", str(target))
    assert speech.speech_log_path() == target


def test_speech_log_path_default_under_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EPHOR_SPEECH_LOG", raising=False)
    monkeypatch.setenv("EPHOR_STATE_DIR", str(tmp_path / "sessions"))
    p = speech.speech_log_path()
    assert p.name == "speech.jsonl"
    assert p.parent == tmp_path

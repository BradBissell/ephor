"""Unit tests for the SpeechPlayer queue logic.

Subprocess management is exercised by injecting a fake spawner — we don't
spawn kokoro in the test suite (no audio device, no model file, no point).
Test the *behavioural contracts* (FIFO, dedup, preempt, stop) and trust
the real spawner to do its job in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_orchestrator import speech
from claude_orchestrator.speech_player import (
    MAX_QUEUE,
    QueueItem,
    SpeechPlayer,
)


class FakeProc:
    """Minimal Popen stand-in for queue tests."""

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.pid = 99999  # the test stubs os.killpg so this is never signalled
        self._returncode: int | None = None

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = -15

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9

    def wait(self, timeout: float = 0) -> int:
        if self._returncode is None:
            self._returncode = 0
        return self._returncode

    def finish(self) -> None:
        """Test-only: simulate the subprocess exiting cleanly."""
        self._returncode = 0


@pytest.fixture
def proc_log() -> list[FakeProc]:
    return []


@pytest.fixture
def player(proc_log: list[FakeProc]) -> SpeechPlayer:
    def spawner(_item: QueueItem) -> FakeProc:
        proc = FakeProc()
        proc_log.append(proc)
        return proc  # type: ignore[return-value]

    # Patch os.killpg in the player module so terminate doesn't try to
    # signal a real process group on the FakeProc's bogus pid.
    import claude_orchestrator.speech_player as sp

    sp.os.killpg = lambda *_args, **_kwargs: None  # type: ignore[assignment]
    return SpeechPlayer(spawner=spawner)


def _item(sid: str, text: str = "Hello.") -> QueueItem:
    return QueueItem(session_id=sid, text=text, sentences=[text])


# ---- FIFO across different sessions --------------------------------------


def test_first_enqueue_starts_immediately(player: SpeechPlayer, proc_log: list[FakeProc]) -> None:
    player.enqueue(_item("a"))
    assert player.now_playing is not None
    assert player.now_playing.session_id == "a"
    assert player.queue_snapshot == []
    assert len(proc_log) == 1


def test_second_enqueue_for_different_session_queues(
    player: SpeechPlayer, proc_log: list[FakeProc]
) -> None:
    player.enqueue(_item("a"))
    player.enqueue(_item("b"))
    assert player.now_playing is not None
    assert player.now_playing.session_id == "a"
    assert [q.session_id for q in player.queue_snapshot] == ["b"]
    assert len(proc_log) == 1  # only `a` started spawning


def test_finished_playback_advances_queue(player: SpeechPlayer, proc_log: list[FakeProc]) -> None:
    player.enqueue(_item("a"))
    player.enqueue(_item("b"))
    player.enqueue(_item("c"))
    proc_log[0].finish()
    player.tick()
    assert player.now_playing is not None
    assert player.now_playing.session_id == "b"
    assert [q.session_id for q in player.queue_snapshot] == ["c"]


def test_queue_drains_in_order(player: SpeechPlayer, proc_log: list[FakeProc]) -> None:
    for sid in ("a", "b", "c"):
        player.enqueue(_item(sid))
    seen = [player.now_playing.session_id]  # type: ignore[union-attr]
    while player.now_playing is not None:
        proc_log[-1].finish()
        player.tick()
        if player.now_playing is not None:
            seen.append(player.now_playing.session_id)
    assert seen == ["a", "b", "c"]


# ---- same-session semantics ----------------------------------------------


def test_same_session_in_queue_replaces_existing_entry(
    player: SpeechPlayer, proc_log: list[FakeProc]
) -> None:
    """A new Stop for sid `b` while `b` is already queued must replace the
    queued entry, not duplicate it. We never want to read a stale reply
    for a conversation that just got a fresher response."""
    player.enqueue(_item("a"))
    player.enqueue(_item("b", text="OLD"))
    player.enqueue(_item("b", text="NEW"))
    assert [q.session_id for q in player.queue_snapshot] == ["b"]
    assert player.queue_snapshot[0].text == "NEW"
    # `a` is still playing; not preempted.
    assert player.now_playing.session_id == "a"  # type: ignore[union-attr]


def test_same_session_currently_playing_preempts(
    player: SpeechPlayer, proc_log: list[FakeProc]
) -> None:
    """User chose preempt-on-collision: a new response for the in-flight
    session kills the audio and starts the new one."""
    player.enqueue(_item("a", text="OLD"))
    old_proc = proc_log[-1]
    player.enqueue(_item("a", text="NEW"))
    # Previous proc was terminated.
    assert old_proc.terminated or old_proc.poll() is not None
    # New playback started for the same session.
    assert player.now_playing.session_id == "a"  # type: ignore[union-attr]
    assert player.now_playing.text == "NEW"  # type: ignore[union-attr]
    assert len(proc_log) == 2


def test_preempt_does_not_disturb_queue(player: SpeechPlayer, proc_log: list[FakeProc]) -> None:
    player.enqueue(_item("a", text="OLD"))
    player.enqueue(_item("b"))
    player.enqueue(_item("a", text="NEW"))  # preempts a; b stays queued
    assert player.now_playing.text == "NEW"  # type: ignore[union-attr]
    assert [q.session_id for q in player.queue_snapshot] == ["b"]


# ---- stop event ----------------------------------------------------------


def test_stop_event_drops_queued_session(player: SpeechPlayer) -> None:
    player.enqueue(_item("a"))
    player.enqueue(_item("b"))
    player.enqueue(_item("c"))
    player.stop("b")
    assert [q.session_id for q in player.queue_snapshot] == ["c"]
    assert player.now_playing.session_id == "a"  # type: ignore[union-attr]


def test_stop_event_kills_current_and_advances(
    player: SpeechPlayer, proc_log: list[FakeProc]
) -> None:
    """Used when UPS fires for the speaking session — kill the audio and
    move on to whatever's next in the queue."""
    player.enqueue(_item("a"))
    player.enqueue(_item("b"))
    proc_for_a = proc_log[-1]
    player.stop("a")
    assert proc_for_a.terminated or proc_for_a.poll() is not None
    assert player.now_playing.session_id == "b"  # type: ignore[union-attr]


# ---- queue cap -----------------------------------------------------------


def test_queue_caps_at_max_and_drops_oldest(
    player: SpeechPlayer,
) -> None:
    player.enqueue(_item("playing"))  # currently playing
    for i in range(MAX_QUEUE + 5):
        player.enqueue(_item(f"q{i}"))
    snap = player.queue_snapshot
    assert len(snap) == MAX_QUEUE
    # Oldest were dropped — we keep the most recent N.
    expected_tail = [f"q{i}" for i in range(5, MAX_QUEUE + 5)]
    assert [q.session_id for q in snap] == expected_tail


# ---- watcher integration -------------------------------------------------


def test_tick_routes_log_events_into_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proc_log: list[FakeProc]
) -> None:
    """The player's tick reads new log records via SpeechWatcher and
    routes them into the queue, so the TUI just needs to call tick()."""
    log = tmp_path / "speech.jsonl"
    monkeypatch.setenv("CCO_SPEECH_LOG", str(log))

    def spawner(_item: QueueItem) -> FakeProc:
        proc = FakeProc()
        proc_log.append(proc)
        return proc  # type: ignore[return-value]

    import claude_orchestrator.speech_player as sp

    sp.os.killpg = lambda *_a, **_k: None  # type: ignore[assignment]

    watcher = speech.SpeechWatcher(log)
    player = SpeechPlayer(spawner=spawner, watcher=watcher)

    speech.append_start("alpha", "Hello from alpha.")
    speech.append_start("beta", "Hello from beta.")

    player.tick()
    assert player.now_playing is not None
    assert player.now_playing.session_id == "alpha"
    assert [q.session_id for q in player.queue_snapshot] == ["beta"]


def test_tick_advances_after_subprocess_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proc_log: list[FakeProc]
) -> None:
    log = tmp_path / "speech.jsonl"
    monkeypatch.setenv("CCO_SPEECH_LOG", str(log))

    def spawner(_item: QueueItem) -> FakeProc:
        proc = FakeProc()
        proc_log.append(proc)
        return proc  # type: ignore[return-value]

    import claude_orchestrator.speech_player as sp

    sp.os.killpg = lambda *_a, **_k: None  # type: ignore[assignment]

    watcher = speech.SpeechWatcher(log)
    player = SpeechPlayer(spawner=spawner, watcher=watcher)

    speech.append_start("alpha", "First.")
    speech.append_start("beta", "Second.")
    player.tick()
    assert player.now_playing.session_id == "alpha"  # type: ignore[union-attr]
    proc_log[-1].finish()
    player.tick()
    assert player.now_playing.session_id == "beta"  # type: ignore[union-attr]


# ---- mute ---------------------------------------------------------------


def test_player_starts_unmuted_by_default(player: SpeechPlayer) -> None:
    assert player.is_muted is False


def test_muted_player_does_not_spawn_real_subprocess(
    proc_log: list[FakeProc],
) -> None:
    """When muted, the player still tracks the queue (so the bar mirrors
    activity) but spawner is bypassed — no audio."""
    import claude_orchestrator.speech_player as sp

    sp.os.killpg = lambda *_a, **_k: None  # type: ignore[assignment]

    def spawner(_item: QueueItem) -> FakeProc:
        proc = FakeProc()
        proc_log.append(proc)
        return proc  # type: ignore[return-value]

    player = SpeechPlayer(spawner=spawner, muted=True)
    player.enqueue(_item("a"))
    assert player.now_playing is not None  # tracked in current
    assert player.now_playing.session_id == "a"
    assert proc_log == []  # but no subprocess spawned


def test_muting_mid_playback_silences_but_keeps_current(
    player: SpeechPlayer, proc_log: list[FakeProc]
) -> None:
    """Pressing mute while audio is playing should kill the audio
    immediately but keep the item visible on the bar — UX expectation
    is "shut up", not "skip ahead." Natural reap timing handles
    advancing to the next queued item."""
    player.enqueue(_item("a"))
    proc_a = proc_log[-1]
    assert player.is_muted is False
    player.set_muted(True)
    assert player.is_muted is True
    # Subprocess was killed (terminate or wait set returncode).
    assert proc_a.poll() is not None
    # Item is still "current" so the bar continues to render it.
    assert player.now_playing is not None
    assert player.now_playing.session_id == "a"


def test_unmute_does_not_restart_playback(player: SpeechPlayer, proc_log: list[FakeProc]) -> None:
    """Unmuting mid-message shouldn't replay the same item from scratch.
    The current item finishes its silent run; subsequent items play
    aloud as normal."""
    player.set_muted(True)
    player.enqueue(_item("a"))
    assert proc_log == []
    player.set_muted(False)
    # No retroactive spawn for the already-current item.
    assert proc_log == []
    # New enqueues for a different session DO spawn (preempt unrelated).
    proc_log.clear()


def test_unmute_then_new_item_spawns_audio(player: SpeechPlayer, proc_log: list[FakeProc]) -> None:
    player.set_muted(True)
    player.enqueue(_item("a"))
    # Drain `a` synthetically so queue is empty.
    player.stop("a")
    proc_log.clear()
    player.set_muted(False)
    player.enqueue(_item("b"))
    assert len(proc_log) == 1


def test_set_muted_idempotent(player: SpeechPlayer, proc_log: list[FakeProc]) -> None:
    player.enqueue(_item("a"))
    proc_a = proc_log[-1]
    player.set_muted(False)  # was already unmuted
    assert proc_a.poll() is None  # not killed


# ---- calibration --------------------------------------------------------


def test_natural_completion_updates_calibrated_rate(
    proc_log: list[FakeProc],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a real subprocess exits cleanly, the player records the
    observed chars/sec back to speech_settings so the next playback's
    progress estimate matches actual kokoro speed."""
    monkeypatch.setenv("CCO_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("CCO_TTS_ENABLED", raising=False)
    monkeypatch.delenv("CCO_SPEECH_CHARS_PER_SEC", raising=False)

    import claude_orchestrator.speech_player as sp
    from claude_orchestrator import speech_settings

    sp.os.killpg = lambda *_a, **_k: None  # type: ignore[assignment]

    # Fake "now" so we can control the observed duration deterministically.
    base_time = 1_000_000.0
    monkeypatch.setattr(sp.time, "time", lambda: base_time)

    def spawner(_item: QueueItem) -> FakeProc:
        proc = FakeProc()
        proc_log.append(proc)
        return proc  # type: ignore[return-value]

    player = SpeechPlayer(spawner=spawner)
    text = "x" * 500  # well over the calibration min (100 chars)
    player.enqueue(QueueItem(session_id="a", text=text, sentences=[text], speed=1.0))
    proc_log[-1].finish()
    # Advance "now" by 30 wall-clock seconds, then tick to reap.
    monkeypatch.setattr(sp.time, "time", lambda: base_time + 30.0)
    player.tick()

    rate = speech_settings.load().calibrated_chars_per_sec
    assert rate is not None
    # 500 chars / (30s - 1.5s startup) ≈ 17.5 chars/sec.
    assert 15.0 <= rate <= 20.0


def test_preempted_playback_does_not_calibrate(
    proc_log: list[FakeProc],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A killed playback's duration is truncated and would skew the
    rolling average low. Make sure preempt → no calibration."""
    monkeypatch.setenv("CCO_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("CCO_TTS_ENABLED", raising=False)

    import claude_orchestrator.speech_player as sp
    from claude_orchestrator import speech_settings

    sp.os.killpg = lambda *_a, **_k: None  # type: ignore[assignment]

    def spawner(_item: QueueItem) -> FakeProc:
        proc = FakeProc()
        proc_log.append(proc)
        return proc  # type: ignore[return-value]

    player = SpeechPlayer(spawner=spawner)
    text = "x" * 500
    player.enqueue(QueueItem(session_id="a", text=text, sentences=[text], speed=1.0))
    # Same-session preempt — kills the in-flight proc.
    player.enqueue(QueueItem(session_id="a", text=text, sentences=[text], speed=1.0))

    rate = speech_settings.load().calibrated_chars_per_sec
    assert rate is None  # preempted samples shouldn't ever land in calibration


def test_short_text_is_not_calibrated(
    proc_log: list[FakeProc],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tiny messages are dominated by startup latency and produce noisy
    estimates. Player should skip calibration below the threshold."""
    monkeypatch.setenv("CCO_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("CCO_TTS_ENABLED", raising=False)

    import claude_orchestrator.speech_player as sp
    from claude_orchestrator import speech_settings

    sp.os.killpg = lambda *_a, **_k: None  # type: ignore[assignment]
    base_time = 1_000_000.0
    monkeypatch.setattr(sp.time, "time", lambda: base_time)

    def spawner(_item: QueueItem) -> FakeProc:
        proc = FakeProc()
        proc_log.append(proc)
        return proc  # type: ignore[return-value]

    player = SpeechPlayer(spawner=spawner)
    short = "Done."  # 5 chars, below threshold
    player.enqueue(QueueItem(session_id="a", text=short, sentences=[short], speed=1.0))
    proc_log[-1].finish()
    monkeypatch.setattr(sp.time, "time", lambda: base_time + 3.0)
    player.tick()

    rate = speech_settings.load().calibrated_chars_per_sec
    assert rate is None


def test_calibration_uses_exponential_moving_average(
    proc_log: list[FakeProc],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single bad reading shouldn't yank the rate; multiple consistent
    readings should converge."""
    monkeypatch.setenv("CCO_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("CCO_TTS_ENABLED", raising=False)

    import claude_orchestrator.speech_player as sp
    from claude_orchestrator import speech_settings

    sp.os.killpg = lambda *_a, **_k: None  # type: ignore[assignment]

    def spawner(_item: QueueItem) -> FakeProc:
        proc = FakeProc()
        proc_log.append(proc)
        return proc  # type: ignore[return-value]

    base_time = 1_000_000.0

    def fake_time(offset: float = 0) -> float:
        return base_time + offset

    # Feed a stable rate of ~20 chars/sec across 5 messages.
    monkeypatch.setattr(sp.time, "time", lambda: fake_time(0))
    player = SpeechPlayer(spawner=spawner)
    text = "y" * 500
    elapsed = 0
    for _ in range(5):
        monkeypatch.setattr(sp.time, "time", lambda e=elapsed: fake_time(e))
        player.enqueue(QueueItem(session_id="a", text=text, sentences=[text], speed=1.0))
        proc_log[-1].finish()
        elapsed += 26.5  # 500 / (26.5 - 1.5) ≈ 20 chars/sec
        monkeypatch.setattr(sp.time, "time", lambda e=elapsed: fake_time(e))
        player.tick()

    rate = speech_settings.load().calibrated_chars_per_sec
    assert rate is not None
    # Should converge close to 20.
    assert 18.0 <= rate <= 22.0


# ---- watcher unit -------------------------------------------------------


def test_watcher_returns_only_new_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "speech.jsonl"
    monkeypatch.setenv("CCO_SPEECH_LOG", str(log))
    speech.append_start("alpha", "Old.")
    watcher = speech.SpeechWatcher(log)
    # Initial position is end-of-file → first poll should return nothing.
    assert watcher.poll() == []
    speech.append_start("beta", "New.")
    events = watcher.poll()
    assert len(events) == 1
    assert events[0]["session_id"] == "beta"
    # Subsequent poll with no new writes returns []
    assert watcher.poll() == []


def test_watcher_recovers_from_truncation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "speech.jsonl"
    monkeypatch.setenv("CCO_SPEECH_LOG", str(log))
    speech.append_start("a", "A.")
    speech.append_start("b", "B.")
    watcher = speech.SpeechWatcher(log)
    # Simulate the truncation pass: rewrite file with just one short line.
    log.write_text(
        '{"event":"start","ts":"2026-04-29T10:00:00.000000Z",'
        '"session_id":"c","text":"C.","sentences":["C."],"speed":1.0}\n'
    )
    events = watcher.poll()
    assert len(events) == 1
    assert events[0]["session_id"] == "c"


# ---- speak_mode: summary --------------------------------------------------


def _start_event(
    sid: str,
    text: str = "long full assistant reply.",
    *,
    transcript_path: str | None = None,
    cwd: str | None = None,
) -> dict[str, object]:
    ev: dict[str, object] = {
        "event": "start",
        "session_id": sid,
        "text": text,
        "sentences": [text],
        "speed": 1.3,
    }
    if transcript_path is not None:
        ev["transcript_path"] = transcript_path
    if cwd is not None:
        ev["cwd"] = cwd
    return ev


def test_full_mode_routes_event_directly_into_queue(
    proc_log: list[FakeProc],
) -> None:
    """In `full` mode, the player ignores transcript_path and speaks the
    full text the hook captured. No summarizer call."""
    import claude_orchestrator.speech_player as sp

    sp.os.killpg = lambda *_a, **_k: None  # type: ignore[assignment]

    calls: list[tuple[Path, str | None]] = []

    def fake_summarizer(path: Path, cwd: str | None) -> str:
        calls.append((path, cwd))
        return "should-not-be-used"

    player = SpeechPlayer(
        spawner=lambda _i: proc_log.append(FakeProc()) or proc_log[-1],  # type: ignore[return-value]
        speak_mode=sp.SPEAK_MODE_FULL,
        summarizer=fake_summarizer,
    )
    player._route_event(_start_event("s1", "full reply", transcript_path="/tmp/x"))

    assert calls == []
    assert player.now_playing is not None
    assert player.now_playing.text == "full reply"


def test_summary_mode_replaces_text_with_summarizer_output(
    proc_log: list[FakeProc], tmp_path: Path
) -> None:
    """A start event in summary mode is held back until the summarizer
    finishes; the resulting QueueItem carries the brief, not the full reply."""
    import claude_orchestrator.speech_player as sp

    sp.os.killpg = lambda *_a, **_k: None  # type: ignore[assignment]

    def fake_summarizer(path: Path, cwd: str | None) -> str:
        return "DR-1: brief"

    player = SpeechPlayer(
        spawner=lambda _i: proc_log.append(FakeProc()) or proc_log[-1],  # type: ignore[return-value]
        speak_mode=sp.SPEAK_MODE_SUMMARY,
        summarizer=fake_summarizer,
    )
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")

    player._route_event(_start_event("s1", "the full reply that we DO NOT want spoken",
                                       transcript_path=str(transcript),
                                       cwd=str(tmp_path)))

    # Nothing playing yet — the worker has to run + the next tick drains
    # the pending queue.
    _wait_for_pending(player)
    player.tick()

    assert player.now_playing is not None
    assert player.now_playing.text == "DR-1: brief"


def test_summary_mode_falls_back_to_truncated_text_when_summarizer_returns_empty(
    proc_log: list[FakeProc], tmp_path: Path
) -> None:
    import claude_orchestrator.speech_player as sp

    sp.os.killpg = lambda *_a, **_k: None  # type: ignore[assignment]

    def empty_summarizer(_p: Path, _cwd: str | None) -> str:
        return ""

    player = SpeechPlayer(
        spawner=lambda _i: proc_log.append(FakeProc()) or proc_log[-1],  # type: ignore[return-value]
        speak_mode=sp.SPEAK_MODE_SUMMARY,
        summarizer=empty_summarizer,
    )
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")

    full_text = "short reply"
    player._route_event(_start_event("s1", full_text, transcript_path=str(transcript)))

    _wait_for_pending(player)
    player.tick()
    assert player.now_playing is not None
    # Fallback: speaks the original text (audible notification still happens).
    assert player.now_playing.text == full_text


def test_summary_mode_without_transcript_path_falls_back_to_full(
    proc_log: list[FakeProc],
) -> None:
    """A start event missing transcript_path can't be summarized — play
    the full text rather than dropping the notification entirely."""
    import claude_orchestrator.speech_player as sp

    sp.os.killpg = lambda *_a, **_k: None  # type: ignore[assignment]

    def panic_summarizer(_p: Path, _cwd: str | None) -> str:
        raise AssertionError("summarizer should not be called without transcript_path")

    player = SpeechPlayer(
        spawner=lambda _i: proc_log.append(FakeProc()) or proc_log[-1],  # type: ignore[return-value]
        speak_mode=sp.SPEAK_MODE_SUMMARY,
        summarizer=panic_summarizer,
    )
    player._route_event(_start_event("s1", "fallback content"))
    assert player.now_playing is not None
    assert player.now_playing.text == "fallback content"


def test_set_speak_mode_runtime_switch() -> None:
    import claude_orchestrator.speech_player as sp

    player = SpeechPlayer(spawner=lambda _i: None)
    assert player.speak_mode == sp.SPEAK_MODE_FULL
    player.set_speak_mode(sp.SPEAK_MODE_SUMMARY)
    assert player.speak_mode == sp.SPEAK_MODE_SUMMARY
    # Unknown values are ignored — defense against typos in env / CLI.
    player.set_speak_mode("loud-please")
    assert player.speak_mode == sp.SPEAK_MODE_SUMMARY


def _wait_for_pending(player: SpeechPlayer, *, timeout: float = 2.0) -> None:
    """Block until the background summarizer thread posts a result."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not player._pending.empty():
            return
        time.sleep(0.01)
    raise AssertionError("summarizer thread did not produce a pending item in time")

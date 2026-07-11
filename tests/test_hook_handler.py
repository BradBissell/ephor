"""End-to-end tests for the hook handler shell script.

These tests exercise the actual event_handler.sh by piping JSON to it and
inspecting the resulting state file. They require `bash` and `jq` on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from ephor.config import hook_handler_path

HANDLER = hook_handler_path()
SCHEMA_VERSION = 3


@pytest.fixture
def state_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Sandboxed env: state/pending/lock dirs all under tmp_path."""
    state = tmp_path / "sessions"
    pending = tmp_path / "pending"
    lock = tmp_path / "locks"
    speech_log = tmp_path / "speech.jsonl"
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "EPHOR_STATE_DIR": str(state),
        "EPHOR_PENDING_DIR": str(pending),
        "EPHOR_LOCK_DIR": str(lock),
        "EPHOR_SPEECH_LOG": str(speech_log),
    }
    return env


def _fire_hook(
    input_json: dict[str, object], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HANDLER)],
        input=json.dumps(input_json),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
        check=False,
    )


def _state_file(env: dict[str, str], sid: str) -> Path:
    return Path(env["EPHOR_STATE_DIR"]) / f"{sid}.json"


def _read_state(env: dict[str, str], sid: str) -> dict[str, object]:
    return json.loads(_state_file(env, sid).read_text())


def _required_tools_present() -> bool:
    return all(shutil.which(t) for t in ("bash", "jq", "flock"))


@pytest.fixture(autouse=True)
def _skip_if_tools_missing() -> None:
    if not _required_tools_present():
        pytest.skip("requires bash + jq + flock on PATH")


def test_session_start_writes_idle_state(state_env: dict[str, str]) -> None:
    sid = "test-session-1"
    result = _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "SessionStart",
            "cwd": "/tmp/myproject",
        },
        state_env,
    )
    assert result.returncode == 0, result.stderr
    state = _read_state(state_env, sid)
    assert state["session_id"] == sid
    assert state["status"] == "IDLE"
    assert state["schema_version"] == SCHEMA_VERSION
    assert state["project_name"] == "myproject"
    assert state["last_event"] == "SessionStart"
    assert state["last_event_seq"] == 1


def test_pre_tool_use_increments_tool_count(state_env: dict[str, str]) -> None:
    sid = "test-session-2"
    for _ in range(3):
        result = _fire_hook(
            {
                "session_id": sid,
                "hook_event_name": "PreToolUse",
                "cwd": "/tmp/x",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
            },
            state_env,
        )
        assert result.returncode == 0
    state = _read_state(state_env, sid)
    assert state["status"] == "WORKING"
    assert state["tool_count"] == 3
    assert state["last_event_seq"] == 3


def test_notification_question_transitions_to_waiting_answer(
    state_env: dict[str, str],
) -> None:
    sid = "test-session-3"
    _fire_hook(
        {"session_id": sid, "hook_event_name": "SessionStart", "cwd": "/tmp/x"},
        state_env,
    )
    _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "Notification",
            "cwd": "/tmp/x",
            "message": "Claude is waiting for your input",
        },
        state_env,
    )
    state = _read_state(state_env, sid)
    assert state["status"] == "WAITING_ANSWER"
    assert state["notification"]["type"] == "question"


def test_permission_request_transitions_to_waiting_permission(
    state_env: dict[str, str],
) -> None:
    sid = "test-session-4"
    _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "PermissionRequest",
            "cwd": "/tmp/x",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
        },
        state_env,
    )
    state = _read_state(state_env, sid)
    assert state["status"] == "WAITING_PERMISSION"
    assert state["notification"]["type"] == "permission"
    assert state["notification"]["tool"] == "Bash"


def test_permission_request_emits_pending_decision(
    state_env: dict[str, str],
) -> None:
    sid = "test-session-5"
    pending_file = Path(state_env["EPHOR_PENDING_DIR"]) / f"{sid}.json"
    pending_file.parent.mkdir(parents=True, exist_ok=True)
    pending_file.write_text(json.dumps({"hookSpecificOutput": {"decision": {"behavior": "allow"}}}))
    result = _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "PermissionRequest",
            "cwd": "/tmp/x",
            "tool_name": "Bash",
        },
        state_env,
    )
    assert result.returncode == 0
    decision = json.loads(result.stdout)
    assert decision["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert not pending_file.exists(), "pending file should be consumed after read"


def test_post_tool_use_failure_increments_error_count(
    state_env: dict[str, str],
) -> None:
    sid = "test-session-6"
    _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "PostToolUseFailure",
            "cwd": "/tmp/x",
            "tool_name": "Bash",
        },
        state_env,
    )
    state = _read_state(state_env, sid)
    assert state["status"] == "ERROR"
    assert state["error_count"] == 1


def test_stop_transitions_to_idle(state_env: dict[str, str]) -> None:
    sid = "test-session-7"
    _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "PreToolUse",
            "cwd": "/tmp/x",
            "tool_name": "Bash",
        },
        state_env,
    )
    _fire_hook(
        {"session_id": sid, "hook_event_name": "Stop", "cwd": "/tmp/x"},
        state_env,
    )
    state = _read_state(state_env, sid)
    assert state["status"] == "IDLE"


def test_handler_fails_open_on_missing_session_id(state_env: dict[str, str]) -> None:
    result = _fire_hook(
        {"hook_event_name": "PreToolUse", "cwd": "/tmp/x"},
        state_env,
    )
    # Must not block claude — exit 0, no state file written.
    assert result.returncode == 0
    sessions = list(Path(state_env["EPHOR_STATE_DIR"]).glob("*.json"))
    assert sessions == []


def test_handler_fails_open_on_invalid_session_id(state_env: dict[str, str]) -> None:
    result = _fire_hook(
        {
            "session_id": "../escape-attempt",
            "hook_event_name": "SessionStart",
            "cwd": "/tmp/x",
        },
        state_env,
    )
    assert result.returncode == 0
    # No file should be created with a path-traversal session_id.
    sessions = list(Path(state_env["EPHOR_STATE_DIR"]).rglob("*.json"))
    assert sessions == []


def test_handler_fails_open_on_garbage_input(state_env: dict[str, str]) -> None:
    result = subprocess.run(
        ["bash", str(HANDLER)],
        input="not valid json at all",
        capture_output=True,
        text=True,
        timeout=5,
        env=state_env,
        check=False,
    )
    assert result.returncode == 0


def test_state_file_permissions_are_0600(state_env: dict[str, str]) -> None:
    sid = "test-session-8"
    _fire_hook(
        {"session_id": sid, "hook_event_name": "SessionStart", "cwd": "/tmp/x"},
        state_env,
    )
    mode = _state_file(state_env, sid).stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_cco_internal_env_skips_state_write(state_env: dict[str, str]) -> None:
    """When the summarizer subprocess fires `claude -p`, our hook handler
    sees EPHOR_INTERNAL=1 in the inherited env and exits without writing a
    state file. Without this guard, every summarization would create a
    ghost session in the dashboard."""
    sid = "test-session-internal"
    result = _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "SessionStart",
            "cwd": "/tmp/myproject",
        },
        {**state_env, "EPHOR_INTERNAL": "1"},
    )
    assert result.returncode == 0
    # No state file written.
    assert not _state_file(state_env, sid).exists()


def test_user_prompt_submit_records_last_summary(state_env: dict[str, str]) -> None:
    sid = "test-session-prompt"
    result = _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/tmp/myproject",
            "prompt": "fix the failing test in tests/test_foo.py",
        },
        state_env,
    )
    assert result.returncode == 0, result.stderr
    state = _read_state(state_env, sid)
    assert state["last_summary"] == "fix the failing test in tests/test_foo.py"


def test_user_prompt_submit_truncates_to_70_chars(state_env: dict[str, str]) -> None:
    sid = "test-session-prompt-long"
    long_prompt = "x" * 200
    _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/tmp/myproject",
            "prompt": long_prompt,
        },
        state_env,
    )
    state = _read_state(state_env, sid)
    assert isinstance(state["last_summary"], str)
    assert len(state["last_summary"]) == 70


def test_user_prompt_submit_strips_newlines(state_env: dict[str, str]) -> None:
    """Newlines/tabs become spaces so the JSON state file stays single-line-safe."""
    sid = "test-session-prompt-multi"
    _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/tmp/myproject",
            "prompt": "first line\nsecond\tline",
        },
        state_env,
    )
    state = _read_state(state_env, sid)
    assert "\n" not in state["last_summary"]  # type: ignore[operator]
    assert "\t" not in state["last_summary"]  # type: ignore[operator]


def test_concurrent_writes_dont_lose_updates(state_env: dict[str, str]) -> None:
    """Fire 10 hooks concurrently for the same session; tool_count must equal 10."""
    sid = "test-session-9"
    procs: list[subprocess.Popen[str]] = []
    for _ in range(10):
        p = subprocess.Popen(
            ["bash", str(HANDLER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=state_env,
        )
        p.stdin.write(  # type: ignore[union-attr]
            json.dumps(
                {
                    "session_id": sid,
                    "hook_event_name": "PreToolUse",
                    "cwd": "/tmp/x",
                    "tool_name": "Bash",
                }
            )
        )
        p.stdin.close()  # type: ignore[union-attr]
        procs.append(p)

    for p in procs:
        p.wait(timeout=5)
        assert p.returncode == 0

    state = _read_state(state_env, sid)
    assert state["tool_count"] == 10, (
        f"flock should have serialised all 10 writes; got {state['tool_count']}"
    )
    assert state["last_event_seq"] == 10


def _run_populate_tmux_mapping(
    stdin_json: str,
    *,
    tmux_body: str | None = None,
    tmux_set: bool = True,
) -> str:
    """Source event_handler.sh's `populate_tmux_mapping` in a subshell with a
    controlled `tmux` shim and run it against stdin_json. Lets us exercise
    the function in isolation without fighting the handler's PATH reset.
    """
    handler_text = HANDLER.read_text()
    start = handler_text.index("populate_tmux_mapping() {")
    end = handler_text.index("\n}\n", start) + 3
    fn_body = handler_text[start:end]

    body = tmux_body if tmux_body is not None else 'echo ""; return 1'
    tmux_var = "fake-server" if tmux_set else ""
    script = (
        "#!/usr/bin/env bash\nset -u\n"
        f'TMUX="{tmux_var}"\n'
        f"tmux() {{ {body}; }}\nexport -f tmux\n\n"
        f"{fn_body}\n\n"
        "populate_tmux_mapping\n"
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        input=stdin_json,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_populate_tmux_mapping_enriches_stdin_with_tmux_fields() -> None:
    out = _run_populate_tmux_mapping(
        '{"session_id":"x","status":"IDLE"}',
        tmux_body=r'printf "work\tclaude\t%%9"',
    )
    parsed = json.loads(out)
    assert parsed["tmux_session"] == "work"
    assert parsed["tmux_window"] == "claude"
    assert parsed["tmux_pane"] == "%9"
    assert parsed["session_id"] == "x"


def test_populate_tmux_mapping_passes_stdin_through_when_tmux_unset() -> None:
    """No TMUX env → no enrichment, but the JSON must survive intact."""
    body = '{"session_id":"x","status":"IDLE"}'
    out = _run_populate_tmux_mapping(body, tmux_set=False)
    assert json.loads(out) == json.loads(body)


def test_populate_tmux_mapping_passes_stdin_through_on_tmux_failure() -> None:
    """Regression: a non-zero `tmux display-message` (server hiccup) used to
    cause populate_tmux_mapping to silently drop stdin, producing an empty
    state file. It must always pass stdin through."""
    body = '{"session_id":"x","status":"WORKING"}'
    out = _run_populate_tmux_mapping(body, tmux_body="return 1")
    assert json.loads(out) == json.loads(body)


def test_populate_tmux_mapping_passes_stdin_through_on_empty_tmux_output() -> None:
    body = '{"session_id":"x","status":"WORKING"}'
    out = _run_populate_tmux_mapping(body, tmux_body='echo ""')
    assert json.loads(out) == json.loads(body)


# ---- speech-event log emission -------------------------------------------


def _write_transcript(path: Path, assistant_text: str) -> None:
    """Write a minimal Claude Code transcript JSONL with one assistant message."""
    rec = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": assistant_text}]},
    }
    path.write_text(json.dumps(rec) + "\n")


def _wait_for_speech_log(path: Path, timeout: float = 6.0) -> list[dict[str, object]]:
    """Poll for the backgrounded speech-event subshell to flush. Returns the
    parsed records once at least one start event has landed, or raises."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                lines = path.read_text().strip().splitlines()
            except OSError:
                lines = []
            records = [json.loads(line) for line in lines if line.strip()]
            if any(r.get("event") == "start" for r in records):
                return records
        time.sleep(0.2)
    raise AssertionError(f"speech log {path} never gained a start event")


def test_stop_hook_appends_speech_start_event(state_env: dict[str, str], tmp_path: Path) -> None:
    """Stop hook must read the assistant text from the transcript and emit a
    start record to the speech log so the TUI's SpeechBar can mirror TTS."""
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, "First sentence. Second one!")
    sid = "speech-sess-1"
    result = _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "Stop",
            "cwd": "/tmp/myproject",
            "transcript_path": str(transcript),
        },
        state_env,
    )
    assert result.returncode == 0, result.stderr

    speech_log = Path(state_env["EPHOR_SPEECH_LOG"])
    records = _wait_for_speech_log(speech_log)
    starts = [r for r in records if r.get("event") == "start"]
    assert len(starts) >= 1
    rec = starts[-1]
    assert rec["session_id"] == sid
    assert "First sentence." in rec["text"]  # type: ignore[operator]
    assert isinstance(rec["sentences"], list)
    assert rec["sentences"]  # non-empty
    assert "speed" in rec


def test_user_prompt_submit_appends_speech_stop_event(
    state_env: dict[str, str],
) -> None:
    """A new user prompt cancels in-flight TTS — the speech log must reflect
    that immediately so the bar clears without waiting for the
    estimated-duration timeout."""
    sid = "speech-sess-2"
    _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/tmp/myproject",
            "prompt": "another prompt",
        },
        state_env,
    )
    speech_log = Path(state_env["EPHOR_SPEECH_LOG"])
    assert speech_log.is_file(), "UPS must write the speech log inline"
    records = [json.loads(line) for line in speech_log.read_text().strip().splitlines()]
    assert any(r.get("event") == "stop" and r.get("session_id") == sid for r in records)


def test_speech_max_chars_env_extends_cap(state_env: dict[str, str], tmp_path: Path) -> None:
    """EPHOR_SPEECH_MAX_CHARS must override the default cap so users with
    longer-form responses can have the full text mirrored on the bar."""
    long_text = "Sentence one. " + ("filler word " * 400) + "Done."  # ~5000 chars
    transcript = tmp_path / "transcript-long.jsonl"
    _write_transcript(transcript, long_text)
    sid = "speech-long"
    env = dict(state_env)
    env["EPHOR_SPEECH_MAX_CHARS"] = "6000"
    _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "Stop",
            "cwd": "/tmp/x",
            "transcript_path": str(transcript),
        },
        env,
    )
    speech_log = Path(env["EPHOR_SPEECH_LOG"])
    records = _wait_for_speech_log(speech_log)
    starts = [r for r in records if r.get("event") == "start"]
    assert len(starts) >= 1
    text = starts[-1]["text"]
    assert isinstance(text, str)
    # Pre-fix this would be capped at 1500; with the env override and a
    # long enough source, we should retain well beyond the legacy cap.
    assert len(text) > 1500


def test_stop_hook_skips_speech_event_when_transcript_missing(
    state_env: dict[str, str], tmp_path: Path
) -> None:
    """No transcript_path → no speech record, but the hook must still exit
    cleanly and the session state must transition to IDLE."""
    sid = "speech-sess-3"
    result = _fire_hook(
        {"session_id": sid, "hook_event_name": "Stop", "cwd": "/tmp/x"},
        state_env,
    )
    assert result.returncode == 0
    state = _read_state(state_env, sid)
    assert state["status"] == "IDLE"

    import time

    # Give the would-be background subshell a moment to fail/exit. With
    # no transcript_path it returns immediately, so the log file should
    # not be created at all.
    time.sleep(0.3)
    speech_log = Path(state_env["EPHOR_SPEECH_LOG"])
    if speech_log.is_file():
        # If it does exist (e.g. from a prior fire in the same env), at
        # least confirm we didn't add a bogus start record.
        records = [json.loads(line) for line in speech_log.read_text().strip().splitlines()]
        assert not any(r.get("event") == "start" and r.get("session_id") == sid for r in records)


# ---------------------------------------------------------------------------
# multi-provider event-name / field-name dialects
# ---------------------------------------------------------------------------


def _fire_as(
    provider: str, input_json: dict[str, object], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Fire the handler tagged as a given provider (as the installer would)."""
    return subprocess.run(
        ["bash", str(HANDLER)],
        input=json.dumps(input_json),
        capture_output=True,
        text=True,
        timeout=5,
        env={**env, "EPHOR_PROVIDER": provider},
        check=False,
    )


def test_records_provider_from_env(state_env: dict[str, str]) -> None:
    sid = "prov-1"
    _fire_as(
        "gemini", {"session_id": sid, "hook_event_name": "SessionStart", "cwd": "/p"}, state_env
    )
    assert _read_state(state_env, sid)["provider"] == "gemini"


def test_gemini_before_tool_maps_to_working(state_env: dict[str, str]) -> None:
    sid = "gem-tool"
    _fire_as(
        "gemini",
        {
            "session_id": sid,
            "hook_event_name": "BeforeTool",
            "cwd": "/p",
            "tool_name": "write_file",
        },
        state_env,
    )
    state = _read_state(state_env, sid)
    assert state["status"] == "WORKING"
    assert state["tool_count"] == 1


def test_gemini_after_agent_maps_to_idle(state_env: dict[str, str]) -> None:
    sid = "gem-done"
    _fire_as("gemini", {"session_id": sid, "hook_event_name": "BeforeTool", "cwd": "/p"}, state_env)
    _fire_as("gemini", {"session_id": sid, "hook_event_name": "AfterAgent", "cwd": "/p"}, state_env)
    assert _read_state(state_env, sid)["status"] == "IDLE"


def test_gemini_tool_permission_notification_waits(state_env: dict[str, str]) -> None:
    sid = "gem-perm"
    _fire_as(
        "gemini",
        {
            "session_id": sid,
            "hook_event_name": "Notification",
            "cwd": "/p",
            "notification_type": "ToolPermission",
            "message": "Allow write?",
        },
        state_env,
    )
    state = _read_state(state_env, sid)
    assert state["status"] == "WAITING_PERMISSION"
    assert state["notification"]["type"] == "permission"


def test_grok_user_prompt_field_captured(state_env: dict[str, str]) -> None:
    sid = "grok-prompt"
    _fire_as(
        "grok",
        {
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/p",
            "user_prompt": "please refactor the parser",
        },
        state_env,
    )
    assert _read_state(state_env, sid)["last_summary"] == "please refactor the parser"


def test_rejects_non_lowercase_provider(state_env: dict[str, str]) -> None:
    """A junk EPHOR_PROVIDER is dropped rather than recorded verbatim."""
    sid = "prov-junk"
    _fire_as(
        "../evil", {"session_id": sid, "hook_event_name": "SessionStart", "cwd": "/p"}, state_env
    )
    assert _read_state(state_env, sid)["provider"] == ""


# ---------------------------------------------------------------------------
# Grok Build dialect (camelCase keys, snake_case event values, env detection)
# ---------------------------------------------------------------------------


def _fire_grok(
    event_value: str, input_json: dict[str, object], env: dict[str, str], sid: str
) -> subprocess.CompletedProcess[str]:
    """Fire as xAI Grok Build would: EPHOR_PROVIDER=claude (via ~/.claude compat)
    but GROK_* env set, and a camelCase payload."""
    return subprocess.run(
        ["bash", str(HANDLER)],
        input=json.dumps(input_json),
        capture_output=True,
        text=True,
        timeout=5,
        env={
            **env,
            "EPHOR_PROVIDER": "claude",
            "GROK_SESSION_ID": sid,
            "GROK_HOOK_EVENT": event_value,
        },
        check=False,
    )


def test_grok_build_dialect_is_parsed_and_labeled(state_env: dict[str, str]) -> None:
    sid = "019f4f01-8ec5-77c2-8ecb-a85fbb656d44"
    # snake_case hookEventName + camelCase sessionId, as Grok Build sends.
    _fire_grok(
        "pre_tool_use",
        {
            "hookEventName": "pre_tool_use",
            "sessionId": sid,
            "cwd": "/proj",
            "toolName": "run_terminal_command",
        },
        state_env,
        sid,
    )
    state = _read_state(state_env, sid)
    # session id came from .sessionId; snake_case event normalized to PascalCase.
    assert state["last_event"] == "PreToolUse"
    assert state["status"] == "WORKING"
    assert state["tool_count"] == 1
    # provider corrected to grok via the GROK_* env, despite EPHOR_PROVIDER=claude.
    assert state["provider"] == "grok"


def test_grok_build_stop_maps_to_idle(state_env: dict[str, str]) -> None:
    sid = "019f4f01-dead-beef-cafe-000000000000"
    _fire_grok(
        "session_start",
        {"hookEventName": "session_start", "sessionId": sid, "cwd": "/p"},
        state_env,
        sid,
    )
    _fire_grok("stop", {"hookEventName": "stop", "sessionId": sid, "cwd": "/p"}, state_env, sid)
    state = _read_state(state_env, sid)
    assert state["last_event"] == "Stop"
    assert state["status"] == "IDLE"
    assert state["provider"] == "grok"


def test_grok_build_acp_transcript_reply_captured(
    state_env: dict[str, str], tmp_path: Path
) -> None:
    """Grok Build's `stop` carries a transcriptPath to an ACP session/update
    stream; the handler extracts the last completed turn's agent text for TTS."""
    acp = tmp_path / "updates.jsonl"
    acp.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "user_message_chunk",
                            "content": {"type": "text", "text": "hi"},
                        }
                    },
                },
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "A semaphore "},
                        }
                    },
                },
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "limits concurrency."},
                        }
                    },
                },
                {
                    "method": "session/update",
                    "params": {"update": {"sessionUpdate": "turn_completed"}},
                },
            ]
        )
        + "\n"
    )
    sid = "019f4f08-8935-7abc-def0-112233445566"
    subprocess.run(
        ["bash", str(HANDLER)],
        input=json.dumps(
            {"hookEventName": "stop", "sessionId": sid, "cwd": "/p", "transcriptPath": str(acp)}
        ),
        capture_output=True,
        text=True,
        timeout=8,
        env={**state_env, "GROK_SESSION_ID": sid, "GROK_HOOK_EVENT": "stop"},
        check=False,
    )
    # emit_speech_start_event is backgrounded; wait briefly for the record.
    speech_log = Path(state_env["EPHOR_SPEECH_LOG"])
    text = ""
    for _ in range(20):
        if speech_log.exists():
            for line in speech_log.read_text().splitlines():
                ev = json.loads(line)
                if ev.get("event") == "start":
                    text = ev.get("text", "")
        if text:
            break
        time.sleep(0.2)
    assert "semaphore" in text.lower()
    assert "limits concurrency" in text

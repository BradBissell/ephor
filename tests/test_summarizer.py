"""Tests for the LLM summarizer.

Mocks `subprocess.run` and `shutil.which` end-to-end so we never invoke
the real `claude` CLI.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ephor import summarizer as summarizer_module
from ephor.summarizer import (
    MAX_LENGTH,
    _extract_messages,
    _extract_text,
    _format_for_prompt,
    summarize_transcript,
)

# ---- _extract_text ---------------------------------------------------------


def test_extract_text_from_string() -> None:
    assert _extract_text("hello world") == "hello world"


def test_extract_text_from_block_list_keeps_only_text() -> None:
    content = [
        {"type": "text", "text": "first"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
        {"type": "text", "text": "second"},
    ]
    assert _extract_text(content) == "first\nsecond"


def test_extract_text_returns_empty_for_garbage() -> None:
    assert _extract_text(None) == ""
    assert _extract_text(42) == ""  # type: ignore[arg-type]
    assert _extract_text([{"no": "type"}]) == ""


# ---- _extract_messages -----------------------------------------------------


def _write_jsonl(path: Path, *entries: dict[str, Any]) -> None:
    with path.open("w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def test_extract_messages_drops_non_role_entries(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    _write_jsonl(
        p,
        {"type": "summary", "summary": "ignore me"},
        {"message": {"role": "user", "content": "hi"}},
        {"message": {"role": "assistant", "content": "hello"}},
    )
    msgs = _extract_messages(p)
    assert msgs == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_extract_messages_handles_missing_file(tmp_path: Path) -> None:
    assert _extract_messages(tmp_path / "nope.jsonl") == []


def test_extract_messages_skips_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    p.write_text("not-json\n" + json.dumps({"message": {"role": "user", "content": "good"}}) + "\n")
    msgs = _extract_messages(p)
    assert msgs == [{"role": "user", "content": "good"}]


def test_format_for_prompt_renders_role_prefixed_text() -> None:
    out = _format_for_prompt(
        [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
        ]
    )
    assert "USER: first" in out
    assert "ASSISTANT: ok" in out


# ---- summarize_transcript via `claude -p` -----------------------------------


def _stub_claude_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend `claude` is on PATH."""
    monkeypatch.setattr(summarizer_module, "_claude_binary", lambda: "/usr/bin/claude")


def _stub_subprocess_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    raises: BaseException | None = None,
) -> list[dict[str, Any]]:
    """Replace subprocess.run with a fake. Returns a list captured by each call
    so tests can assert on the args/env."""
    captured: list[dict[str, Any]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.append({"args": args, "kwargs": kwargs})
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(
            args=args, returncode=returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


def test_summarize_returns_empty_when_claude_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(summarizer_module, "_claude_binary", lambda: None)
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, {"message": {"role": "user", "content": "hi"}})
    assert summarize_transcript(p) == ""


def test_summarize_returns_empty_when_no_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_claude_binary(monkeypatch)
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert summarize_transcript(p) == ""


def test_summarize_returns_parsed_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_claude_binary(monkeypatch)
    captured = _stub_subprocess_run(
        monkeypatch,
        stdout=json.dumps({"result": "Refactoring auth middleware"}),
    )
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, {"message": {"role": "user", "content": "do the thing"}})

    assert summarize_transcript(p) == "Refactoring auth middleware"
    assert len(captured) == 1
    args = captured[0]["args"]
    assert args[0] == "/usr/bin/claude"
    assert "-p" in args
    assert "--append-system-prompt" in args
    assert "--output-format" in args
    assert args[args.index("--output-format") + 1] == "json"


def test_summarize_passes_transcript_text_via_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_claude_binary(monkeypatch)
    captured = _stub_subprocess_run(
        monkeypatch,
        stdout=json.dumps({"result": "ok"}),
    )
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, {"message": {"role": "user", "content": "lookup the bug"}})

    summarize_transcript(p)
    stdin = captured[0]["kwargs"].get("input")
    assert stdin is not None
    assert "lookup the bug" in stdin
    assert "USER:" in stdin


def test_summarize_sets_cco_internal_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without EPHOR_INTERNAL=1, the ephor hook handler would create a ghost
    session for our summarizer subprocess."""
    _stub_claude_binary(monkeypatch)
    captured = _stub_subprocess_run(monkeypatch, stdout=json.dumps({"result": "ok"}))
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, {"message": {"role": "user", "content": "?"}})

    summarize_transcript(p)
    env = captured[0]["kwargs"].get("env") or {}
    assert env.get("EPHOR_INTERNAL") == "1"


def test_summarize_strips_quotes_and_trailing_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_claude_binary(monkeypatch)
    _stub_subprocess_run(monkeypatch, stdout=json.dumps({"result": '"Doing the thing."'}))
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, {"message": {"role": "user", "content": "?"}})
    assert summarize_transcript(p) == "Doing the thing"


def test_summarize_truncates_overly_long_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_claude_binary(monkeypatch)
    _stub_subprocess_run(monkeypatch, stdout=json.dumps({"result": "x" * 200}))
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, {"message": {"role": "user", "content": "?"}})
    out = summarize_transcript(p)
    assert len(out) == MAX_LENGTH
    assert out.endswith("…")


def test_summarize_returns_empty_on_nonzero_returncode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_claude_binary(monkeypatch)
    _stub_subprocess_run(monkeypatch, returncode=1, stderr="auth failed")
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, {"message": {"role": "user", "content": "?"}})
    assert summarize_transcript(p) == ""


def test_summarize_returns_empty_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_claude_binary(monkeypatch)
    _stub_subprocess_run(
        monkeypatch,
        raises=subprocess.TimeoutExpired(cmd="claude", timeout=30),
    )
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, {"message": {"role": "user", "content": "?"}})
    assert summarize_transcript(p) == ""


def test_summarize_returns_empty_on_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_claude_binary(monkeypatch)
    _stub_subprocess_run(monkeypatch, raises=OSError("fork: out of memory"))
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, {"message": {"role": "user", "content": "?"}})
    assert summarize_transcript(p) == ""


def test_summarize_returns_empty_on_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_claude_binary(monkeypatch)
    _stub_subprocess_run(monkeypatch, stdout="not json at all")
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, {"message": {"role": "user", "content": "?"}})
    assert summarize_transcript(p) == ""


def test_summarize_returns_empty_when_result_field_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_claude_binary(monkeypatch)
    _stub_subprocess_run(monkeypatch, stdout=json.dumps({"session_id": "abc", "no_result": True}))
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, {"message": {"role": "user", "content": "?"}})
    assert summarize_transcript(p) == ""


# ---- Jira ticket prefix ---------------------------------------------------


def test_summarize_prefixes_jira_ticket_from_cwd_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When cwd's path contains a Jira key, the summary is prefixed."""
    _stub_claude_binary(monkeypatch)
    _stub_subprocess_run(
        monkeypatch,
        stdout=json.dumps({"result": "Refactoring auth middleware"}),
    )
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, {"message": {"role": "user", "content": "x"}})

    # Make a worktree-shaped path that doesn't have git but does have a key.
    worktree = tmp_path / "DR-9999"
    worktree.mkdir()

    assert summarize_transcript(p, cwd=worktree) == "DR-9999: Refactoring auth middleware"


def test_summarize_without_cwd_does_not_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_claude_binary(monkeypatch)
    _stub_subprocess_run(
        monkeypatch,
        stdout=json.dumps({"result": "Doing stuff"}),
    )
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, {"message": {"role": "user", "content": "x"}})
    assert summarize_transcript(p) == "Doing stuff"


def test_summarize_does_not_double_prefix_when_model_emits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the model invented its own DR-X: prefix, we must not double it."""
    _stub_claude_binary(monkeypatch)
    _stub_subprocess_run(
        monkeypatch,
        stdout=json.dumps({"result": "DR-1234: Wiring up the new column"}),
    )
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, {"message": {"role": "user", "content": "x"}})

    worktree = tmp_path / "DR-1234"
    worktree.mkdir()

    out = summarize_transcript(p, cwd=worktree)
    assert out == "DR-1234: Wiring up the new column"


def test_summarize_truncates_to_fit_with_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The total length stays bounded — the ticket prefix eats into MAX_LENGTH."""
    _stub_claude_binary(monkeypatch)
    long_result = "x" * (MAX_LENGTH + 30)
    _stub_subprocess_run(
        monkeypatch,
        stdout=json.dumps({"result": long_result}),
    )
    p = tmp_path / "t.jsonl"
    _write_jsonl(p, {"message": {"role": "user", "content": "x"}})

    worktree = tmp_path / "DR-42"
    worktree.mkdir()

    out = summarize_transcript(p, cwd=worktree)
    assert out.startswith("DR-42: ")
    assert out.endswith("…")
    assert len(out) <= MAX_LENGTH + len("DR-42: ")

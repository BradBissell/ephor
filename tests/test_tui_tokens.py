"""Tests for the per-session token tracker.

Mocks ~/.claude/projects/<encoded>/<sid>.jsonl to avoid touching the user's
real transcripts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ephor.constants import AgentStatus
from ephor.state.models import AgentState
from ephor.tui import tokens as tokens_module
from ephor.tui.tokens import (
    TokenTracker,
    _sum_tokens_in_file,
    format_tokens,
    transcript_path,
)


def _agent(sid: str, cwd: str) -> AgentState:
    return AgentState(
        session_id=sid,
        cwd=cwd,
        started_at="2026-04-29T10:00:00Z",
        status=AgentStatus.IDLE,
    )


def _write_jsonl(path: Path, *messages: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for m in messages:
            fh.write(json.dumps(m) + "\n")


# ---- transcript_path -------------------------------------------------------


def test_transcript_path_encodes_cwd() -> None:
    p = transcript_path("/home/alice/work", "abc")
    assert p.name == "abc.jsonl"
    assert p.parent.name == "-home-alice-work"


def test_transcript_path_prefers_direct_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tokens_module, "_TRANSCRIPTS_ROOT", tmp_path)
    sid = "5f3e-abc"
    direct = tmp_path / "-home-alice-work" / f"{sid}.jsonl"
    direct.parent.mkdir(parents=True)
    direct.write_text("{}\n")
    assert transcript_path("/home/alice/work", sid) == direct


def test_transcript_path_falls_back_across_renamed_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session whose cwd was renamed after start still resolves by session id."""
    monkeypatch.setattr(tokens_module, "_TRANSCRIPTS_ROOT", tmp_path)
    sid = "2ba5a05c-dead-beef"
    # Transcript sits under the OLD encoded dir…
    old = tmp_path / "-home-brad-projects-claude-orchestrator" / f"{sid}.jsonl"
    old.parent.mkdir(parents=True)
    old.write_text("{}\n")
    # …but the session's cwd is now the renamed dir (direct path won't exist).
    resolved = transcript_path("/home/brad/projects/ephor", sid)
    assert resolved == old


def test_transcript_path_unsafe_sid_no_glob_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crafted session id with a path separator must not trigger a search."""
    monkeypatch.setattr(tokens_module, "_TRANSCRIPTS_ROOT", tmp_path)
    resolved = transcript_path("/x", "../../etc/passwd")
    assert resolved == tmp_path / "-x" / "../../etc/passwd.jsonl"  # direct, unresolved


# ---- _sum_tokens_in_file ---------------------------------------------------


def test_sum_tokens_handles_missing_file(tmp_path: Path) -> None:
    assert _sum_tokens_in_file(tmp_path / "nope.jsonl") == 0


def test_sum_tokens_skips_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    p.write_text(
        "not-json\n"
        + json.dumps({"type": "user", "message": "no usage here"})
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 100,
                        "cache_creation_input_tokens": 200,
                        "cache_read_input_tokens": 50,
                        "output_tokens": 30,
                    }
                },
            }
        )
        + "\n"
    )
    # 100 + 200 + 50 + 30 = 380
    assert _sum_tokens_in_file(p) == 380


def test_sum_tokens_accumulates_across_messages(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    _write_jsonl(
        p,
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 5,
                }
            },
        },
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 20,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 7,
                }
            },
        },
    )
    assert _sum_tokens_in_file(p) == 42


# ---- TokenTracker caching --------------------------------------------------


def test_tracker_returns_zero_when_transcript_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tokens_module, "_TRANSCRIPTS_ROOT", tmp_path)
    tracker = TokenTracker()
    assert tracker.total_for(_agent("nope", "/x/y")) == 0


def test_tracker_caches_until_mtime_or_size_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tokens_module, "_TRANSCRIPTS_ROOT", tmp_path)
    agent = _agent("sid-1", "/h/proj")
    path = transcript_path(agent.cwd, agent.session_id)
    _write_jsonl(
        path,
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 5,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 5,
                }
            },
        },
    )
    tracker = TokenTracker()
    assert tracker.total_for(agent) == 10

    # Stub the parser so we can detect cache hits.
    calls = {"n": 0}
    real = tokens_module._sum_tokens_in_file

    def counting(p: Path) -> int:
        calls["n"] += 1
        return real(p)

    monkeypatch.setattr(tokens_module, "_sum_tokens_in_file", counting)

    # Same mtime + size → cache hit, parser not called.
    tracker.total_for(agent)
    tracker.total_for(agent)
    assert calls["n"] == 0

    # Append a new message → size + mtime change → cache miss → parser called.
    with path.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": 1,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "output_tokens": 1,
                        }
                    },
                }
            )
            + "\n"
        )
    assert tracker.total_for(agent) == 12
    assert calls["n"] == 1


def test_total_across_sums_all_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens_module, "_TRANSCRIPTS_ROOT", tmp_path)
    a = _agent("a", "/h/x")
    b = _agent("b", "/h/y")
    for agent, count in ((a, 100), (b, 250)):
        _write_jsonl(
            transcript_path(agent.cwd, agent.session_id),
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": count,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 0,
                    }
                },
            },
        )
    tracker = TokenTracker()
    assert tracker.total_across([a, b]) == 350


# ---- format_tokens ---------------------------------------------------------


def test_format_tokens_under_1k() -> None:
    assert format_tokens(0) == "0"
    assert format_tokens(999) == "999"


def test_format_tokens_thousands() -> None:
    assert format_tokens(1500) == "1.5k"
    assert format_tokens(142_300) == "142.3k"


def test_format_tokens_millions() -> None:
    assert format_tokens(1_500_000) == "1.5M"

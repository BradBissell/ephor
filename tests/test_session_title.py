"""`ephor title` — naming the calling session's dashboard row."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ephor.cli import main
from ephor.session_title import ancestor_pids, normalize_title, resolve_current_session
from ephor.state.models import AgentState
from ephor.summarizer import MAX_LENGTH
from ephor.summary_store import SummaryStore


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sd = tmp_path / "sessions"
    sd.mkdir(parents=True)
    monkeypatch.setenv("EPHOR_STATE_DIR", str(sd))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    return sd


def _agent(sid: str, **kwargs: object) -> AgentState:
    base: dict[str, object] = {
        "session_id": sid,
        "cwd": "/tmp/x",
        "started_at": "2026-04-29T10:00:00Z",
        "last_event_time": "2026-04-29T10:00:00Z",
    }
    base.update(kwargs)
    return AgentState(**base)  # type: ignore[arg-type]


def _write(directory: Path, sid: str, **kwargs: object) -> None:
    (directory / f"{sid}.json").write_text(_agent(sid, **kwargs).to_json())


def _summary(sid: str) -> str | None:
    return SummaryStore().get(sid)


def test_ancestor_pids_starts_with_parent() -> None:
    ancestors = ancestor_pids()
    assert ancestors[0] == os.getppid()
    assert 1 not in ancestors


def test_ancestor_pids_of_unknown_pid_is_empty() -> None:
    assert ancestor_pids(2**31 - 1) == []


def test_resolve_prefers_ancestor_agent_pid_over_env() -> None:
    agents = [_agent("other", agent_pid=111), _agent("mine", agent_pid=222)]
    env = {"CLAUDE_CODE_SESSION_ID": "other", "TMUX_PANE": "%1"}
    found = resolve_current_session(agents, ancestors=[50, 222, 7], env=env)
    assert found is not None
    assert found.session_id == "mine"


def test_resolve_takes_the_first_listed_match() -> None:
    # scan() lists most-recently-active first, so a resumed session beats its stale sibling.
    agents = [_agent("resumed", agent_pid=222), _agent("stale", agent_pid=222)]
    found = resolve_current_session(agents, ancestors=[222], env={})
    assert found is not None
    assert found.session_id == "resumed"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"CLAUDE_CODE_SESSION_ID": "b"}, "b"),
        ({"TMUX_PANE": "%7"}, "c"),
        ({"CLAUDE_CODE_SESSION_ID": "missing", "TMUX_PANE": "%7"}, "c"),
        ({}, None),
    ],
)
def test_resolve_falls_back_to_claude_env_then_tmux_pane(
    env: dict[str, str], expected: str | None
) -> None:
    agents = [_agent("a", agent_pid=1), _agent("b"), _agent("c", tmux_pane="%7")]
    found = resolve_current_session(agents, ancestors=[999], env=env)
    assert (found.session_id if found else None) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  DR-1234:\tfix   cart\nrounding  ", "DR-1234: fix cart rounding"),
        ("", ""),
        ("x" * 200, "x" * MAX_LENGTH),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


def test_cli_title_names_the_calling_session(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(state_dir, "abc12345-mine", agent_pid=os.getppid())
    _write(state_dir, "def67890-other", agent_pid=2**31 - 1)

    rc = main(["title", "DR-1234:", "fix", "cart", "rounding"])

    assert rc == 0
    assert _summary("abc12345-mine") == "DR-1234: fix cart rounding"
    assert _summary("def67890-other") is None
    assert 'abc12345 → "DR-1234: fix cart rounding"' in capsys.readouterr().out


def test_cli_title_writes_the_summary_sidecar_shape(state_dir: Path) -> None:
    _write(state_dir, "abc12345", agent_pid=os.getppid())

    main(["title", "Review PR stack"])

    path = state_dir.parent / "summaries" / "abc12345.json"
    payload = json.loads(path.read_text())
    assert payload["summary"] == "Review PR stack"
    assert payload["generated_at"].endswith("Z")
    assert path.stat().st_mode & 0o777 == 0o600


def test_cli_title_with_session_prefix(state_dir: Path) -> None:
    _write(state_dir, "abc12345")
    rc = main(["title", "--session", "abc", "Named by hand"])
    assert rc == 0
    assert _summary("abc12345") == "Named by hand"


@pytest.mark.parametrize("prefix", ["zzz", "ab"])
def test_cli_title_rejects_unknown_or_ambiguous_prefix(
    state_dir: Path, prefix: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(state_dir, "abc12345")
    _write(state_dir, "abd67890")
    rc = main(["title", "--session", prefix, "x"])
    assert rc == 1
    assert prefix in capsys.readouterr().err


def test_cli_title_clear_removes_the_title(state_dir: Path) -> None:
    _write(state_dir, "abc12345", agent_pid=os.getppid())
    SummaryStore().set("abc12345", "old title")

    rc = main(["title", "--clear"])

    assert rc == 0
    assert _summary("abc12345") is None


def test_cli_title_without_text_is_a_usage_error(state_dir: Path) -> None:
    _write(state_dir, "abc12345", agent_pid=os.getppid())
    assert main(["title", "   "]) == 2
    assert _summary("abc12345") is None


def test_cli_title_outside_any_session_fails(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(state_dir, "abc12345", agent_pid=2**31 - 1)
    rc = main(["title", "x"])
    assert rc == 1
    assert "--session" in capsys.readouterr().err

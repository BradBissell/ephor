"""Flow runners: the command wrapper, the env a flow gets, and tmux pane listing.

The wrapper is exercised with a real ``sh`` (never tmux): it is the one piece
of shell the tick depends on to learn how a command ended.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ephor import board
from ephor.board import FlowRule, FlowSettings, Trigger
from ephor.flows import runners, runs
from ephor.flows.runs import Run, RunState
from ephor.launcher import StartResult

RUN = Run(
    run_id="DR-1.notify.10-1",
    flow="notify",
    ticket="DR-1",
    trigger="on_enter",
    generation="10-1",
    state=RunState.CLAIMED,
    created_at="",
    updated_at="",
    kind="command",
    title="Fix the thing; rm -rf / $(whoami)",
    from_status="In Progress",
    to_status="Peer Review",
    status_id="10",
    seq=1,
)


def _wrapper_exit(command: str, tmp_path: Path) -> int | None:
    """Run the wrapper exactly as the window would, minus tmux."""
    runs.runs_dir().mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "EPHOR_FLOW_COMMAND": command}
    subprocess.run(
        ["/bin/sh", "-c", runners.command_wrapper(RUN.run_id)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return runs.read_exit(RUN.run_id)


@pytest.mark.parametrize(
    ("command", "code"),
    [
        ("true", 0),
        ("false", 1),
        ("exit 3", 3),  # an exit in the command cannot skip the recording line
        ("true # trailing comment", 0),
        ("sh -c 'exit 7'", 7),
    ],
)
def test_the_wrapper_records_the_exit_status(command: str, code: int, tmp_path: Path) -> None:
    assert _wrapper_exit(command, tmp_path) == code
    assert not any(p.name.startswith(".tmp.") for p in runs.runs_dir().iterdir())


def _fake_window(
    calls: list[dict[str, Any]], result: tuple[str | None, str, str] = ("@1", "%1", "ok")
):
    def open_window(
        name: str,
        cwd: Path,
        command: str,
        env: dict[str, str],
        /,
        *,
        detached: bool,
        target_session: str,
    ) -> tuple[str | None, str, str]:
        calls.append(
            {
                "name": name,
                "cwd": cwd,
                "command": command,
                "env": env,
                "detached": detached,
                "target_session": target_session,
            }
        )
        return result

    return open_window


def _command_rule(command: str = "notify.sh {ticket} {title}") -> FlowRule:
    config = board.parse(
        {
            "board": {"projects": ["DR"]},
            "flow": [{"name": "notify", "on_enter": "Peer Review", "command": command}],
        }
    )
    assert config.errors == ()
    return config.rules[0]


def test_a_command_gets_quoted_values_and_the_safe_env(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    outcome = runners.launch(
        RUN,
        _command_rule(),
        repo=None,
        repo_is_worktree=False,
        payload=tmp_path / "p.json",
        settings=FlowSettings(tmux_session="work"),
        launchers=runners.Launchers(open_window=_fake_window(calls)),
    )
    assert outcome.ok and (outcome.window, outcome.pane) == ("@1", "%1")
    (call,) = calls
    assert call["cwd"] == Path.home()
    assert call["detached"] is True and call["target_session"] == "work"
    assert call["name"] == "flow:DR-1:notify"
    env = call["env"]
    # The title arrives as one shell word, never as shell syntax.
    assert shlex.split(env["EPHOR_FLOW_COMMAND"]) == ["notify.sh", "DR-1", RUN.title]
    assert env["EPHOR_FLOW_TITLE"] == RUN.title
    assert env["EPHOR_FLOW_STATUS"] == "Peer Review"
    assert env["EPHOR_FLOW_PAYLOAD"] == str(tmp_path / "p.json")
    assert env["EPHOR_FLOW_RUN"] == RUN.run_id
    assert env["EPHOR_TICKET"] == "DR-1"
    assert call["command"] == f"sh -c {shlex.quote(runners.command_wrapper(RUN.run_id))}"


def test_a_command_window_failure_is_an_outcome(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    outcome = runners.launch(
        RUN,
        _command_rule(),
        repo=tmp_path,
        repo_is_worktree=False,
        payload=tmp_path / "p.json",
        settings=FlowSettings(),
        launchers=runners.Launchers(open_window=_fake_window(calls, (None, "", "tmux: no server"))),
    )
    assert not outcome.ok and outcome.message == "tmux: no server" and not outcome.fatal
    assert calls[0]["cwd"] == tmp_path


def test_a_launcher_that_raises_does_not_kill_the_tick(tmp_path: Path) -> None:
    def boom(*_: object, **__: object) -> tuple[str | None, str, str]:
        raise OSError("tmux exploded")

    outcome = runners.launch(
        RUN,
        _command_rule(),
        repo=None,
        repo_is_worktree=False,
        payload=tmp_path / "p.json",
        settings=FlowSettings(),
        launchers=runners.Launchers(open_window=boom),
    )
    assert not outcome.ok and "tmux exploded" in outcome.message


def test_an_agent_prompt_that_would_read_as_an_option_fails_the_run(tmp_path: Path) -> None:
    # Validation forbids this template; the runner checks the rendered prompt anyway.
    rule = FlowRule(name="review", trigger=Trigger.ON_ENTER, value="Peer Review", start="{title}")
    run = Run(**{**RUN.__dict__, "kind": "start", "title": "  --dangerously-skip-permissions"})
    calls: list[dict[str, Any]] = []
    outcome = runners.launch(
        run,
        rule,
        repo=tmp_path,
        repo_is_worktree=False,
        payload=tmp_path / "p.json",
        settings=FlowSettings(),
        launchers=runners.Launchers(open_window=_fake_window(calls)),
    )
    assert outcome.fatal and not outcome.ok
    assert calls == []


def test_a_worktree_agent_flow_passes_the_flow_env_to_the_launcher(tmp_path: Path) -> None:
    rule = FlowRule(
        name="impl",
        trigger=Trigger.ON_ENTER,
        value="In Progress",
        start="/implement {ticket}",
        worktree=True,
    )
    seen: dict[str, Any] = {}

    def start(
        ticket: str,
        /,
        *,
        title: str,
        provider: str,
        cwd: Path,
        prompt: str,
        window_name: str,
        detached: bool,
        target_session: str,
        extra_env: dict[str, str] | None,
    ) -> StartResult:
        seen.update(
            ticket=ticket,
            title=title,
            provider=provider,
            cwd=cwd,
            prompt=prompt,
            window_name=window_name,
            detached=detached,
            target_session=target_session,
            extra_env=extra_env,
        )
        return StartResult(True, "ok", window="@3", pane="%3")

    outcome = runners.launch(
        RUN,
        rule,
        repo=tmp_path,
        repo_is_worktree=False,
        payload=tmp_path / "p.json",
        settings=FlowSettings(),
        launchers=runners.Launchers(start=start),
    )
    assert outcome.ok and outcome.pane == "%3"
    assert seen["extra_env"] == {
        "EPHOR_FLOW_RUN": RUN.run_id,
        "EPHOR_FLOW_PAYLOAD": str(tmp_path / "p.json"),
        "EPHOR_TICKET": "DR-1",
    }
    assert seen["prompt"] == "/implement DR-1"
    assert seen["title"] == RUN.title


def test_live_panes(monkeypatch: pytest.MonkeyPatch) -> None:
    def ok(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [], 0, stdout="41.1700000000\t%1\n41.1700000000\t%22\n\n", stderr=""
        )

    def no_server(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [], 1, stdout="", stderr="no server running on /tmp/tmux-1000/default"
        )

    def no_socket(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [], 1, stdout="", stderr="error connecting to /tmp/tmux-1000/default"
        )

    def broken(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, stdout="", stderr="lost server")

    def missing(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(subprocess, "run", ok)
    live = runners.live_panes()
    assert live == runners.LivePanes("41.1700000000", frozenset({"%1", "%22"}))
    assert live.has("%1") and live.has("%1", "41.1700000000")
    # Same pane id on a different server: a pane that only shares the number.
    assert not live.has("%1", "7.1600000000")
    assert not live.has("%5")
    # No server is an answer: nothing is running.
    monkeypatch.setattr(subprocess, "run", no_server)
    assert runners.live_panes() == runners.LivePanes("", frozenset())
    monkeypatch.setattr(subprocess, "run", no_socket)
    assert runners.live_panes() == runners.LivePanes("", frozenset())
    # Anything else is unknown.
    monkeypatch.setattr(subprocess, "run", broken)
    assert runners.live_panes() is None
    monkeypatch.setattr(subprocess, "run", missing)
    assert runners.live_panes() is None


# What tmux 3.4 prints for #{pane_start_command}: the line in double quotes,
# inner double quotes escaped.
_FLOW_PANE = (
    '"exec env EPHOR_FLOW_PAYLOAD=/p.json EPHOR_FLOW_RUN=DR-1.notify.10-1 '
    "EPHOR_FLOW_TITLE='say \\\"hi\\\" EPHOR_FLOW_RUN=DR-9.x.1-0' EPHOR_TICKET=DR-1 "
    'sh -c \'sh -c \\"$EPHOR_FLOW_COMMAND\\"\'"'
)


def test_flow_run_of_reads_only_the_env_assignments() -> None:
    assert runners.flow_run_of(_FLOW_PANE) == "DR-1.notify.10-1"
    # Unquoted, as an older tmux prints it.
    assert runners.flow_run_of("exec env A=1 EPHOR_FLOW_RUN=DR-2.r.3-0 claude") == "DR-2.r.3-0"
    # A mention in a quoted value, or after the command, is not the run.
    assert runners.flow_run_of("exec env EPHOR_FLOW_TITLE='EPHOR_FLOW_RUN=DR-9.x.1-0' sh") == ""
    assert runners.flow_run_of("exec env A=1 sh EPHOR_FLOW_RUN=DR-9.x.1-0") == ""
    assert runners.flow_run_of('"sleep 60"') == ""
    assert runners.flow_run_of("exec env EPHOR_FLOW_RUN=not-a-run sh") == ""
    assert runners.flow_run_of("exec env EPHOR_FLOW_RUN='unterminated") == ""
    assert runners.flow_run_of("") == ""


def test_flow_run_of_round_trips_what_open_window_builds() -> None:
    env = {"EPHOR_FLOW_RUN": "DR-1.review.10-1", "EPHOR_FLOW_TITLE": 'it\'s "odd" $HOME'}
    prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(env.items()))
    line = f"exec env {prefix} claude '/review-pr DR-1'"
    tmux_view = '"' + line.replace("\\", "\\\\").replace('"', '\\"') + '"'
    assert runners.flow_run_of(tmux_view) == "DR-1.review.10-1"


def test_flow_panes(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = f'%1\t"bash"\n%4\t{_FLOW_PANE}\n%5\t{_FLOW_PANE}\n\n'
    seen: list[list[str]] = []

    def ok(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess([], 0, stdout=listing, stderr="")

    def down(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, stdout="", stderr="no server running")

    def slow(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("tmux", 5)

    monkeypatch.setattr(subprocess, "run", ok)
    assert runners.flow_panes() == {"DR-1.notify.10-1": "%4"}
    assert "#{pane_start_command}" in seen[0][-1]
    monkeypatch.setattr(subprocess, "run", down)
    assert runners.flow_panes() is None
    monkeypatch.setattr(subprocess, "run", slow)
    assert runners.flow_panes() is None

"""Launching a flow: a detached tmux window per run, never a child of the tick.

The tick is short-lived — a TUI worker, one pass of ``ephor flows watch``, a
systemd timer — and the thing a flow starts can run for hours. So a flow
never runs *inside* the tick. Each run gets its own tmux window named
``flow:<KEY>:<flow>``, opened detached (``-d``) so it never takes focus from
whatever pane you are typing in, optionally in the session ``[flows]
tmux_session`` names. The window's pane id goes on the run record, and later
ticks look at that pane, and at the exit file, to learn how the run ended.

Two kinds of flow:

* ``start =`` opens a coding-agent session with a rendered prompt
  (``/review-pr DR-8222``). It reuses :mod:`ephor.launcher` rather than
  duplicating it: ``launcher.start`` when the rule wants a fresh worktree,
  else an agent window (``launcher.agent_invocation`` + ``open_window``) in
  the ticket's existing worktree or the project's repo root.
* ``command =`` runs any executable — a shell script, a LangGraph program,
  anything — under a small ``sh`` wrapper that records its exit status in
  ``runs/<run_id>.exit``.

Every flow is told about its event the same way: ``EPHOR_FLOW_RUN`` (the run
id), ``EPHOR_FLOW_PAYLOAD`` (a 0600 JSON file with the ticket, title,
statuses and repo) and ``EPHOR_TICKET``. A ``command =`` flow also gets
``EPHOR_FLOW_TITLE`` and ``EPHOR_FLOW_STATUS``, which are the safe way to use
ticket text inside a quoted string.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ephor import eventlog, launcher
from ephor.board import FlowRule, FlowSettings
from ephor.flows.runs import Run, exit_path, exit_tmp_path, valid_run_id
from ephor.launcher import StartResult

_TMUX_TIMEOUT_SEC = 5.0
_ENV_WORD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class StartFn(Protocol):
    """:func:`ephor.launcher.start`, as far as a runner calls it."""

    def __call__(
        self,
        ticket_raw: str,
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
    ) -> StartResult: ...


class OpenWindowFn(Protocol):
    """:func:`ephor.launcher.open_window`, as far as a runner calls it."""

    def __call__(
        self,
        name: str,
        cwd: Path,
        command: str,
        env: dict[str, str],
        /,
        *,
        detached: bool,
        target_session: str,
    ) -> tuple[str | None, str, str]: ...


# The defaults look the launcher up at call time, so a test that patches
# ``launcher.open_window`` reaches a tick built with default Launchers. Their
# parameters are spelled out so mypy checks both the call sites here and the
# launcher's real signatures.


def _start(
    ticket_raw: str,
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
    return launcher.start(
        ticket_raw,
        title=title,
        provider=provider,
        cwd=cwd,
        prompt=prompt,
        window_name=window_name,
        detached=detached,
        target_session=target_session,
        extra_env=extra_env,
    )


def _open_window(
    name: str,
    cwd: Path,
    command: str,
    env: dict[str, str],
    /,
    *,
    detached: bool,
    target_session: str,
) -> tuple[str | None, str, str]:
    return launcher.open_window(
        name, cwd, command, env, detached=detached, target_session=target_session
    )


@dataclass(frozen=True)
class Launchers:
    """The two launcher entry points a runner uses, injectable for tests."""

    start: StartFn = field(default=_start)
    open_window: OpenWindowFn = field(default=_open_window)


@dataclass(frozen=True)
class LaunchOutcome:
    ok: bool
    message: str
    window: str = ""
    pane: str = ""
    # True when retrying cannot help (the rendered prompt is unsafe): the run
    # fails at once instead of burning its attempts.
    fatal: bool = False


def window_name(ticket: str, flow: str) -> str:
    return f"flow:{ticket}:{flow}"


def flow_env(run: Run, payload: Path) -> dict[str, str]:
    """What every flow is told about the run that started it."""
    return {
        "EPHOR_FLOW_RUN": run.run_id,
        "EPHOR_FLOW_PAYLOAD": str(payload),
        "EPHOR_TICKET": run.ticket,
    }


def launch(
    run: Run,
    rule: FlowRule,
    *,
    repo: Path | None,
    repo_is_worktree: bool,
    payload: Path,
    settings: FlowSettings,
    launchers: Launchers | None = None,
) -> LaunchOutcome:
    """Open ``run``'s window. Never raises; a failure is an outcome."""
    tools = launchers or Launchers()
    try:
        if rule.launches_agent:
            return _launch_agent(
                run,
                rule,
                repo=repo,
                repo_is_worktree=repo_is_worktree,
                payload=payload,
                settings=settings,
                tools=tools,
            )
        return _launch_command(
            run, rule, repo=repo, payload=payload, settings=settings, tools=tools
        )
    except Exception as exc:  # noqa: BLE001 - a launch failure must not kill the tick
        return LaunchOutcome(False, f"launch raised {type(exc).__name__}: {exc}")


def _launch_agent(
    run: Run,
    rule: FlowRule,
    *,
    repo: Path | None,
    repo_is_worktree: bool,
    payload: Path,
    settings: FlowSettings,
    tools: Launchers,
) -> LaunchOutcome:
    prompt = rule.render(ticket=run.ticket, title=run.title, status=run.matched_status)
    if prompt.lstrip().startswith("-"):
        # Validation makes a start= template begin with literal text, so this
        # is belt and braces — but a prompt that reaches the agent CLI as an
        # option is exactly the hole that check exists to close.
        return LaunchOutcome(False, "rendered prompt begins with '-'", fatal=True)
    if repo is None:
        return LaunchOutcome(False, "no repo")
    name = window_name(run.ticket, run.flow)
    extra = flow_env(run, payload)

    if rule.worktree and not repo_is_worktree:
        result = tools.start(
            run.ticket,
            title=run.title,
            provider=rule.provider,
            cwd=repo,
            prompt=prompt,
            window_name=name,
            detached=True,
            target_session=settings.tmux_session,
            extra_env=extra,
        )
        if not result.ok:
            return LaunchOutcome(False, result.message)
        return LaunchOutcome(True, result.message, window=result.window, pane=result.pane)

    # The ticket's own worktree (it already exists, so there is nothing for
    # launcher.start to create), or a repo root for a worktree = false rule.
    command, env = launcher.agent_invocation(rule.provider, ticket=run.ticket, prompt=prompt)
    window, pane, message = tools.open_window(
        name,
        repo,
        command,
        {**extra, **env},
        detached=True,
        target_session=settings.tmux_session,
    )
    if window is None:
        return LaunchOutcome(False, message)
    # launcher.start writes this for its own launches. The launch guard reads
    # it, so a second tick in the next ten minutes sees this agent even
    # before its hook has written a state file.
    eventlog.append(
        eventlog.EventKind.SESSION_STARTED,
        ticket=run.ticket,
        detail=f"flow {run.flow} in {repo.name}",
        provider=rule.provider,
        run_id=run.run_id,
    )
    return LaunchOutcome(True, message, window=window, pane=pane)


def command_wrapper(run_id: str) -> str:
    """The ``sh`` line that runs ``$EPHOR_FLOW_COMMAND`` and records its exit status.

    The command runs in its own ``sh -c`` rather than being pasted into the
    wrapper, so an ``exit`` or a trailing ``#`` comment in it cannot skip the
    line that records the status. The status is written to a temporary name
    and renamed into place, so a tick never reads half a number.
    """
    final = exit_path(run_id)
    tmp = exit_tmp_path(run_id)
    return (
        'sh -c "$EPHOR_FLOW_COMMAND"; code=$?; '
        f'printf "%s\\n" "$code" > {shlex.quote(str(tmp))} && '
        f"mv -f {shlex.quote(str(tmp))} {shlex.quote(str(final))}"
    )


def _launch_command(
    run: Run,
    rule: FlowRule,
    *,
    repo: Path | None,
    payload: Path,
    settings: FlowSettings,
    tools: Launchers,
) -> LaunchOutcome:
    rendered = rule.render(ticket=run.ticket, title=run.title, status=run.matched_status)
    env = {
        **flow_env(run, payload),
        "EPHOR_FLOW_TITLE": run.title,
        "EPHOR_FLOW_STATUS": run.matched_status,
        "EPHOR_FLOW_COMMAND": rendered,
    }
    cwd = repo if repo is not None else Path.home()
    window, pane, message = tools.open_window(
        window_name(run.ticket, run.flow),
        cwd,
        f"sh -c {shlex.quote(command_wrapper(run.run_id))}",
        env,
        detached=True,
        target_session=settings.tmux_session,
    )
    if window is None:
        return LaunchOutcome(False, message)
    return LaunchOutcome(True, message, window=window, pane=pane)


def live_panes() -> set[str] | None:
    """Every tmux pane id that exists right now, or None when tmux did not answer.

    None is "unknown", never "every pane is gone": a tick that cannot reach
    the tmux server must not mark every launched run finished.
    """
    try:
        proc = subprocess.run(
            ["/usr/bin/env", "tmux", "list-panes", "-a", "-F", "#{pane_id}"],
            capture_output=True,
            text=True,
            timeout=_TMUX_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def flow_panes() -> dict[str, str] | None:
    """run id -> pane id of every flow window alive right now, or None if tmux did not answer.

    A launch that opened its window but never recorded it (the tick died, or
    the run file could not be written) is found here by the run id in the
    pane's start command, so a retry adopts that window instead of opening a
    second one. None is "unknown": the caller must not read it as "no such
    window" and relaunch.
    """
    try:
        proc = subprocess.run(
            [
                "/usr/bin/env",
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{pane_id}\t#{pane_start_command}",
            ],
            capture_output=True,
            text=True,
            timeout=_TMUX_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    found: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        pane, _, command = line.partition("\t")
        run_id = flow_run_of(command)
        if pane.strip() and run_id:
            found.setdefault(run_id, pane.strip())
    return found


def flow_run_of(start_command: str) -> str:
    """The run id a pane's ``#{pane_start_command}`` was opened for, or "".

    :func:`ephor.launcher.open_window` starts every window as ``exec env
    K=V … command``, and every flow window carries ``EPHOR_FLOW_RUN`` among
    those assignments. tmux prints the line wrapped in double quotes with
    inner ones escaped. Only the assignment prefix is read: a title or a
    command that merely *mentions* ``EPHOR_FLOW_RUN=`` sits inside a quoted
    value, or after it, and is never mistaken for one.
    """
    text = start_command.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = re.sub(r"\\(.)", r"\1", text[1:-1])
    try:
        words = shlex.split(text)
    except ValueError:
        return ""
    if words[:2] != ["exec", "env"]:
        return ""
    for word in words[2:]:
        if not _ENV_WORD_RE.match(word):
            break
        name, _, value = word.partition("=")
        if name == "EPHOR_FLOW_RUN":
            return value if valid_run_id(value) else ""
    return ""

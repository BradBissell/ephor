"""The flows tick: matching, adopt mode, the run state machine, guards and dispatch.

Every call that would leave the process — Jira, tmux, the session scan, the
event log's launch-guard read — goes through :class:`TickDeps`, so these
tests drive the whole algorithm against fakes and a sandboxed state dir.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ephor import board, eventlog, work_items
from ephor.board import BoardConfig
from ephor.filelock import locked
from ephor.flows import runners, runs
from ephor.flows import snapshot as snap
from ephor.flows import tick as flows_tick
from ephor.flows.runs import Run, RunState
from ephor.flows.snapshot import Snapshot, TicketState
from ephor.flows.tick import TickDeps, TickResult, TickStatus
from ephor.github import PullRequest
from ephor.jira_api import Issue
from ephor.launcher import StartResult
from ephor.state.models import AgentState
from ephor.work_items import WorkItem

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
ME = "acct-me"

REVIEW = {"name": "review", "on_enter": "Peer Review", "start": "/review-pr {ticket}"}
NOTIFY = {
    "name": "notify",
    "on_enter": "Peer Review",
    "command": "notify.sh {ticket}",
    "mode": "auto",
}


@pytest.fixture(autouse=True)
def _jira_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPHOR_JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("EPHOR_JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("EPHOR_JIRA_TOKEN", "t")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def _config(
    *rules: dict[str, Any], repos: dict[str, str] | None = None, **flows: Any
) -> BoardConfig:
    data: dict[str, Any] = {
        "board": {"projects": ["DR"], "columns": ["To Do", "In Progress", "Peer Review", "Done"]},
        "flow": list(rules),
    }
    if repos:
        data["repos"] = repos
    if flows:
        data["flows"] = flows
    config = board.parse(data)
    assert config.errors == ()
    return config


class FakeJira:
    def __init__(self) -> None:
        self.issues: dict[str, Issue] = {}
        self.fresh: dict[str, Issue | None] = {}
        self.fail_search = False
        self.account: str | None = ME
        self.myself_calls = 0
        self.fresh_calls: list[str] = []

    def put(
        self, key: str, status: str, status_id: str, *, assignee: str = ME, title: str = "Title"
    ) -> None:
        self.issues[key] = Issue(
            key=key,
            status=status,
            status_id=status_id,
            title=title,
            assignee="Me",
            assignee_id=assignee,
        )

    def search(self, jql: str, fields: tuple[str, ...]) -> list[Issue] | None:
        return None if self.fail_search else list(self.issues.values())

    def myself(self) -> str | None:
        self.myself_calls += 1
        return self.account

    def fetch_fresh(self, key: str) -> Issue | None:
        self.fresh_calls.append(key)
        if key in self.fresh:
            return self.fresh[key]
        return self.issues.get(key)


class FakeLaunch:
    def __init__(self) -> None:
        self.windows: list[dict[str, Any]] = []
        self.starts: list[dict[str, Any]] = []
        self.fail = ""

    def open_window(
        self,
        name: str,
        cwd: Path,
        command: str,
        env: dict[str, str],
        *,
        detached: bool = False,
        target_session: str = "",
    ) -> tuple[str | None, str, str]:
        self.windows.append(
            {
                "name": name,
                "cwd": cwd,
                "command": command,
                "env": env,
                "detached": detached,
                "target_session": target_session,
            }
        )
        if self.fail:
            return None, "", self.fail
        n = len(self.windows)
        return f"@{n}", f"%{n}", f"opened tmux window @{n}"

    def start(
        self,
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
        self.starts.append(
            {
                "ticket": ticket,
                "title": title,
                "provider": provider,
                "cwd": cwd,
                "prompt": prompt,
                "window_name": window_name,
                "detached": detached,
                "target_session": target_session,
                "extra_env": extra_env,
            }
        )
        if self.fail:
            return StartResult(False, self.fail)
        return StartResult(True, "started", window="@90", pane="%90")


class World:
    """Fakes for one test, and a tick that uses them."""

    def __init__(self) -> None:
        self.jira = FakeJira()
        self.launch = FakeLaunch()
        self.sessions: list[AgentState] = []
        self.starts: dict[str, str] = {}
        self.work: dict[str, WorkItem] = {}
        self.panes: set[str] | None = set()
        # The tmux server those panes are on (pid.start_time).
        self.server = "100.1"
        # run id -> pane of the flow windows tmux reports; None = tmux silent.
        self.flow_panes: dict[str, str] | None = {}
        # ticket -> what `gh pr list --search` says; a missing key is "no PRs".
        self.prs: dict[str, list[PullRequest] | None] = {}
        self.pr_calls: list[tuple[Path, str]] = []
        self.dead_pids: set[int] = set()

    def find_prs(self, repo: Path, key: str) -> list[PullRequest] | None:
        self.pr_calls.append((repo, key))
        found = self.prs.get(key, [])
        return None if found is None else list(found)

    def deps(self) -> TickDeps:
        return TickDeps(
            search=self.jira.search,
            myself=self.jira.myself,
            fetch_fresh=self.jira.fetch_fresh,
            sessions=lambda: list(self.sessions),
            ticket_of=lambda agent: agent.ticket.upper(),
            recent_starts=lambda _since: dict(self.starts),
            work_item=self.work.get,
            live_panes=lambda: (
                None
                if self.panes is None
                else runners.LivePanes(self.server, frozenset(self.panes))
            ),
            flow_panes=lambda: None if self.flow_panes is None else dict(self.flow_panes),
            find_prs=self.find_prs,
            pid_alive=lambda pid: pid not in self.dead_pids,
            launchers=runners.Launchers(
                start=self.launch.start, open_window=self.launch.open_window
            ),
        )

    def tick(self, config: BoardConfig, at: datetime, *, dry_run: bool = False) -> TickResult:
        return flows_tick.run(config, dry_run=dry_run, deps=self.deps(), now=at)


@pytest.fixture
def world() -> World:
    return World()


def _baseline(world: World, config: BoardConfig, at: datetime = T0) -> None:
    """A first tick (adopts) with DR-1 In Progress, so the next one can see a move."""
    world.jira.put("DR-1", "In Progress", "3")
    result = world.tick(config, at)
    assert result.ok, result.message


def _move_to_review(world: World, config: BoardConfig, at: datetime) -> TickResult:
    world.jira.put("DR-1", "Peer Review", "10")
    result = world.tick(config, at)
    assert result.ok, result.message
    return result


def _only_run() -> Run:
    found = runs.list_runs()
    assert len(found) == 1, [r.run_id for r in found]
    return found[0]


def _events(kind: eventlog.EventKind) -> list[eventlog.Event]:
    return [e for e in eventlog.read() if e.kind is kind]


# --- adopt mode, matching, dedupe ------------------------------------------------------


def test_the_first_tick_adopts_and_fires_nothing(world: World) -> None:
    config = _config(REVIEW)
    world.jira.put("DR-1", "Peer Review", "10")
    result = world.tick(config, T0)
    assert result.ok
    assert result.adopted == ("review",)
    assert runs.list_runs() == []
    stored = snap.load()
    assert stored is not None
    assert stored.rules_seen == (config.rules[0].fingerprint,)
    assert stored.myself == ME


def test_a_move_into_the_status_creates_an_awaiting_run(world: World) -> None:
    config = _config(REVIEW)
    _baseline(world, config)
    result = _move_to_review(world, config, T0 + timedelta(minutes=1))
    run = _only_run()
    assert run.run_id == "DR-1.review.10-1"
    assert run.state is RunState.AWAITING_CONFIRM
    assert not run.stale
    assert (run.from_status, run.to_status, run.status_id, run.seq) == (
        "In Progress",
        "Peer Review",
        "10",
        1,
    )
    assert [a.verb for a in result.actions] == ["created"]
    assert world.launch.windows == [] and world.launch.starts == []
    assert stat.S_IMODE(runs.run_path(run.run_id).stat().st_mode) == 0o600


def test_the_same_move_seen_twice_creates_one_run(world: World) -> None:
    config = _config(REVIEW)
    _baseline(world, config)
    before = snap.snapshot_path().read_text()
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    # A tick that died before writing its snapshot: the next one sees the
    # same move again, and its create fails harmlessly.
    snap.snapshot_path().write_text(before)
    result = _move_to_review(world, config, T0 + timedelta(minutes=2))
    assert [r.run_id for r in runs.list_runs()] == ["DR-1.review.10-1"]
    assert [a.verb for a in result.actions if a.verb == "created"] == []


def test_a_ticket_appearing_in_the_status_counts_as_entering_it(world: World) -> None:
    config = _config(REVIEW)
    _baseline(world, config)
    world.jira.put("DR-2", "Peer Review", "10")
    world.tick(config, T0 + timedelta(minutes=1))
    assert [r.run_id for r in runs.list_runs()] == ["DR-2.review.10-0"]


def test_a_rule_added_later_adopts_before_it_fires(world: World) -> None:
    _baseline(world, _config())
    config = _config(REVIEW)
    result = _move_to_review(world, config, T0 + timedelta(minutes=1))
    assert result.adopted == ("review",)
    assert runs.list_runs() == []
    # The next move is fired on.
    world.jira.put("DR-1", "In Progress", "3")
    world.tick(config, T0 + timedelta(minutes=2))
    _move_to_review(world, config, T0 + timedelta(minutes=3))
    assert [r.run_id for r in runs.list_runs()] == ["DR-1.review.10-3"]


def test_editing_what_a_rule_runs_does_not_readopt(world: World) -> None:
    _baseline(world, _config(REVIEW))
    edited = _config({**REVIEW, "start": "/review-pr {ticket} carefully"})
    result = _move_to_review(world, edited, T0 + timedelta(minutes=1))
    assert result.adopted == ()
    assert len(runs.list_runs()) == 1


def test_on_exit_fires_on_leaving_and_never_on_appearing(world: World) -> None:
    config = _config(
        {"name": "left", "on_exit": "In Progress", "command": "x.sh", "mode": "confirm"}
    )
    _baseline(world, config)
    world.jira.put("DR-5", "Peer Review", "10")  # appears: left nothing
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    assert [r.run_id for r in runs.list_runs()] == ["DR-1.left.3-1"]


def test_assignee_me_ignores_someone_elses_ticket(world: World) -> None:
    config = _config(REVIEW)
    _baseline(world, config)
    world.jira.put("DR-1", "Peer Review", "10", assignee="acct-other")
    world.tick(config, T0 + timedelta(minutes=1))
    assert runs.list_runs() == []


def test_assignee_any_fires_for_anyone(world: World) -> None:
    config = _config({**REVIEW, "assignee": "any"})
    _baseline(world, config)
    world.jira.put("DR-1", "Peer Review", "10", assignee="acct-other")
    world.tick(config, T0 + timedelta(minutes=1))
    assert len(runs.list_runs()) == 1


def test_myself_is_cached_in_the_snapshot(world: World) -> None:
    config = _config(REVIEW)
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    assert world.jira.myself_calls == 1


def test_an_unknown_myself_writes_nothing(world: World) -> None:
    config = _config(REVIEW)
    world.jira.account = None
    world.jira.put("DR-1", "In Progress", "3")
    result = world.tick(config, T0)
    assert result.status is TickStatus.MYSELF_FAILED
    assert snap.load() is None


def test_without_assignee_me_rules_myself_is_never_asked(world: World) -> None:
    config = _config({**REVIEW, "assignee": "any"})
    world.jira.account = None
    _baseline(world, config)
    assert world.jira.myself_calls == 0


def test_a_failed_search_writes_nothing(world: World) -> None:
    config = _config(REVIEW)
    _baseline(world, config)
    before = snap.snapshot_path().read_text()
    world.jira.fail_search = True
    result = world.tick(config, T0 + timedelta(minutes=1))
    assert result.status is TickStatus.SEARCH_FAILED
    assert snap.snapshot_path().read_text() == before
    assert runs.list_runs() == []


def test_a_stale_gap_makes_even_auto_runs_wait(world: World, repo: Path) -> None:
    config = _config(NOTIFY, repos={"DR": str(repo)})
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(hours=2))
    run = _only_run()
    assert run.state is RunState.AWAITING_CONFIRM
    assert run.stale
    assert world.launch.windows == []


def test_a_config_with_errors_keeps_the_snapshot_but_dispatches_nothing(world: World) -> None:
    good = _config(REVIEW)
    _baseline(world, good)
    broken = board.parse(
        {
            "board": {
                "projects": ["DR"],
                "columns": ["To Do", "In Progress", "Peer Review", "Done"],
            },
            "flow": [REVIEW, {"name": "bad", "on_enter": "X"}],
        }
    )
    assert broken.lanes_ok and not broken.dispatch_ok
    world.jira.put("DR-1", "Peer Review", "10")
    result = world.tick(broken, T0 + timedelta(minutes=1))
    assert result.ok
    assert "error" in result.dispatch_note
    assert runs.list_runs() == []
    stored = snap.load()
    assert stored is not None
    assert stored.tickets["DR-1"].status == "Peer Review"
    assert stored.rules_seen == (good.rules[0].fingerprint,)


# --- auto dispatch of command flows --------------------------------------------------------


def test_an_auto_command_flow_launches_in_a_detached_window(world: World, repo: Path) -> None:
    config = _config(NOTIFY, repos={"DR": str(repo)}, tmux_session="flows")
    _baseline(world, config)
    result = _move_to_review(world, config, T0 + timedelta(minutes=1))
    run = _only_run()
    assert run.state is RunState.LAUNCHED
    assert (run.window, run.pane, run.attempts) == ("@1", "%1", 1)
    assert [a.verb for a in result.actions] == ["created", "dispatched"]

    (window,) = world.launch.windows
    assert window["name"] == "flow:DR-1:notify"
    assert window["detached"] is True
    assert window["target_session"] == "flows"
    assert window["cwd"] == repo
    env = window["env"]
    assert env["EPHOR_FLOW_RUN"] == run.run_id
    assert env["EPHOR_TICKET"] == "DR-1"
    assert env["EPHOR_FLOW_TITLE"] == "Title"
    assert env["EPHOR_FLOW_STATUS"] == "Peer Review"
    assert env["EPHOR_FLOW_COMMAND"] == "notify.sh DR-1"
    assert str(runs.exit_path(run.run_id)) in window["command"]

    payload = Path(env["EPHOR_FLOW_PAYLOAD"])
    assert run.payload_path == str(payload)
    assert stat.S_IMODE(payload.stat().st_mode) == 0o600
    data = json.loads(payload.read_text())
    assert data["run_id"] == run.run_id
    assert (data["from_status"], data["to_status"], data["status_id"]) == (
        "In Progress",
        "Peer Review",
        "10",
    )
    assert data["repo_path"] == str(repo)
    assert data["assignee"] == "Me"

    (event,) = _events(eventlog.EventKind.FLOW_DISPATCHED)
    assert event.ticket == "DR-1"
    assert event.extra is not None
    assert event.extra["run_id"] == run.run_id
    assert event.extra["generation"] == "10-1"
    assert world.jira.fresh_calls == ["DR-1"]


def test_a_command_flow_without_a_repo_runs_in_home(world: World) -> None:
    config = _config(NOTIFY)
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    assert world.launch.windows[0]["cwd"] == Path.home()
    assert _only_run().state is RunState.LAUNCHED


# --- approve, and start= flows -------------------------------------------------------------


def _approved_review(world: World, config: BoardConfig) -> Run:
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    approved, _ = runs.approve("DR-1.review.10-1")
    assert approved is not None and approved.state is RunState.CLAIMED
    return approved


def test_an_approved_start_flow_opens_an_agent_in_the_repo_root(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    result = world.tick(config, T0 + timedelta(minutes=2))
    assert [a.verb for a in result.actions] == ["dispatched"]
    (window,) = world.launch.windows
    assert window["cwd"] == repo
    assert window["command"] == "claude '/review-pr DR-1'"
    assert window["env"]["EPHOR_TICKET"] == "DR-1"
    assert window["env"]["EPHOR_PROVIDER"] == "claude"
    assert window["env"]["EPHOR_FLOW_RUN"] == "DR-1.review.10-1"
    assert window["detached"] is True
    assert world.launch.starts == []
    assert _only_run().state is RunState.LAUNCHED
    # The launch guard of the next tick must see this agent before its hook does.
    started = _events(eventlog.EventKind.SESSION_STARTED)
    assert [e.ticket for e in started] == ["DR-1"]


def test_a_worktree_flow_uses_the_launcher(world: World, repo: Path) -> None:
    config = _config({**REVIEW, "worktree": True}, repos={"DR": str(repo)}, tmux_session="flows")
    _approved_review(world, config)
    world.tick(config, T0 + timedelta(minutes=2))
    (call,) = world.launch.starts
    assert call["ticket"] == "DR-1"
    assert call["cwd"] == repo
    assert call["prompt"] == "/review-pr DR-1"
    assert call["window_name"] == "flow:DR-1:review"
    assert call["detached"] is True
    assert call["target_session"] == "flows"
    assert call["extra_env"]["EPHOR_FLOW_RUN"] == "DR-1.review.10-1"
    run = _only_run()
    assert (run.state, run.pane) == (RunState.LAUNCHED, "%90")


def test_a_ticket_with_its_own_worktree_opens_there(
    world: World, tmp_path: Path, repo: Path
) -> None:
    tree = tmp_path / "repo-DR-1"
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {repo}/.git/worktrees/repo-DR-1\n")
    world.work["DR-1"] = WorkItem(
        key="DR-1", worktree=str(tree), last_seen="2026-01-01T00:00:00+00:00"
    )
    config = _config({**REVIEW, "worktree": True}, repos={"DR": str(repo)})
    _approved_review(world, config)
    world.tick(config, T0 + timedelta(minutes=2))
    assert world.launch.starts == []
    assert world.launch.windows[0]["cwd"] == tree


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_a_repo_root_session_never_becomes_the_tickets_worktree(
    world: World, tmp_path: Path
) -> None:
    """worktree = false launches in the repo root; a later worktree = true flow still forks."""
    from ephor import jira
    from tests.test_gitinfo import _repo

    root = _repo(tmp_path / "aim")
    # What the review flow's agent looks like to the dashboard: in the repo
    # root, EPHOR_TICKET set — a confident (declared) match, so it is promoted.
    agent = AgentState.from_dict(
        {
            "session_id": "sess-review",
            "cwd": str(root),
            "started_at": "2026-01-01T00:00:00+00:00",
            "status": "IDLE",
            "ticket": "DR-1",
            "schema_version": 4,
        }
    )
    match = jira.match_for_agent(agent)
    assert match is not None and match.key == "DR-1" and match.confident
    jira.promote(match, agent)
    item = work_items.load("DR-1")
    assert item is not None and item.confirmed
    assert (item.worktree, item.branch) == ("", "")
    assert "sess-review" in item.session_ids

    implement = {"name": "impl", "on_enter": "Peer Review", "start": "/impl {ticket}"}
    config = _config({**implement, "worktree": True}, repos={"DR": str(root)})
    world.work["DR-1"] = replace(item, worktree=str(root), last_seen="2026-01-01T00:00:00+00:00")
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    approved, _ = runs.approve("DR-1.impl.10-1")
    assert approved is not None
    world.tick(config, T0 + timedelta(minutes=2))
    # Even with a stale record naming the repo root, the flow goes through
    # launcher.start (a fresh worktree), never straight into the main tree.
    (call,) = world.launch.starts
    assert call["cwd"] == root
    assert world.launch.windows == []


def test_a_start_flow_with_no_repo_is_blocked(world: World) -> None:
    config = _config(REVIEW)
    _approved_review(world, config)
    world.tick(config, T0 + timedelta(minutes=2))
    run = _only_run()
    assert (run.state, run.reason) == (RunState.BLOCKED, "no repo")
    assert world.launch.windows == []


# --- guards ------------------------------------------------------------------------------------


def _agent(sid: str, ticket: str = "", status: str = "IDLE") -> AgentState:
    return AgentState.from_dict(
        {
            "session_id": sid,
            "cwd": "/nowhere",
            "started_at": "2026-01-01T00:00:00+00:00",
            "status": status,
            "ticket": ticket,
            "schema_version": 4,
        }
    )


def test_paused_blocks_before_every_other_guard(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)}, max_active_agents=1)
    _approved_review(world, config)
    world.sessions = [_agent("s1", "DR-1")]  # would also trip two other guards
    runs.pause()
    result = world.tick(config, T0 + timedelta(minutes=2))
    run = _only_run()
    assert (run.state, run.reason) == (RunState.BLOCKED, "paused")
    assert result.dispatch_note.startswith("paused")
    assert world.jira.fresh_calls == []


def test_max_active_agents_counts_every_live_session(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)}, max_active_agents=2)
    _approved_review(world, config)
    world.sessions = [_agent("s1"), _agent("s2"), _agent("s3", status="DEAD")]
    world.tick(config, T0 + timedelta(minutes=2))
    assert _only_run().reason.startswith("max_active_agents: 2")


@pytest.mark.parametrize("how", ["session", "started", "work"])
def test_the_launch_guard_refuses_a_ticket_someone_is_on(
    world: World, repo: Path, how: str
) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    if how == "session":
        world.sessions = [_agent("s1", "DR-1")]
    elif how == "started":
        world.starts = {"DR-1": "2026-10-20T12:01:30+00:00"}
    else:
        world.work["DR-1"] = WorkItem(
            key="DR-1", last_seen=(T0 + timedelta(minutes=-5)).isoformat()
        )
    world.tick(config, T0 + timedelta(minutes=2))
    run = _only_run()
    assert run.state is RunState.BLOCKED
    assert run.reason.startswith("launch guard")
    assert world.launch.windows == []


def test_a_dead_session_on_the_ticket_does_not_block(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    world.sessions = [_agent("s1", "DR-1", status="DEAD")]
    world.tick(config, T0 + timedelta(minutes=2))
    assert _only_run().state is RunState.LAUNCHED


def test_command_flows_skip_the_agent_guards(world: World) -> None:
    config = _config(NOTIFY, max_active_agents=1)
    world.sessions = [_agent("s1", "DR-1")]
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    assert _only_run().state is RunState.LAUNCHED


def test_max_runs_per_hour_caps_launches_across_flows(world: World) -> None:
    config = _config(NOTIFY, max_runs_per_hour=1)
    world.jira.put("DR-1", "In Progress", "3")
    world.jira.put("DR-2", "In Progress", "3")
    world.tick(config, T0)
    world.jira.put("DR-1", "Peer Review", "10")
    world.jira.put("DR-2", "Peer Review", "10")
    world.tick(config, T0 + timedelta(minutes=1))
    states = {r.ticket: (r.state, r.reason) for r in runs.list_runs()}
    assert states["DR-1"][0] is RunState.LAUNCHED
    assert states["DR-2"][0] is RunState.BLOCKED
    assert states["DR-2"][1].startswith("max_runs_per_hour")
    # An hour later the cap has room again, and the blocked run goes.
    world.tick(config, T0 + timedelta(minutes=62))
    assert {r.ticket: r.state for r in runs.list_runs()}["DR-2"] is RunState.LAUNCHED


def test_the_ticket_breaker_stops_a_ticket_bouncing(world: World) -> None:
    config = _config(NOTIFY, ticket_breaker=2)
    world.panes = None  # the windows' fate is not what this is about
    _baseline(world, config)
    at = T0
    for _ in range(3):
        at += timedelta(minutes=1)
        world.jira.put("DR-1", "In Progress", "3")
        world.tick(config, at)
        at += timedelta(minutes=1)
        _move_to_review(world, config, at)
    found = sorted(runs.list_runs(), key=lambda r: r.seq)
    assert [r.state for r in found] == [RunState.LAUNCHED, RunState.LAUNCHED, RunState.BLOCKED]
    assert found[-1].reason.startswith("ticket_breaker")


def test_a_blocked_run_logs_one_event_not_one_per_tick(world: World) -> None:
    config = _config(REVIEW)
    _approved_review(world, config)
    for minute in (2, 3, 4):
        world.tick(config, T0 + timedelta(minutes=minute))
    assert len(_events(eventlog.EventKind.FLOW_BLOCKED)) == 1


def test_a_blocked_run_goes_once_the_guard_clears(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    world.sessions = [_agent("s1", "DR-1")]
    world.tick(config, T0 + timedelta(minutes=2))
    assert _only_run().state is RunState.BLOCKED
    world.sessions = []
    world.tick(config, T0 + timedelta(minutes=3))
    assert _only_run().state is RunState.LAUNCHED


# --- the pre-launch re-check --------------------------------------------------------------


def test_a_ticket_that_already_left_is_superseded_before_launch(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    world.jira.fresh["DR-1"] = Issue(key="DR-1", status="QA", status_id="11", assignee_id=ME)
    world.tick(config, T0 + timedelta(minutes=2))
    run = _only_run()
    assert run.state is RunState.SUPERSEDED
    assert "QA" in run.reason
    assert world.launch.windows == []
    assert len(_events(eventlog.EventKind.FLOW_CANCELLED)) == 1


def test_a_ticket_reassigned_away_is_superseded(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    world.jira.fresh["DR-1"] = Issue(
        key="DR-1", status="Peer Review", status_id="10", assignee_id="x"
    )
    world.tick(config, T0 + timedelta(minutes=2))
    assert _only_run().reason == "ticket is no longer assigned to you"


def test_an_unanswered_recheck_blocks_rather_than_launching(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    world.jira.fresh["DR-1"] = None
    world.tick(config, T0 + timedelta(minutes=2))
    run = _only_run()
    assert (run.state, run.reason) == (RunState.BLOCKED, "Jira re-check did not answer")
    assert world.launch.windows == []


# --- expiry and supersession -----------------------------------------------------------------


def test_an_unconfirmed_run_expires_after_confirm_ttl(world: World) -> None:
    config = _config(REVIEW, confirm_ttl="1h")
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    for minute in range(2, 60, 20):  # stay inside max_event_age
        world.tick(config, T0 + timedelta(minutes=minute))
    assert _only_run().state is RunState.AWAITING_CONFIRM
    world.tick(config, T0 + timedelta(minutes=62))
    run = _only_run()
    assert (run.state, run.reason) == (RunState.EXPIRED, "not confirmed within confirm_ttl")


def test_an_unconfirmed_run_expires_when_its_ticket_moves_on(world: World) -> None:
    config = _config(REVIEW)
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    world.jira.put("DR-1", "Done", "20")
    world.tick(config, T0 + timedelta(minutes=2))
    run = _only_run()
    assert run.state is RunState.EXPIRED
    assert "Done" in run.reason
    # Approve is refused once a tick has expired it.
    approved, message = runs.approve(run.run_id)
    assert approved is None and "expired" in message


def test_a_blocked_run_whose_ticket_moved_is_superseded(world: World) -> None:
    config = _config(REVIEW)
    _approved_review(world, config)
    world.tick(config, T0 + timedelta(minutes=2))
    assert _only_run().state is RunState.BLOCKED
    world.jira.put("DR-1", "In Progress", "3")
    world.tick(config, T0 + timedelta(minutes=3))
    assert _only_run().state is RunState.SUPERSEDED


def test_a_run_for_a_removed_flow_expires(world: World) -> None:
    config = _config(REVIEW)
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    world.tick(_config(), T0 + timedelta(minutes=2))
    assert _only_run().state is RunState.EXPIRED


# --- launch failures, retries, stuck --------------------------------------------------------------


def test_a_failed_launch_is_retried_then_stuck(world: World) -> None:
    config = _config(NOTIFY)
    world.launch.fail = "tmux new-window failed: no server"
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    run = _only_run()
    assert (run.state, run.attempts) == (RunState.BLOCKED, 1)
    assert run.reason.startswith("launch failed")
    world.tick(config, T0 + timedelta(minutes=2))
    world.tick(config, T0 + timedelta(minutes=3))
    run = _only_run()
    assert (run.state, run.attempts) == (RunState.STUCK, 3)
    assert len(world.launch.windows) == 3
    world.tick(config, T0 + timedelta(minutes=4))
    assert len(world.launch.windows) == 3


def test_a_runs_own_failed_attempt_does_not_count_against_its_caps(world: World) -> None:
    config = _config(NOTIFY, max_runs_per_hour=1, ticket_breaker=1)
    world.launch.fail = "tmux: no server"
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    world.launch.fail = ""
    world.tick(config, T0 + timedelta(minutes=2))
    run = _only_run()
    assert (run.state, run.attempts) == (RunState.LAUNCHED, 2)


def _crashed_mid_launch(
    world: World, config: BoardConfig, attempts: int, at: datetime, *, flow: str = "review"
) -> Run:
    """A claimed (and approved) run whose launch began at ``at`` and never reported a window."""
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    run = runs.update(
        f"DR-1.{flow}.10-1",
        expect={RunState.AWAITING_CONFIRM, RunState.CLAIMED, RunState.LAUNCHED},
        state=RunState.CLAIMED,
        attempts=attempts,
        launched_at=runs.iso(at),
        approved_at=runs.iso(T0 + timedelta(minutes=1)),
        pane="",
    )
    assert run is not None
    return run


def test_a_command_claim_without_a_window_waits_then_retries(world: World) -> None:
    config = _config({**NOTIFY, "mode": "confirm"})
    _crashed_mid_launch(world, config, 1, T0 + timedelta(minutes=1), flow="notify")
    result = world.tick(config, T0 + timedelta(minutes=5))
    assert [a.verb for a in result.actions] == ["waiting"]
    assert world.launch.windows == []
    world.tick(config, T0 + timedelta(minutes=12))
    run = _only_run()
    assert (run.state, run.attempts) == (RunState.LAUNCHED, 2)


def test_an_agent_claim_without_a_window_or_a_record_is_stuck_not_relaunched(
    world: World, repo: Path
) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _crashed_mid_launch(world, config, 1, T0 + timedelta(minutes=1))
    world.tick(config, T0 + timedelta(minutes=12))
    run = _only_run()
    assert (run.state, run.reason) == (RunState.STUCK, flows_tick.UNACCOUNTED)
    assert world.launch.windows == [] and world.launch.starts == []


def test_an_agent_whose_launch_was_never_recorded_is_done_not_relaunched(
    world: World, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LAUNCHED write is lost, the agent's window closes, the guards lapse."""
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    real = runs.update
    lost = {"n": 0}

    def flaky(run_id: str, *, expect: Any, now: Any = None, **changes: Any) -> Run | None:
        if changes.get("state") is RunState.LAUNCHED and lost["n"] == 0:
            lost["n"] += 1
            return None  # runs/.lock held for > 2s, or the disk refused the write
        return real(run_id, expect=expect, now=now, **changes)

    monkeypatch.setattr(runs, "update", flaky)
    world.tick(config, T0 + timedelta(minutes=2))
    assert len(world.launch.windows) == 1
    assert _only_run().state is RunState.CLAIMED
    world.flow_panes = {}
    world.starts = {}
    world.tick(config, T0 + timedelta(minutes=14))
    run = _only_run()
    assert run.state is RunState.DONE
    assert len(world.launch.windows) == 1


def test_a_claim_out_of_attempts_is_stuck(world: World) -> None:
    config = _config({**NOTIFY, "mode": "confirm"})
    _crashed_mid_launch(world, config, 3, T0 + timedelta(minutes=1), flow="notify")
    world.tick(config, T0 + timedelta(minutes=12))
    run = _only_run()
    assert run.state is RunState.STUCK
    assert world.launch.windows == []
    assert len(_events(eventlog.EventKind.FLOW_FINISHED)) == 1


# --- finishing launched runs ------------------------------------------------------------------------


def _launched_command(world: World) -> tuple[BoardConfig, Run]:
    config = _config(NOTIFY)
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    run = _only_run()
    assert run.state is RunState.LAUNCHED
    return config, run


@pytest.mark.parametrize(("code", "state"), [("0\n", RunState.DONE), ("3\n", RunState.FAILED)])
def test_the_exit_file_finishes_a_command_run(world: World, code: str, state: RunState) -> None:
    config, run = _launched_command(world)
    world.panes = {run.pane}
    runs.exit_path(run.run_id).write_text(code)
    world.tick(config, T0 + timedelta(minutes=2))
    finished = _only_run()
    assert finished.state is state
    assert finished.exit_code == int(code)
    (event,) = _events(eventlog.EventKind.FLOW_FINISHED)
    assert event.extra is not None and event.extra["run_id"] == run.run_id


def test_a_command_window_gone_without_an_exit_file_failed(world: World) -> None:
    config, _ = _launched_command(world)
    world.panes = set()
    world.tick(config, T0 + timedelta(minutes=2))
    run = _only_run()
    assert (run.state, run.reason) == (RunState.FAILED, "window closed without an exit status")


def test_a_running_command_stays_launched(world: World) -> None:
    config, run = _launched_command(world)
    world.panes = {run.pane}
    world.tick(config, T0 + timedelta(minutes=2))
    assert _only_run().state is RunState.LAUNCHED


def test_an_unreachable_tmux_finishes_nothing(world: World) -> None:
    config, _ = _launched_command(world)
    world.panes = None
    world.tick(config, T0 + timedelta(minutes=2))
    assert _only_run().state is RunState.LAUNCHED


def test_an_agent_run_is_done_when_its_window_closes(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    world.tick(config, T0 + timedelta(minutes=2))
    world.panes = set()
    world.tick(config, T0 + timedelta(minutes=3))
    assert _only_run().state is RunState.DONE


# --- dry run and the lock ---------------------------------------------------------------------------


def test_a_dry_run_writes_nothing_and_launches_nothing(world: World) -> None:
    config = _config(NOTIFY)
    _baseline(world, config)
    before = snap.snapshot_path().read_text()
    events_before = len(eventlog.read())
    world.jira.put("DR-1", "Peer Review", "10")
    result = world.tick(config, T0 + timedelta(minutes=1), dry_run=True)
    assert result.ok and result.dry_run
    assert [a.verb for a in result.actions] == ["created", "dispatch"]
    assert "flow:DR-1:notify" in result.actions[1].detail
    assert [t.key for t in result.transitions] == ["DR-1"]
    assert runs.list_runs() == []
    assert not runs.payloads_dir().exists()
    assert world.launch.windows == []
    assert snap.snapshot_path().read_text() == before
    assert len(eventlog.read()) == events_before


def test_a_dry_run_of_a_first_tick_writes_no_snapshot(world: World) -> None:
    world.jira.put("DR-1", "Peer Review", "10")
    result = world.tick(_config(REVIEW), T0, dry_run=True)
    assert result.adopted == ("review",)
    assert snap.load() is None


def test_a_tick_that_cannot_take_the_lock_does_nothing(world: World) -> None:
    config = _config(REVIEW)
    world.jira.put("DR-1", "Peer Review", "10")
    with locked(snap.tick_lock_path(), timeout=0) as held:
        assert held
        result = world.tick(config, T0)
    assert result.status is TickStatus.LOCKED
    assert snap.load() is None


def test_preconditions(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    assert flows_tick.run(None).status is TickStatus.NO_CONFIG
    broken = board.parse({"board": {"columns": []}})
    assert flows_tick.run(broken).status is TickStatus.BOARD_INVALID
    monkeypatch.delenv("EPHOR_JIRA_URL")
    assert flows_tick.run(_config()).status is TickStatus.JIRA_OFF


def test_terminal_runs_are_pruned_after_thirty_days(world: World) -> None:
    config = _config(REVIEW)
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    runs.deny("DR-1.review.10-1")
    old = runs.load("DR-1.review.10-1")
    assert old is not None
    path = runs.run_path(old.run_id)
    data = json.loads(path.read_text())
    data["updated_at"] = (T0 - timedelta(days=40)).isoformat()
    path.write_text(json.dumps(data))
    world.tick(config, T0 + timedelta(minutes=2))
    assert runs.list_runs() == []


def test_generation_current_tracks_status_and_seq() -> None:
    run = Run(
        run_id="DR-1.review.10-1",
        flow="review",
        ticket="DR-1",
        trigger="on_enter",
        generation="10-1",
        state=RunState.CLAIMED,
        created_at="",
        updated_at="",
        to_status="Peer Review",
        status_id="10",
        seq=1,
    )
    same = TicketState(status="Peer Review", status_id="10", seq=1)
    assert flows_tick.generation_current(run, {"DR-1": same})
    assert not flows_tick.generation_current(
        run, {"DR-1": TicketState(status="QA", status_id="11", seq=2)}
    )
    assert not flows_tick.generation_current(
        run, {"DR-1": TicketState(status="Peer Review", status_id="10", seq=3)}
    )
    assert not flows_tick.generation_current(run, {})
    # Unseen tickets keep their last status, so their runs stay current.
    assert flows_tick.generation_current(
        run, {"DR-1": TicketState(status_id="10", seq=1, unseen_ticks=5)}
    )


def test_snapshot_is_written_last(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(NOTIFY)
    _baseline(world, config)
    order: list[str] = []
    real_save = snap.save
    real_create = runs.create

    def save(snapshot: Snapshot, path: Path | None = None) -> bool:
        order.append("snapshot")
        return real_save(snapshot, path)

    def create(run: Run) -> bool:
        order.append("run")
        return real_create(run)

    monkeypatch.setattr(snap, "save", save)
    monkeypatch.setattr(runs, "create", create)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    assert order == ["run", "snapshot"]
    assert os.path.exists(snap.snapshot_path())


# --- launches within one tick count against the agent guards ---------------------------------


def _approve_all() -> None:
    for run in runs.list_runs():
        if run.state is RunState.AWAITING_CONFIRM:
            approved, _ = runs.approve(run.run_id)
            assert approved is not None


def test_max_active_agents_counts_agents_this_tick_launched(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)}, max_active_agents=2)
    for key in ("DR-1", "DR-2", "DR-3"):
        world.jira.put(key, "In Progress", "3")
    world.tick(config, T0)
    for key in ("DR-1", "DR-2", "DR-3"):
        world.jira.put(key, "Peer Review", "10")
    world.tick(config, T0 + timedelta(minutes=1))
    _approve_all()
    world.sessions = [_agent("s1")]
    result = world.tick(config, T0 + timedelta(minutes=2))
    verbs = sorted(a.verb for a in result.actions)
    assert verbs == ["blocked", "blocked", "dispatched"]
    assert len(world.launch.windows) == 1
    blocked = [r for r in runs.list_runs() if r.state is RunState.BLOCKED]
    assert {r.reason for r in blocked} == {"max_active_agents: 2 sessions running"}


def test_two_agent_flows_on_one_move_launch_one_agent(world: World, repo: Path) -> None:
    second = {**REVIEW, "name": "sec-review", "start": "/security-review {ticket}"}
    config = _config(REVIEW, second, repos={"DR": str(repo)})
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    _approve_all()
    result = world.tick(config, T0 + timedelta(minutes=2))
    assert sorted(a.verb for a in result.actions) == ["blocked", "dispatched"]
    assert len(world.launch.windows) == 1
    (blocked,) = [r for r in runs.list_runs() if r.state is RunState.BLOCKED]
    assert blocked.reason == "launch guard: an agent was launched on DR-1 this tick"


def test_a_dry_run_counts_its_own_would_be_launches_against_the_caps(world: World) -> None:
    config = _config(NOTIFY, max_runs_per_hour=1)
    world.jira.put("DR-1", "In Progress", "3")
    world.jira.put("DR-2", "In Progress", "3")
    world.tick(config, T0)
    world.jira.put("DR-1", "Peer Review", "10")
    world.jira.put("DR-2", "Peer Review", "10")
    result = world.tick(config, T0 + timedelta(minutes=1), dry_run=True)
    verbs = [a.verb for a in result.actions if a.verb != "created"]
    assert sorted(verbs) == ["blocked", "dispatch"]
    (blocked,) = [a for a in result.actions if a.verb == "blocked"]
    assert blocked.detail.startswith("max_runs_per_hour")
    assert runs.list_runs() == []
    assert world.launch.windows == []


def test_a_dry_run_counts_would_be_agents_against_the_launch_guard(
    world: World, repo: Path
) -> None:
    second = {**REVIEW, "name": "sec-review", "start": "/security-review {ticket}"}
    config = _config(REVIEW, second, repos={"DR": str(repo)})
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    _approve_all()
    result = world.tick(config, T0 + timedelta(minutes=2), dry_run=True)
    assert sorted(a.verb for a in result.actions) == ["blocked", "dispatch"]


# --- a ticket that leaves the board and comes back -----------------------------------------------


def _dropped_from_review(world: World, config: BoardConfig) -> datetime:
    """DR-9 appears in Peer Review (firing once), then leaves the search for good."""
    world.panes = None  # the first window's fate is not what this is about
    _baseline(world, config)
    at = T0 + timedelta(minutes=1)
    world.jira.put("DR-9", "Peer Review", "10")
    world.tick(config, at)
    assert [r.run_id for r in runs.list_runs()] == ["DR-9.notify.10-0"]
    del world.jira.issues["DR-9"]
    for _ in range(snap.DROP_AFTER_UNSEEN + 1):
        at += timedelta(minutes=1)
        world.tick(config, at)
    stored = snap.load()
    assert stored is not None and "DR-9" not in stored.tickets
    assert stored.dropped["DR-9"].seq == 0
    assert stored.dropped["DR-9"].status_id == "10"
    return at


def test_a_ticket_that_returns_in_the_status_it_left_in_does_not_fire_again(
    world: World,
) -> None:
    config = _config(NOTIFY)
    at = _dropped_from_review(world, config)
    # Back in the search (an edit to an old ticket), still in Peer Review:
    # it never moved, so the visit it is on already fired.
    world.jira.put("DR-9", "Peer Review", "10")
    result = world.tick(config, at + timedelta(minutes=1))
    assert [a.verb for a in result.actions if a.verb == "created"] == []
    assert [t.key for t in result.transitions] == []
    assert [r.run_id for r in runs.list_runs()] == ["DR-9.notify.10-0"]
    assert len(world.launch.windows) == 1
    stored = snap.load()
    assert stored is not None and "DR-9" not in stored.dropped
    assert stored.tickets["DR-9"].seq == 0


def test_a_ticket_that_returns_in_another_status_is_a_new_visit(world: World) -> None:
    config = _config(NOTIFY)
    at = _dropped_from_review(world, config)
    world.jira.put("DR-9", "In Progress", "3")
    world.tick(config, at + timedelta(minutes=1))
    world.jira.put("DR-9", "Peer Review", "10")
    world.tick(config, at + timedelta(minutes=2))
    # seq 0 was the dropped visit; the return to In Progress is 1, Peer Review 2.
    assert "DR-9.notify.10-2" in {r.run_id for r in runs.list_runs()}
    assert len(world.launch.windows) == 2


def test_a_lost_snapshot_does_not_reuse_the_seq_of_runs_on_disk(world: World) -> None:
    config = _config(NOTIFY)
    world.panes = None
    _baseline(world, config)
    world.jira.put("DR-9", "Peer Review", "10")
    world.tick(config, T0 + timedelta(minutes=1))
    assert [r.run_id for r in runs.list_runs()] == ["DR-9.notify.10-0"]
    snap.snapshot_path().unlink()
    world.tick(config, T0 + timedelta(minutes=2))  # a new baseline: adopts, fires nothing
    stored = snap.load()
    assert stored is not None and stored.tickets["DR-9"].seq == 1
    world.jira.put("DR-9", "In Progress", "3")
    world.tick(config, T0 + timedelta(minutes=3))
    world.jira.put("DR-9", "Peer Review", "10")
    world.tick(config, T0 + timedelta(minutes=4))
    assert "DR-9.notify.10-3" in {r.run_id for r in runs.list_runs()}


# --- a change of board scope adopts what it brings in ---------------------------------------------


def _scoped(projects: list[str], *rules: dict[str, Any]) -> BoardConfig:
    config = board.parse(
        {
            "board": {
                "projects": projects,
                "columns": ["To Do", "In Progress", "Peer Review", "Done"],
            },
            "flow": list(rules),
        }
    )
    assert config.errors == ()
    return config


def test_widening_the_board_adopts_the_tickets_it_brings_in(world: World) -> None:
    narrow = _scoped(["DR"], NOTIFY)
    _baseline(world, narrow)
    world.tick(narrow, T0 + timedelta(minutes=1))
    wide = _scoped(["DR", "OPS"], NOTIFY)
    for n in range(1, 9):
        world.jira.put(f"OPS-{n}", "Peer Review", "10")
    world.jira.put("DR-1", "Peer Review", "10")  # a real move on the same tick still fires
    result = world.tick(wide, T0 + timedelta(minutes=2))
    assert [r.run_id for r in runs.list_runs()] == ["DR-1.notify.10-1"]
    assert "board scope changed" in result.dispatch_note
    # Once adopted, the next ticket to appear under the new scope fires.
    world.jira.put("OPS-9", "Peer Review", "10")
    result = world.tick(wide, T0 + timedelta(minutes=3))
    assert "OPS-9.notify.10-0" in {r.run_id for r in runs.list_runs()}
    assert result.dispatch_note == ""


def test_a_snapshot_from_before_scope_was_recorded_adopts_once(world: World) -> None:
    config = _config(NOTIFY)
    _baseline(world, config)
    data = json.loads(snap.snapshot_path().read_text())
    del data["scope"]
    snap.snapshot_path().write_text(json.dumps(data))
    world.jira.put("DR-2", "Peer Review", "10")
    world.tick(config, T0 + timedelta(minutes=1))
    assert runs.list_runs() == []
    world.jira.put("DR-3", "Peer Review", "10")
    world.tick(config, T0 + timedelta(minutes=2))
    assert [r.run_id for r in runs.list_runs()] == ["DR-3.notify.10-0"]


# --- a retry looks for the attempt it is retrying ---------------------------------------------------


class _Crash(BaseException):
    """The process dying (Ctrl-C, the TUI quitting) right after tmux opened the window."""


def _crash_after_window(world: World, config: BoardConfig) -> Run:
    real = world.launch.open_window

    def crashing(
        name: str,
        cwd: Path,
        command: str,
        env: dict[str, str],
        /,
        *,
        detached: bool,
        target_session: str,
    ) -> tuple[str | None, str, str]:
        real(name, cwd, command, env, detached=detached, target_session=target_session)
        raise _Crash

    _baseline(world, config)
    world.jira.put("DR-1", "Peer Review", "10")
    deps = TickDeps(
        **{**world.deps().__dict__, "launchers": runners.Launchers(open_window=crashing)}
    )
    with pytest.raises(_Crash):
        flows_tick.run(config, deps=deps, now=T0 + timedelta(minutes=1))
    run = _only_run()
    assert (run.state, run.attempts) == (RunState.CLAIMED, 1)
    assert len(world.launch.windows) == 1
    return run


def test_a_retry_adopts_the_window_an_unrecorded_attempt_opened(world: World) -> None:
    config = _config(NOTIFY)
    run = _crash_after_window(world, config)
    world.panes = {"%1"}
    world.flow_panes = {run.run_id: "%1"}
    result = world.tick(config, T0 + timedelta(minutes=5))
    assert [a.verb for a in result.actions] == ["waiting"]
    result = world.tick(config, T0 + timedelta(minutes=12))
    adopted = _only_run()
    assert (adopted.state, adopted.pane, adopted.attempts) == (RunState.LAUNCHED, "%1", 1)
    assert adopted.reason == "adopted window from an earlier attempt"
    assert adopted.window == "flow:DR-1:notify"
    assert len(world.launch.windows) == 1
    (event,) = _events(eventlog.EventKind.FLOW_DISPATCHED)
    assert event.extra is not None and event.extra["reason"] == adopted.reason


def test_a_retry_folds_in_the_exit_of_an_attempt_that_finished(world: World) -> None:
    config = _config(NOTIFY)
    run = _crash_after_window(world, config)
    runs.exit_path(run.run_id).write_text("4\n")
    world.tick(config, T0 + timedelta(minutes=12))
    finished = _only_run()
    assert (finished.state, finished.exit_code) == (RunState.FAILED, 4)
    assert len(world.launch.windows) == 1
    assert len(_events(eventlog.EventKind.FLOW_FINISHED)) == 1


def test_a_retry_waits_while_tmux_cannot_say_what_is_running(world: World) -> None:
    config = _config(NOTIFY)
    _crash_after_window(world, config)
    world.flow_panes = None
    world.tick(config, T0 + timedelta(minutes=12))
    run = _only_run()
    assert run.state is RunState.BLOCKED
    assert run.reason.startswith("tmux did not answer")
    world.tick(config, T0 + timedelta(minutes=13))
    assert _only_run().state is RunState.BLOCKED
    assert len(world.launch.windows) == 1
    # tmux answers, and the window is not there: now it is safe to relaunch.
    world.flow_panes = {}
    world.tick(config, T0 + timedelta(minutes=14))
    relaunched = _only_run()
    assert (relaunched.state, relaunched.attempts) == (RunState.LAUNCHED, 2)
    assert len(world.launch.windows) == 2


def test_a_relaunch_clears_a_half_written_exit_file(world: World) -> None:
    config = _config(NOTIFY)
    world.launch.fail = "tmux: no server"
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    run = _only_run()
    assert (run.state, run.attempts) == (RunState.BLOCKED, 1)
    runs.exit_tmp_path(run.run_id).write_text("0\n")
    world.launch.fail = ""
    world.tick(config, T0 + timedelta(minutes=2))
    assert not runs.exit_tmp_path(run.run_id).exists()
    relaunched = _only_run()
    assert relaunched.state is RunState.LAUNCHED
    world.panes = {relaunched.pane}
    world.tick(config, T0 + timedelta(minutes=3))
    assert _only_run().state is RunState.LAUNCHED


def test_a_reported_failure_that_left_an_exit_status_is_not_relaunched(world: World) -> None:
    """tmux said the launch failed, but the command ran: one execution per event."""
    config = _config(NOTIFY)
    world.launch.fail = "tmux new-window failed: timed out after 20 seconds"
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    run = _only_run()
    runs.exit_path(run.run_id).write_text("0\n")
    world.launch.fail = ""
    world.tick(config, T0 + timedelta(minutes=2))
    finished = _only_run()
    assert (finished.state, finished.exit_code) == (RunState.DONE, 0)
    assert len(world.launch.windows) == 1


def test_a_reported_failure_whose_window_opened_is_adopted(world: World) -> None:
    config = _config(NOTIFY)
    world.launch.fail = "tmux new-window failed: timed out after 20 seconds"
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    run = _only_run()
    assert (run.state, run.attempts) == (RunState.BLOCKED, 1)
    # tmux now reports the window the "failed" attempt did open.
    world.flow_panes = {run.run_id: "%7"}
    world.panes = {"%7"}
    world.launch.fail = ""
    world.tick(config, T0 + timedelta(minutes=2))
    adopted = _only_run()
    assert (adopted.state, adopted.pane) == (RunState.LAUNCHED, "%7")
    assert adopted.tmux_server == world.server
    assert len(world.launch.windows) == 1


# --- on_pr --------------------------------------------------------------------------

PR_REVIEW = {"name": "pr-review", "on_pr": "opened", "start": "/review-pr {ticket}"}
PR_PING = {"name": "pr-ping", "on_pr": "opened", "command": "ping.sh {ticket}", "mode": "auto"}
SHIPPED = {"name": "shipped", "on_pr": "merged", "command": "shipped.sh {ticket}", "mode": "auto"}


def _pr(number: int, state: str = "OPEN", sha: str = "abc123") -> PullRequest:
    return PullRequest(
        number=number,
        url=f"https://github.com/o/r/pull/{number}",
        state=state,
        title=f"DR-1: change {number}",
        head_ref="feature/DR-1-x",
        head_sha=sha,
    )


def _pr_baseline(world: World, config: BoardConfig, at: datetime = T0) -> None:
    """DR-1 In Progress with no PR yet: the rule adopts and the lookup sets a baseline."""
    world.jira.put("DR-1", "In Progress", "3")
    result = world.tick(config, at)
    assert result.ok, result.message
    stored = snap.load()
    assert stored is not None and stored.prs == {"DR-1": ()}


def test_on_pr_opened_fires_once_for_a_new_open_pr(world: World, repo: Path) -> None:
    config = _config(PR_REVIEW, repos={"DR": str(repo)})
    _pr_baseline(world, config)
    world.prs["DR-1"] = [_pr(12)]
    result = world.tick(config, T0 + timedelta(minutes=5))
    run = _only_run()
    assert run.run_id == "DR-1.pr-review.pr12"
    assert run.state is RunState.AWAITING_CONFIRM
    assert (run.trigger, run.event, run.pr_number, run.head_sha) == (
        "on_pr",
        "opened",
        12,
        "abc123",
    )
    assert run.to_status == "In Progress"  # what {status} renders as
    assert run.label == "PR #12 opened"
    assert [(e.key, e.value, e.generation) for e in result.events] == [("DR-1", "opened", "pr12")]
    # Seen again on later lookups: the same PR never fires twice.
    world.tick(config, T0 + timedelta(minutes=10))
    world.tick(config, T0 + timedelta(minutes=15))
    assert [r.run_id for r in runs.list_runs()] == ["DR-1.pr-review.pr12"]


def test_on_pr_lookups_are_spaced_by_pr_interval(world: World, repo: Path) -> None:
    config = _config(PR_PING, repos={"DR": str(repo)}, pr_interval="5m")
    _pr_baseline(world, config)
    assert len(world.pr_calls) == 1
    world.prs["DR-1"] = [_pr(12)]
    world.tick(config, T0 + timedelta(minutes=1))
    world.tick(config, T0 + timedelta(minutes=4))
    assert len(world.pr_calls) == 1
    assert runs.list_runs() == []
    world.tick(config, T0 + timedelta(minutes=5))
    assert len(world.pr_calls) == 2
    assert world.pr_calls[-1] == (repo, "DR-1")
    stored = snap.load()
    assert stored is not None and stored.pr_checked_at == runs.iso(T0 + timedelta(minutes=5))
    assert [r.run_id for r in runs.list_runs()] == ["DR-1.pr-ping.pr12"]


def test_an_auto_pr_flow_launches_with_the_pr_in_its_payload(world: World, repo: Path) -> None:
    config = _config(PR_PING, repos={"DR": str(repo)})
    _pr_baseline(world, config)
    world.prs["DR-1"] = [_pr(12)]
    world.tick(config, T0 + timedelta(minutes=5))
    run = _only_run()
    assert run.state is RunState.LAUNCHED
    [window] = world.launch.windows
    assert window["name"] == "flow:DR-1:pr-ping"
    assert window["env"]["EPHOR_FLOW_RUN"] == "DR-1.pr-ping.pr12"
    payload = json.loads(Path(window["env"]["EPHOR_FLOW_PAYLOAD"]).read_text())
    assert payload["trigger"] == "on_pr"
    assert payload["event"] == "opened"
    assert payload["pr"] == {
        "number": 12,
        "url": "https://github.com/o/r/pull/12",
        "head_sha": "abc123",
        "state": "OPEN",
    }
    assert payload["session_id"] == ""


def test_a_pr_already_open_at_the_first_lookup_is_a_baseline(world: World, repo: Path) -> None:
    config = _config(PR_PING, repos={"DR": str(repo)})
    world.jira.put("DR-1", "In Progress", "3")
    world.prs["DR-1"] = [_pr(12)]
    world.tick(config, T0)  # adopts the rule, and the PR
    world.tick(config, T0 + timedelta(minutes=5))
    # A ticket new to the board whose PR is already open: also a baseline.
    world.jira.put("DR-2", "In Progress", "3")
    world.prs["DR-2"] = [_pr(13)]
    world.tick(config, T0 + timedelta(minutes=10))
    world.tick(config, T0 + timedelta(minutes=15))
    assert runs.list_runs() == []
    assert world.launch.windows == []


def test_on_pr_merged_fires_on_the_merge_and_opened_does_not_refire(
    world: World, repo: Path
) -> None:
    config = _config(PR_PING, SHIPPED, repos={"DR": str(repo)})
    _pr_baseline(world, config)
    world.prs["DR-1"] = [_pr(12)]
    world.tick(config, T0 + timedelta(minutes=5))
    # The ticket goes to Done; its PR merges. Done tickets with a PR last seen
    # open are still asked about — that is how the merge is seen.
    world.jira.put("DR-1", "Done", "20")
    world.prs["DR-1"] = [_pr(12, "MERGED", sha="def456")]
    world.tick(config, T0 + timedelta(minutes=10))
    ids = sorted(r.run_id for r in runs.list_runs())
    assert ids == ["DR-1.pr-ping.pr12", "DR-1.shipped.pr12"]
    shipped = runs.load("DR-1.shipped.pr12")
    assert shipped is not None and shipped.event == "merged" and shipped.head_sha == "def456"
    # Once nothing is open on a Done ticket it is no longer looked up.
    calls = len(world.pr_calls)
    world.tick(config, T0 + timedelta(minutes=15))
    assert len(world.pr_calls) == calls


def test_a_done_ticket_with_no_open_pr_is_not_looked_up(world: World, repo: Path) -> None:
    config = _config(SHIPPED, repos={"DR": str(repo)})
    world.jira.put("DR-1", "Done", "20")
    world.jira.put("DR-2", "In Progress", "3")
    world.tick(config, T0)
    assert [key for _, key in world.pr_calls] == ["DR-2"]


def test_on_pr_only_asks_about_tickets_a_rule_could_fire_on(world: World, repo: Path) -> None:
    config = _config(PR_PING, repos={"DR": str(repo)})
    world.jira.put("DR-1", "In Progress", "3")
    world.jira.put("DR-2", "In Progress", "3", assignee="acct-other")
    world.tick(config, T0)
    assert [key for _, key in world.pr_calls] == ["DR-1"]


def test_a_gh_failure_keeps_the_baseline_and_fires_nothing(world: World, repo: Path) -> None:
    config = _config(PR_PING, repos={"DR": str(repo)})
    _pr_baseline(world, config)
    world.prs["DR-1"] = None  # gh did not answer
    world.tick(config, T0 + timedelta(minutes=5))
    stored = snap.load()
    assert stored is not None and stored.prs == {"DR-1": ()}
    assert runs.list_runs() == []
    # The next answer is compared with the baseline that survived.
    world.prs["DR-1"] = [_pr(12)]
    world.tick(config, T0 + timedelta(minutes=10))
    assert [r.run_id for r in runs.list_runs()] == ["DR-1.pr-ping.pr12"]


def test_a_ticket_with_no_repo_is_not_looked_up(world: World) -> None:
    config = _config(PR_PING)
    world.jira.put("DR-1", "In Progress", "3")
    world.tick(config, T0)
    assert world.pr_calls == []
    stored = snap.load()
    assert stored is not None and stored.prs == {}


def test_an_awaiting_opened_run_expires_when_the_pr_closes(world: World, repo: Path) -> None:
    config = _config(PR_REVIEW, repos={"DR": str(repo)})
    _pr_baseline(world, config)
    world.prs["DR-1"] = [_pr(12)]
    world.tick(config, T0 + timedelta(minutes=5))
    # A status move does not end it: an on_pr run is about the PR.
    world.jira.put("DR-1", "Peer Review", "10")
    world.tick(config, T0 + timedelta(minutes=6))
    assert _only_run().state is RunState.AWAITING_CONFIRM
    world.prs["DR-1"] = [_pr(12, "CLOSED")]
    world.tick(config, T0 + timedelta(minutes=10))
    run = _only_run()
    assert run.state is RunState.EXPIRED
    assert run.reason == "PR #12 is no longer open"


def test_an_approved_pr_review_waits_for_the_implementing_session(world: World, repo: Path) -> None:
    config = _config(PR_REVIEW, repos={"DR": str(repo)})
    _pr_baseline(world, config)
    world.prs["DR-1"] = [_pr(12)]
    world.tick(config, T0 + timedelta(minutes=5))
    runs.approve("DR-1.pr-review.pr12")
    world.sessions = [_agent("impl-session", "DR-1")]
    world.tick(config, T0 + timedelta(minutes=6))
    run = _only_run()
    assert run.state is RunState.BLOCKED
    assert run.reason.startswith("launch guard: session impl-ses")
    world.sessions = []
    world.tick(config, T0 + timedelta(minutes=7))
    assert _only_run().state is RunState.LAUNCHED
    [window] = world.launch.windows
    assert "/review-pr DR-1" in window["command"]


def test_on_pr_after_a_stale_gap_waits_for_a_human(world: World, repo: Path) -> None:
    config = _config(PR_PING, repos={"DR": str(repo)})
    _pr_baseline(world, config)
    world.prs["DR-1"] = [_pr(12)]
    world.tick(config, T0 + timedelta(hours=3))
    run = _only_run()
    assert run.state is RunState.AWAITING_CONFIRM and run.stale
    assert world.launch.windows == []


def test_removing_every_on_pr_rule_clears_the_observation(world: World, repo: Path) -> None:
    config = _config(PR_PING, repos={"DR": str(repo)})
    _pr_baseline(world, config)
    world.tick(_config(repos={"DR": str(repo)}), T0 + timedelta(minutes=1))
    stored = snap.load()
    assert stored is not None and (stored.prs, stored.pr_checked_at) == ({}, "")
    # Put back later: a fresh baseline, so a PR opened meanwhile does not fire.
    world.prs["DR-1"] = [_pr(12)]
    world.tick(config, T0 + timedelta(minutes=2))
    world.tick(config, T0 + timedelta(minutes=8))
    assert runs.list_runs() == []


def test_dry_run_looks_prs_up_but_writes_nothing(world: World, repo: Path) -> None:
    config = _config(PR_PING, repos={"DR": str(repo)})
    _pr_baseline(world, config)
    before = snap.snapshot_path().read_text()
    world.prs["DR-1"] = [_pr(12)]
    result = world.tick(config, T0 + timedelta(minutes=5), dry_run=True)
    assert [a.verb for a in result.actions] == ["created", "dispatch"]
    assert snap.snapshot_path().read_text() == before
    assert runs.list_runs() == [] and world.launch.windows == []


# --- on_session -----------------------------------------------------------------------

DIED = {"name": "on-died", "on_session": "died", "command": "died.sh {ticket}", "mode": "auto"}
IDLE = {"name": "on-idle", "on_session": "idle", "command": "idle.sh {ticket}"}
SID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


def _session(
    sid: str = SID,
    ticket: str = "DR-1",
    status: str = "WORKING",
    *,
    pid: int | None = 4242,
    last_event: datetime = T0,
) -> AgentState:
    return AgentState.from_dict(
        {
            "session_id": sid,
            "cwd": "/nowhere",
            "started_at": "2026-01-01T00:00:00+00:00",
            "status": status,
            "ticket": ticket,
            "agent_pid": pid,
            "last_event_time": runs.iso(last_event),
            "schema_version": 4,
        }
    )


def _session_baseline(world: World, config: BoardConfig, *sessions: AgentState) -> None:
    world.jira.put("DR-1", "In Progress", "3")
    world.sessions = list(sessions)
    result = world.tick(config, T0)
    assert result.ok, result.message


def test_on_session_died_fires_when_a_seen_session_goes_dead(world: World, repo: Path) -> None:
    config = _config(DIED, repos={"DR": str(repo)})
    _session_baseline(world, config, _session())
    world.sessions = [_session(status="DEAD")]
    world.dead_pids.add(4242)
    result = world.tick(config, T0 + timedelta(minutes=1))
    run = _only_run()
    assert run.run_id == f"DR-1.on-died.{SID[:12]}"
    assert (run.event, run.session_id, run.label) == ("died", SID, f"session {SID[:8]} died")
    assert run.state is RunState.LAUNCHED
    assert [e.value for e in result.events] == ["died"]
    payload = json.loads(Path(world.launch.windows[0]["env"]["EPHOR_FLOW_PAYLOAD"]).read_text())
    assert (payload["session_id"], payload["pr"]) == (SID, None)
    # Still DEAD on the next tick, then gone: one death, one run.
    world.tick(config, T0 + timedelta(minutes=2))
    world.sessions = []
    world.tick(config, T0 + timedelta(minutes=3))
    assert len(runs.list_runs()) == 1


def test_a_deleted_state_file_of_a_dead_process_is_a_death(world: World, repo: Path) -> None:
    config = _config(DIED, repos={"DR": str(repo)})
    _session_baseline(world, config, _session(status="IDLE"))
    # The reconciler removed the file before any tick saw it DEAD.
    world.sessions = []
    world.dead_pids.add(4242)
    world.tick(config, T0 + timedelta(minutes=1))
    assert [r.run_id for r in runs.list_runs()] == [f"DR-1.on-died.{SID[:12]}"]


def test_resume_residue_and_unknown_vanishings_are_not_deaths(world: World, repo: Path) -> None:
    config = _config(DIED, repos={"DR": str(repo)})
    other = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
    _session_baseline(world, config, _session(), _session(other, pid=None))
    # SID is marked DEAD by the scan while its process lives on (`--resume`);
    # `other` had no pid recorded and its file disappeared.
    world.sessions = [_session(status="DEAD")]
    world.tick(config, T0 + timedelta(minutes=1))
    world.sessions = []
    world.tick(config, T0 + timedelta(minutes=2))
    assert runs.list_runs() == []


def test_on_session_idle_fires_after_settle(world: World, repo: Path) -> None:
    config = _config(IDLE, repos={"DR": str(repo)}, settle="10m")
    _session_baseline(world, config, _session(status="WORKING"))
    went_idle = T0 + timedelta(minutes=1)
    world.sessions = [_session(status="IDLE", last_event=went_idle)]
    world.tick(config, T0 + timedelta(minutes=2))
    world.tick(config, T0 + timedelta(minutes=10))
    assert runs.list_runs() == []
    world.tick(config, went_idle + timedelta(minutes=10))
    run = _only_run()
    assert run.run_id == f"DR-1.on-idle.{SID[:12]}"
    assert run.state is RunState.AWAITING_CONFIRM
    # Work, then idle again: the same session never fires the rule twice.
    world.sessions = [_session(status="WORKING", last_event=T0 + timedelta(minutes=12))]
    world.tick(config, T0 + timedelta(minutes=12))
    later = T0 + timedelta(minutes=13)
    world.sessions = [_session(status="IDLE", last_event=later)]
    world.tick(config, later + timedelta(minutes=11))
    assert len(runs.list_runs()) == 1


def test_an_idle_run_expires_when_its_session_works_again(world: World, repo: Path) -> None:
    config = _config(IDLE, repos={"DR": str(repo)}, settle="10m")
    _session_baseline(world, config, _session(status="IDLE", last_event=T0))
    world.tick(config, T0 + timedelta(minutes=10))
    assert _only_run().state is RunState.AWAITING_CONFIRM
    world.sessions = [_session(status="WORKING", last_event=T0 + timedelta(minutes=11))]
    world.tick(config, T0 + timedelta(minutes=11))
    run = _only_run()
    assert run.state is RunState.EXPIRED
    assert run.reason == f"session {SID[:8]} is no longer idle"


def test_a_session_first_seen_already_settled_is_a_baseline(world: World, repo: Path) -> None:
    config = _config(IDLE, repos={"DR": str(repo)}, settle="10m")
    _session_baseline(world, config, _session(status="IDLE", last_event=T0 - timedelta(hours=2)))
    newcomer = "99998888-7777-6666-5555-444433332222"
    world.sessions = [
        _session(status="IDLE", last_event=T0 - timedelta(hours=2)),
        _session(newcomer, status="IDLE", last_event=T0 - timedelta(hours=1)),
    ]
    world.tick(config, T0 + timedelta(minutes=1))
    world.tick(config, T0 + timedelta(minutes=2))
    assert runs.list_runs() == []


def test_on_session_ignores_tickets_off_the_board_and_other_assignees(
    world: World, repo: Path
) -> None:
    config = _config(DIED, repos={"DR": str(repo)})
    world.jira.put("DR-2", "In Progress", "3", assignee="acct-other")
    s2 = "22222222-2222-2222-2222-222222222222"
    s3 = "33333333-3333-3333-3333-333333333333"
    _session_baseline(world, config, _session(s2, "DR-2", pid=2), _session(s3, "OPS-9", pid=3))
    world.sessions = [_session(s2, "DR-2", "DEAD", pid=2), _session(s3, "OPS-9", "DEAD", pid=3)]
    world.dead_pids.update({2, 3})
    result = world.tick(config, T0 + timedelta(minutes=1))
    assert sorted(e.key for e in result.events) == ["DR-2", "OPS-9"]
    assert runs.list_runs() == []


def test_session_observations_are_kept_only_while_a_rule_reads_them(
    world: World, repo: Path
) -> None:
    config = _config(DIED, repos={"DR": str(repo)})
    _session_baseline(world, config, _session())
    stored = snap.load()
    assert stored is not None and list(stored.sessions_seen) == [SID]
    assert stored.sessions_seen[SID].pid == 4242
    world.tick(_config(repos={"DR": str(repo)}), T0 + timedelta(minutes=1))
    stored = snap.load()
    assert stored is not None and stored.sessions_seen == {}


def test_a_new_session_rule_adopts_before_it_fires(world: World, repo: Path) -> None:
    _session_baseline(world, _config(repos={"DR": str(repo)}), _session())
    config = _config(DIED, repos={"DR": str(repo)})
    world.sessions = [_session(status="DEAD")]
    world.dead_pids.add(4242)
    result = world.tick(config, T0 + timedelta(minutes=1))
    assert result.adopted == ("on-died",)
    assert runs.list_runs() == []


def test_the_tick_scans_sessions_once(world: World, repo: Path) -> None:
    config = _config(DIED, REVIEW, repos={"DR": str(repo)})
    _session_baseline(world, config, _session("s-other", "DR-7"))
    _approved_review_on(world, config)
    calls: list[int] = []
    original = world.deps

    def counting() -> TickDeps:
        base = original()

        def sessions() -> list[AgentState]:
            calls.append(1)
            return base.sessions()

        return replace(base, sessions=sessions)

    world.deps = counting  # type: ignore[method-assign]
    world.tick(config, T0 + timedelta(minutes=3))
    assert len(calls) == 1


def _approved_review_on(world: World, config: BoardConfig) -> None:
    world.jira.put("DR-1", "Peer Review", "10")
    world.tick(config, T0 + timedelta(minutes=1))
    approved, _ = runs.approve("DR-1.review.10-1")
    assert approved is not None


def test_no_session_event_fires_on_a_ticket_the_last_search_missed(
    world: World, repo: Path
) -> None:
    config = _config(DIED, repos={"DR": str(repo)})
    _session_baseline(world, config, _session())
    # DR-1 drops out of the search (kept in the snapshot, unseen) as its
    # session dies: absence is not a reason to fire, whatever the session did.
    del world.jira.issues["DR-1"]
    world.sessions = [_session(status="DEAD")]
    world.dead_pids.add(4242)
    result = world.tick(config, T0 + timedelta(minutes=1))
    assert [e.value for e in result.events] == ["died"]
    stored = snap.load()
    assert stored is not None and stored.tickets["DR-1"].unseen_ticks == 1
    assert runs.list_runs() == []


def test_a_broken_board_does_not_expire_a_waiting_idle_run(world: World, repo: Path) -> None:
    config = _config(IDLE, repos={"DR": str(repo)}, settle="10m")
    _session_baseline(world, config, _session(status="IDLE", last_event=T0))
    world.tick(config, T0 + timedelta(minutes=10))
    assert _only_run().state is RunState.AWAITING_CONFIRM
    broken = board.parse(
        {
            "board": {"projects": ["DR"]},
            "repos": {"DR": str(repo)},
            "flows": {"settle": "10m"},
            "flow": [IDLE, {"name": "bad", "on_enter": "X"}],
        }
    )
    assert broken.errors
    world.tick(broken, T0 + timedelta(minutes=11))
    assert _only_run().state is RunState.AWAITING_CONFIRM


# --- final review: a run launches the flow that made it -------------------------------


def test_an_auto_command_run_edited_into_a_start_flow_never_launches_unapproved(
    world: World, repo: Path
) -> None:
    v1 = _config({**NOTIFY, "name": "review"}, repos={"DR": str(repo)})
    _baseline(world, v1)
    runs.pause()
    _move_to_review(world, v1, T0 + timedelta(minutes=1))
    run = _only_run()
    assert (run.state, run.reason, run.kind) == (RunState.BLOCKED, "paused", "command")
    v2 = _config(REVIEW, repos={"DR": str(repo)})
    runs.resume()
    world.tick(v2, T0 + timedelta(minutes=2))
    assert world.launch.windows == [] and world.launch.starts == []
    run = _only_run()
    assert (run.state, run.reason) == (RunState.SUPERSEDED, flows_tick.FLOW_CHANGED)


def test_an_auto_run_whose_rule_now_wants_a_confirmation_does_not_launch(world: World) -> None:
    _baseline(world, _config(NOTIFY))
    runs.pause()
    _move_to_review(world, _config(NOTIFY), T0 + timedelta(minutes=1))
    runs.resume()
    world.tick(_config({**NOTIFY, "mode": "confirm"}), T0 + timedelta(minutes=2))
    assert world.launch.windows == []
    assert _only_run().state is RunState.SUPERSEDED


def test_an_approved_run_whose_template_changed_does_not_launch(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    edited = _config({**REVIEW, "start": "/implement {ticket}"}, repos={"DR": str(repo)})
    world.tick(edited, T0 + timedelta(minutes=2))
    assert world.launch.windows == []
    assert (_only_run().state, _only_run().reason) == (
        RunState.SUPERSEDED,
        flows_tick.FLOW_CHANGED,
    )


def test_an_awaiting_run_whose_flow_changed_expires_before_it_can_be_approved(
    world: World,
) -> None:
    config = _config(REVIEW)
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    world.tick(_config({**REVIEW, "start": "/implement {ticket}"}), T0 + timedelta(minutes=2))
    run = _only_run()
    assert (run.state, run.reason) == (RunState.EXPIRED, flows_tick.FLOW_CHANGED)


def test_a_start_run_never_approved_does_not_launch(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    # Claimed by something other than `approve` (a hand-edited run file).
    forged = runs.update(
        "DR-1.review.10-1", expect={RunState.AWAITING_CONFIRM}, state=RunState.CLAIMED
    )
    assert forged is not None and not forged.approved_at
    world.tick(config, T0 + timedelta(minutes=2))
    assert world.launch.windows == []
    assert _only_run().state is RunState.SUPERSEDED


def test_approve_records_when(world: World) -> None:
    config = _config(REVIEW)
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    approved, _ = runs.approve("DR-1.review.10-1", now=T0 + timedelta(minutes=3))
    assert approved is not None and approved.approved_at == runs.iso(T0 + timedelta(minutes=3))


# --- final review: held runs age out --------------------------------------------------------


def test_an_approval_held_past_confirm_ttl_expires_instead_of_launching(
    world: World, repo: Path
) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)}, confirm_ttl="1h", max_event_age="3h")
    _baseline(world, config)
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    runs.approve("DR-1.review.10-1", now=T0 + timedelta(minutes=2))
    world.sessions = [_agent("s1", "DR-1")]
    for minute in (3, 40, 61):
        world.tick(config, T0 + timedelta(minutes=minute))
        assert _only_run().state is RunState.BLOCKED
    world.sessions = []  # the guard clears, more than an hour after the yes
    world.tick(config, T0 + timedelta(minutes=63))
    run = _only_run()
    assert (run.state, run.reason) == (RunState.EXPIRED, flows_tick.HELD_TOO_LONG)
    assert world.launch.windows == []


def test_an_auto_run_held_by_a_long_pause_expires_on_resume(world: World) -> None:
    config = _config(NOTIFY, confirm_ttl="1h", max_event_age="3h")
    _baseline(world, config)
    runs.pause()
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    world.tick(config, T0 + timedelta(minutes=50))
    assert _only_run().state is RunState.BLOCKED
    runs.resume()
    world.tick(config, T0 + timedelta(minutes=62))
    assert (_only_run().state, _only_run().reason) == (RunState.EXPIRED, flows_tick.HELD_TOO_LONG)
    assert world.launch.windows == []


def test_a_short_pause_still_dispatches_on_resume(world: World) -> None:
    config = _config(NOTIFY)
    _baseline(world, config)
    runs.pause()
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    runs.resume()
    world.tick(config, T0 + timedelta(minutes=5))
    assert _only_run().state is RunState.LAUNCHED


# --- final review: unknown never means unmet ------------------------------------------------


def test_a_failed_session_scan_blocks_agent_launches(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)}, max_active_agents=1)
    _approved_review(world, config)

    def boom() -> list[AgentState]:
        raise PermissionError("state dir unreadable")

    deps = replace(world.deps(), sessions=boom)
    flows_tick.run(config, deps=deps, now=T0 + timedelta(minutes=2))
    run = _only_run()
    assert (run.state, run.reason) == (
        RunState.BLOCKED,
        "session scan failed — cannot count live agents",
    )
    assert world.launch.windows == []


@pytest.mark.parametrize("trigger", ["on_enter", "on_exit"])
def test_a_statusless_recheck_blocks_rather_than_deciding(world: World, trigger: str) -> None:
    rule = {"name": "x", trigger: "Peer Review", "command": "x.sh", "mode": "auto"}
    config = _config({**rule, "assignee": "any"})
    if trigger == "on_enter":
        _baseline(world, config)
        world.jira.put("DR-1", "Peer Review", "10")
    else:
        world.jira.put("DR-1", "Peer Review", "10")
        world.tick(config, T0)
        world.jira.put("DR-1", "In Progress", "3")
    world.jira.fresh["DR-1"] = Issue(key="DR-1", status="", status_id="")
    world.tick(config, T0 + timedelta(minutes=1))
    run = _only_run()
    assert (run.state, run.reason) == (RunState.BLOCKED, "Jira re-check returned no status")
    assert world.launch.windows == []


def test_a_recheck_answering_for_another_ticket_blocks(world: World) -> None:
    config = _config(NOTIFY)
    _baseline(world, config)
    world.jira.fresh["DR-1"] = Issue(key="DR-2", status="Peer Review", status_id="10")
    _move_to_review(world, config, T0 + timedelta(minutes=1))
    run = _only_run()
    assert run.state is RunState.BLOCKED and "DR-2" in run.reason


def test_an_approve_racing_expiry_is_still_checked_for_currency(
    world: World, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    triage = {"name": "triage", "on_pr": "opened", "command": "t.sh {ticket}"}
    config = _config(triage, repos={"DR": str(repo)})
    world.jira.put("DR-1", "In Progress", "3")
    world.tick(config, T0)
    world.prs["DR-1"] = []
    world.tick(config, T0 + timedelta(minutes=6))
    world.prs["DR-1"] = [_pr(5)]
    world.tick(config, T0 + timedelta(minutes=12))
    assert _only_run().state is RunState.AWAITING_CONFIRM
    world.prs["DR-1"] = [_pr(5, state="CLOSED")]
    real = flows_tick._tend_awaiting

    def racing(ledger: Any, cfg: Any, snapshot: Any, r: Run, *, watching_sessions: bool) -> None:
        runs.approve(r.run_id)  # y pressed in the f screen at this very moment
        real(ledger, cfg, snapshot, r, watching_sessions=watching_sessions)

    monkeypatch.setattr(flows_tick, "_tend_awaiting", racing)
    world.tick(config, T0 + timedelta(minutes=18))
    run = _only_run()
    assert run.state is RunState.SUPERSEDED
    assert world.launch.windows == []


# --- final review: one event per block, not one per tick ------------------------------------


def test_a_block_whose_reason_counts_up_logs_one_event(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    world.work["DR-1"] = WorkItem(key="DR-1", last_seen=(T0 + timedelta(minutes=1)).isoformat())
    for minute in range(3, 9):
        result = world.tick(config, T0 + timedelta(minutes=minute))
    run = _only_run()
    assert run.state is RunState.BLOCKED
    assert run.block_key == "launch_guard:work_record"
    assert len(_events(eventlog.EventKind.FLOW_BLOCKED)) == 1
    # The tick still reports the up-to-date reason.
    (note,) = [a for a in result.actions if a.verb == "blocked"]
    assert note.detail.endswith("updated 7m ago")


def test_a_block_for_a_different_guard_is_logged(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    world.sessions = [_agent("s1", "DR-1")]
    world.tick(config, T0 + timedelta(minutes=2))
    world.sessions = []
    runs.pause()
    world.tick(config, T0 + timedelta(minutes=3))
    assert [e.extra["reason"] for e in _events(eventlog.EventKind.FLOW_BLOCKED) if e.extra] == [
        "launch guard: session s1 is live on DR-1",
        "paused",
    ]


# --- final review: a pane belongs to a tmux server ------------------------------------------


def test_a_launched_run_records_its_tmux_server(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    world.panes = {"%1"}
    world.tick(config, T0 + timedelta(minutes=2))
    assert (_only_run().pane, _only_run().tmux_server) == ("%1", "100.1")


def test_a_pane_id_reused_by_a_new_tmux_server_is_not_the_runs_pane(
    world: World, repo: Path
) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    world.panes = {"%1"}
    world.tick(config, T0 + timedelta(minutes=2))
    # Reboot: a new server, and an unrelated pane that happens to be %1.
    world.server = "200.2"
    world.tick(config, T0 + timedelta(minutes=3))
    assert (_only_run().state, _only_run().reason) == (RunState.DONE, "agent window closed")


def test_no_tmux_server_at_all_finishes_launched_runs(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    world.panes = {"%1"}
    world.tick(config, T0 + timedelta(minutes=2))
    world.panes, world.server = set(), ""  # what live_panes reports for "no server running"
    world.tick(config, T0 + timedelta(minutes=3))
    assert _only_run().state is RunState.DONE


# --- final review: only facts tie a session to a ticket -------------------------------------


def test_a_session_that_only_mentions_a_ticket_is_not_on_it() -> None:
    chatter = AgentState.from_dict(
        {
            "session_id": "other-session",
            "cwd": "/nowhere",
            "started_at": "2026-01-01T00:00:00+00:00",
            "status": "IDLE",
            "last_summary": "get-peer-reviews: DR-3 and DR-4 are waiting for you",
            "schema_version": 4,
        }
    )
    assert flows_tick._ticket_of(chatter) == ""
    declared = replace(chatter, ticket="DR-3")
    assert flows_tick._ticket_of(declared) == "DR-3"


def test_the_launch_guard_ignores_a_dashboard_seeing_a_session(world: World) -> None:
    eventlog.append(
        eventlog.EventKind.SESSION_STARTED,
        session_id="s1",
        ticket="DR-1",
        detail="claude in aim",
        observed=True,
    )
    eventlog.append(eventlog.EventKind.SESSION_STARTED, ticket="DR-2", detail="old dashboard")
    eventlog.append(
        eventlog.EventKind.SESSION_STARTED,
        ticket="DR-3",
        detail="Claude in aim-DR-3",
        provider="claude",
        worktree="/x/aim-DR-3",
    )
    eventlog.append(
        eventlog.EventKind.SESSION_STARTED,
        ticket="DR-4",
        detail="flow review in aim",
        provider="claude",
        run_id="DR-4.review.10-1",
    )
    assert set(flows_tick._recent_starts(600.0)) == {"DR-3", "DR-4"}


def test_the_launch_guard_says_how_long_ago_a_session_started(world: World, repo: Path) -> None:
    config = _config(REVIEW, repos={"DR": str(repo)})
    _approved_review(world, config)
    world.starts = {"DR-1": (T0 - timedelta(minutes=3)).isoformat()}
    world.tick(config, T0 + timedelta(minutes=2))
    assert _only_run().reason == "launch guard: a session was started on DR-1 5m ago"

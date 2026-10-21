"""board.toml: parsing, collected validation errors, lanes, repos, templates."""

from __future__ import annotations

from pathlib import Path

import pytest

from ephor import board
from ephor.board import Assignee, Mode, Trigger
from ephor.config import config_dir
from ephor.work_items import WorkItem

_EXAMPLE = """
[board]
projects = ["DR"]
jql = "assignee = currentUser() AND (statusCategory != Done OR updated >= -7d)"
columns = ["To Do", "In Progress", { name = "Review", statuses = ["Peer Review", "QA"] }, "Done"]

[repos]
DR = "~/projects/work/aim-myt"

[flows]
tmux_session = ""
max_active_agents = 4
max_runs_per_hour = 6
ticket_breaker = 3
max_event_age = "30m"
confirm_ttl = "24h"
pr_interval = "5m"
settle = "10m"

[[flow]]
name = "review"
on_enter = "Peer Review"
assignee = "me"
start = "/review-pr {ticket}"
provider = "claude"
worktree = false
mode = "confirm"

[[flow]]
name = "notify-pr"
on_pr = "opened"
command = "~/.config/ephor/flows/ping.sh {ticket} {title}"
mode = "auto"
"""


def _write(text: str) -> Path:
    path = config_dir() / "board.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _rules(body: str, header: str = '[board]\nprojects = ["DR"]\n') -> board.BoardConfig:
    config = board.load(_write(header + body))
    assert config is not None
    return config


# --- loading ---------------------------------------------------------------


def test_no_file_means_the_board_is_off() -> None:
    assert board.board_path() == config_dir() / "board.toml"
    assert board.load() is None


def test_the_documented_example_is_valid() -> None:
    _write(_EXAMPLE)
    config = board.load()
    assert config is not None
    assert config.errors == ()
    assert config.lanes_ok and config.dispatch_ok

    assert config.board.projects == ("DR",)
    assert [c.name for c in config.board.columns] == ["To Do", "In Progress", "Review", "Done"]
    assert config.board.lane_names()[-1] == board.OTHER_LANE
    assert config.repos == {"DR": "~/projects/work/aim-myt"}
    assert config.flows.max_event_age == 1800.0
    assert config.flows.confirm_ttl == 86400.0

    review = config.rule("review")
    assert review is not None
    assert review.trigger is Trigger.ON_ENTER and review.value == "Peer Review"
    assert review.assignee is Assignee.ME and review.mode is Mode.CONFIRM
    assert review.launches_agent and not review.worktree

    notify = config.rule("notify-pr")
    assert notify is not None
    assert notify.trigger is Trigger.ON_PR and notify.value == "opened"
    assert notify.mode is Mode.AUTO and not notify.launches_agent


def test_defaults_fill_an_almost_empty_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPHOR_TICKET_PROJECTS", "dr, abc")
    config = board.load(_write("[board]\n"))
    assert config is not None and config.errors == ()
    assert config.board.projects == ("ABC", "DR")
    assert config.board.jql == board.DEFAULT_JQL
    assert [c.name for c in config.board.columns] == list(board.DEFAULT_COLUMNS)
    assert config.flows == board.FlowSettings()
    assert config.rules == ()


def test_a_board_needs_a_project() -> None:
    config = board.load(_write("[board]\n"))
    assert config is not None
    assert not config.lanes_ok
    assert any("projects is empty" in e for e in config.board_errors)


def test_invalid_toml_is_an_error_not_an_exception() -> None:
    config = board.load(_write("[board\nprojects = "))
    assert config is not None
    assert not config.lanes_ok and not config.dispatch_ok
    assert "not valid TOML" in config.errors[0]


# --- the two levels of valid ----------------------------------------------


def test_a_bad_flow_stops_dispatch_but_not_the_lanes() -> None:
    config = _rules('[[flow]]\nname = "x"\non_enter = "Peer Review"\n')  # no start/command
    assert config.lanes_ok is True
    assert config.dispatch_ok is False
    assert any("exactly one of start or command" in e for e in config.flow_errors)
    assert config.rules == ()


def test_errors_are_collected_rather_than_stopping_at_the_first() -> None:
    config = _rules(
        """
[flows]
max_runs_per_hour = 0
settle = "soon"
bogus = 1

[[flow]]
name = "Bad Name"
on_enter = "A"
on_exit = "B"
start = "x"
"""
    )
    joined = "\n".join(config.flow_errors)
    assert "max_runs_per_hour" in joined
    assert "settle" in joined
    assert "'bogus'" in joined
    assert "name must be lowercase" in joined
    assert "exactly one of on_enter" in joined


def test_unknown_keys_are_errors_so_typos_surface() -> None:
    config = _rules('[[flow]]\nname = "x"\non_entr = "Peer Review"\nstart = "/x"\n')
    assert any("unknown key 'on_entr'" in e for e in config.flow_errors)
    config = _rules("", header='[board]\nprojects = ["DR"]\ncolums = []\n')
    assert any("'colums'" in e for e in config.board_errors)


def test_flows_written_as_plural_array_get_a_hint() -> None:
    config = _rules('[[flows]]\nname = "x"\n')
    assert any("[[flow]], singular" in e for e in config.flow_errors)


# --- rules -----------------------------------------------------------------


def test_agent_flows_are_confirm_only() -> None:
    config = _rules('[[flow]]\nname = "x"\non_enter = "Peer Review"\nstart = "/x"\nmode = "auto"\n')
    assert any("confirm-only" in e for e in config.flow_errors)
    assert not config.dispatch_ok


def test_command_flows_may_be_auto_but_not_take_agent_options() -> None:
    ok = _rules('[[flow]]\nname = "x"\non_pr = "merged"\ncommand = "true"\nmode = "auto"\n')
    assert ok.dispatch_ok
    bad = _rules('[[flow]]\nname = "x"\non_pr = "merged"\ncommand = "true"\nworktree = true\n')
    assert any("only to start= flows" in e for e in bad.flow_errors)


def test_an_idle_session_flow_cannot_start_an_agent() -> None:
    # The idle session is itself live on the ticket: the launch guard would
    # hold the run as long as it is current, and it supersedes once it isn't.
    bad = _rules('[[flow]]\nname = "nudge"\non_session = "IDLE"\nstart = "/continue {ticket}"\n')
    assert any('on_session = "idle" cannot use start=' in e for e in bad.flow_errors)
    assert not bad.dispatch_ok


def test_idle_command_flows_and_died_start_flows_still_validate() -> None:
    idle = _rules('[[flow]]\nname = "nudge"\non_session = "idle"\ncommand = "true"\n')
    assert idle.dispatch_ok, idle.flow_errors
    died = _rules('[[flow]]\nname = "redo"\non_session = "died"\nstart = "/continue {ticket}"\n')
    assert died.dispatch_ok, died.flow_errors


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('on_pr = "pushed"', "on_pr must be one of"),
        ('on_session = "finished"', "on_session must be one of"),
        ('on_enter = ""', "non-empty string"),
        ("on_enter = 3", "non-empty string"),
    ],
)
def test_trigger_values_are_checked(line: str, message: str) -> None:
    config = _rules(f'[[flow]]\nname = "x"\n{line}\nstart = "/x"\n')
    assert any(message in e for e in config.flow_errors)


def test_enum_fields_and_provider_are_checked() -> None:
    config = _rules(
        '[[flow]]\nname = "x"\non_enter = "A"\nstart = "/x"\n'
        'assignee = "them"\nmode = "sometimes"\nprovider = "notanagent"\n'
    )
    joined = "\n".join(config.flow_errors)
    assert "assignee must be one of me, any" in joined
    assert "mode must be one of confirm, auto" in joined
    assert "unknown provider 'notanagent'" in joined


def test_flow_names_are_unique() -> None:
    rule = '[[flow]]\nname = "x"\non_enter = "A"\nstart = "/x"\n'
    config = _rules(rule + rule)
    assert any("duplicate flow name" in e for e in config.flow_errors)


def test_a_single_flow_table_is_accepted() -> None:
    config = _rules('[flow]\nname = "x"\non_enter = "A"\nstart = "/x"\n')
    assert config.dispatch_ok and config.rule("x") is not None


def test_status_matching_is_case_insensitive() -> None:
    config = _rules('[[flow]]\nname = "x"\non_enter = "peer review"\nstart = "/x"\n')
    rule = config.rule("x")
    assert rule is not None
    assert rule.matches_status("Peer Review")
    assert rule.matches_status(" PEER REVIEW ")
    assert not rule.matches_status("Peer Reviewed")


def test_fingerprint_tracks_what_a_rule_matches_not_what_it_runs() -> None:
    a = board.FlowRule(name="r", trigger=Trigger.ON_ENTER, value="Peer Review", start="/a")
    b = board.FlowRule(name="r", trigger=Trigger.ON_ENTER, value="peer review", start="/b")
    c = board.FlowRule(name="r", trigger=Trigger.ON_ENTER, value="QA", start="/a")
    d = board.FlowRule(
        name="r", trigger=Trigger.ON_ENTER, value="Peer Review", start="/a", assignee=Assignee.ANY
    )
    assert a.fingerprint == b.fingerprint
    assert len({a.fingerprint, c.fingerprint, d.fingerprint}) == 3


# --- templates -------------------------------------------------------------


def test_templates_accept_only_the_three_fields() -> None:
    for template in ("/x {ticket.__class__}", "/x {nope}", "/x {ticket!r}", "/x {ticket:>10}"):
        config = _rules(f'[[flow]]\nname = "x"\non_enter = "A"\nstart = \'{template}\'\n')
        assert any("placeholder" in e for e in config.flow_errors), template
    config = _rules('[[flow]]\nname = "x"\non_enter = "A"\nstart = \'/x {unclosed\'\n')
    assert any("bad template" in e for e in config.flow_errors)
    ok = _rules('[[flow]]\nname = "x"\non_enter = "A"\ncommand = \'echo {{literal}} {ticket}\'\n')
    assert ok.dispatch_ok


def test_start_templates_render_verbatim() -> None:
    rule = board.FlowRule(
        name="r", trigger=Trigger.ON_ENTER, value="A", start="/review-pr {ticket} ({status})"
    )
    assert rule.render(ticket="DR-1", status="Peer Review") == "/review-pr DR-1 (Peer Review)"


def test_command_templates_shell_quote_every_value() -> None:
    """A ticket title is text other people wrote; it must stay one argument."""
    rule = board.FlowRule(
        name="r", trigger=Trigger.ON_PR, value="opened", command="notify {ticket} {title}"
    )
    rendered = rule.render(ticket="DR-1", title="x; rm -rf ~ $(id)")
    assert rendered == "notify DR-1 'x; rm -rf ~ $(id)'"
    assert rule.render(ticket="DR-1", title="") == "notify DR-1 ''"


# --- lanes and JQL ---------------------------------------------------------


def test_columns_map_statuses_case_insensitively_with_an_other_lane() -> None:
    config = board.load(_write(_EXAMPLE))
    assert config is not None
    settings = config.board
    assert settings.column_for("peer review") == "Review"
    assert settings.column_for("QA") == "Review"
    assert settings.column_for("In Progress") == "In Progress"
    assert settings.column_for("Blocked") == board.OTHER_LANE
    assert settings.column_for("") == board.OTHER_LANE


def test_column_validation() -> None:
    config = _rules(
        "",
        header=(
            '[board]\nprojects = ["DR"]\n'
            'columns = ["A", "a", "Other", "unticketed", { name = "B", statuses = ["A"] }, '
            '{ name = "" }, 3, { name = "C", statuses = "x" }]\n'
        ),
    )
    joined = "\n".join(config.board_errors)
    assert "duplicate column 'a'" in joined
    assert "'Other' is reserved" in joined
    assert "'Unticketed' is reserved" in joined
    assert "status 'a' is already in column 'A'" in joined
    assert "column has no name" in joined
    assert "must be a status name" in joined
    assert "statuses must be a list" in joined
    assert not config.lanes_ok


def test_search_jql_is_always_project_scoped() -> None:
    settings = board.BoardSettings(projects=("DR", "ABC"), jql="assignee = currentUser()")
    assert settings.search_jql() == "project in (DR, ABC) AND (assignee = currentUser())"
    assert board.BoardSettings(projects=("DR",), jql="  ").search_jql() == "project in (DR)"


def test_project_keys_are_validated() -> None:
    config = _rules("", header='[board]\nprojects = ["DR", "not a key) OR (1=1"]\n')
    assert any("not a Jira project key" in e for e in config.board_errors)
    config = _rules('[repos]\n"bad key" = "/x"\nDR = ""\n')
    joined = "\n".join(config.flow_errors)
    assert "'bad key' is not a Jira project key" in joined
    assert "[repos] DR: must be a path" in joined


# --- flows settings and durations -----------------------------------------


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("90s", 90.0),
        ("30m", 1800.0),
        ("24h", 86400.0),
        ("1h30m", 5400.0),
        ("2d", 172800.0),
        (" 1H 5M ", 3900.0),
        ("0.5h", 1800.0),
        (45, 45.0),
        (1.5, 1.5),
    ],
)
def test_parse_duration_accepts(raw: object, seconds: float) -> None:
    assert board.parse_duration(raw) == seconds


@pytest.mark.parametrize("raw", ["", "soon", "10", "5x", "m5", "0m", 0, -3, True, None, [1]])
def test_parse_duration_rejects(raw: object) -> None:
    assert board.parse_duration(raw) is None


def test_tmux_session_must_be_addressable() -> None:
    config = _rules('[flows]\ntmux_session = "work:1"\n')
    assert any("tmux_session" in e for e in config.flow_errors)
    config = _rules('[flows]\ntmux_session = "work"\n')
    assert config.flows.tmux_session == "work"


# --- repo resolution -------------------------------------------------------


def test_resolve_repo_prefers_the_worktree_then_the_project_map(tmp_path: Path) -> None:
    worktree = tmp_path / "repo-DR-1"
    worktree.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    config = board.BoardConfig(path=tmp_path / "b.toml", repos={"DR": str(repo)})

    path, how = config.resolve_repo("DR-1", WorkItem(key="DR-1", worktree=str(worktree)))
    assert (path, how) == (worktree, "worktree")

    path, how = config.resolve_repo("DR-1", WorkItem(key="DR-1"))
    assert (path, how) == (repo, "repos.DR")

    # A worktree that has since been removed falls through to the map.
    gone = WorkItem(key="DR-1", worktree=str(tmp_path / "deleted"))
    assert config.resolve_repo("DR-1", gone) == (repo, "repos.DR")


def test_resolve_repo_refuses_rather_than_guessing(tmp_path: Path) -> None:
    config = board.BoardConfig(path=tmp_path / "b.toml", repos={"DR": str(tmp_path / "nope")})
    path, how = config.resolve_repo("DR-1")
    assert path is None and how.startswith("no repo")
    path, how = config.resolve_repo("ABC-1")
    assert (path, how) == (None, "no repo")


def test_resolve_repo_expands_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "code").mkdir()
    config = board.BoardConfig(path=tmp_path / "b.toml", repos={"DR": "~/code"})
    assert config.resolve_repo("dr-7") == (tmp_path / "code", "repos.DR")


# --- template safety -------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        'echo "{title}"',
        "echo '{title}'",
        "notify --x={title}",
        "notify {title};ls",
        "echo $(cat {title})",
        'echo "a {ticket} b"',
        "echo `{status}`",
    ],
)
def test_command_placeholders_must_be_bare_shell_words(template: str) -> None:
    """Quoting a placeholder undoes the shell-quoting render() adds."""
    config = _rules(
        f"[[flow]]\nname = \"x\"\non_pr = \"opened\"\ncommand = '''{template}'''\nmode = \"auto\"\n"
    )
    assert any("must be a separate, unquoted word" in e for e in config.flow_errors), template
    assert any("$EPHOR_FLOW_TITLE" in e for e in config.flow_errors)
    assert config.rule("x") is None
    assert not config.dispatch_ok


def test_unbalanced_quotes_in_a_command_are_an_error() -> None:
    config = _rules(
        "[[flow]]\nname = \"x\"\non_pr = \"opened\"\ncommand = '''echo \"oops {ticket}'''\n"
    )
    assert any("not valid shell quoting" in e for e in config.flow_errors)


def test_bare_command_placeholders_are_accepted_and_stay_inert(tmp_path: Path) -> None:
    import subprocess

    config = _rules(
        '[[flow]]\nname = "x"\non_pr = "opened"\ncommand = "notify.sh {ticket} {title}"\n'
    )
    assert config.dispatch_ok, config.errors
    rule = config.rule("x")
    assert rule is not None
    marker = tmp_path / "pwned"
    for title in (f"$(touch {marker})", f"x; touch {marker}", f"`touch {marker}`"):
        rendered = rule.render(ticket="DR-1", title=title)
        subprocess.run(["sh", "-c", rendered], cwd=tmp_path, capture_output=True, check=False)
        assert not marker.exists(), rendered


@pytest.mark.parametrize("template", ["{title}", "{title} please", "  {status}", "-x {ticket}"])
def test_start_prompts_must_begin_with_literal_text(template: str) -> None:
    """A title like --dangerously-skip-permissions must never be the agent's first word."""
    config = _rules(f'[[flow]]\nname = "x"\non_enter = "A"\nstart = "{template}"\n')
    assert any("start= must begin with literal text" in e for e in config.flow_errors), template
    assert config.rule("x") is None


def test_start_prompt_with_a_leading_command_is_accepted() -> None:
    config = _rules('[[flow]]\nname = "x"\non_enter = "A"\nstart = "/review-pr {ticket}"\n')
    assert config.dispatch_ok, config.errors
    assert config.rule("x") is not None


# --- ORDER BY --------------------------------------------------------------


def test_search_jql_lifts_a_trailing_order_by_out_of_the_parentheses() -> None:
    settings = board.BoardSettings(
        projects=("DR",), jql="assignee = currentUser() ORDER BY updated DESC"
    )
    assert settings.search_jql() == (
        "project in (DR) AND (assignee = currentUser()) ORDER BY updated DESC"
    )
    lower = board.BoardSettings(projects=("DR",), jql="status = QA order by rank")
    assert lower.search_jql() == "project in (DR) AND (status = QA) ORDER BY rank"
    alone = board.BoardSettings(projects=("DR",), jql="ORDER BY rank")
    assert alone.search_jql() == "project in (DR) ORDER BY rank"


def test_an_order_by_with_no_fields_is_a_board_error() -> None:
    config = _rules("", header='[board]\nprojects = ["DR"]\njql = "status = QA ORDER BY"\n')
    assert "[board] jql: ORDER BY has no field list" in config.board_errors
    ok = _rules("", header='[board]\nprojects = ["DR"]\njql = "status = QA ORDER BY rank"\n')
    assert ok.lanes_ok, ok.board_errors


def test_the_documented_example_board_is_valid() -> None:
    example = Path(__file__).resolve().parent.parent / "docs" / "examples" / "board.toml"
    config = board.load(example)
    assert config is not None
    assert config.errors == ()
    assert {r.trigger for r in config.rules} == {
        board.Trigger.ON_ENTER,
        board.Trigger.ON_PR,
        board.Trigger.ON_SESSION,
    }


def test_every_toml_snippet_in_the_flows_guide_is_valid() -> None:
    import re
    import tomllib

    guide = Path(__file__).resolve().parent.parent / "docs" / "flows.md"
    blocks = re.findall(r"```toml\n(.*?)```", guide.read_text(), re.DOTALL)
    assert len(blocks) >= 5
    for block in blocks:
        data = tomllib.loads(block)
        data.setdefault("board", {"projects": ["DR"]})
        assert board.parse(data).errors == (), block

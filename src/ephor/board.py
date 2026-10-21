"""``board.toml``: which tickets make up the board, and what moving them should do.

The dashboard has always been keyed on sessions. The board adds the other
half of the picture — the tickets you own, laid out in the lanes of your Jira
workflow whether or not anything is running on them — and *flows*: small
rules of the form "when a ticket enters Peer Review, offer to start
``/review-pr`` on it".

This module is only the configuration. It reads ``config_dir()/board.toml``,
validates every part of it, and answers the pure questions the rest of the
board asks: which lane a status belongs in, which repo a ticket's flow runs
in, how a flow's command line is rendered. It talks to no network, starts
nothing and writes nothing; the flows tick (:mod:`ephor.flows`) does those
things with what this returns.

**Errors are collected, not raised.** A config file is edited by hand, and
one typo should produce a list the CLI and the TUI can show — not a
traceback, and not a board that silently drops the half it could not parse.
Two levels of "valid" fall out of that:

* the ``[board]`` table alone decides whether lanes can render, so a broken
  flow rule never hides your tickets;
* *any* error anywhere means nothing is dispatched. A flow rule that was
  half-understood is exactly the rule that launches an agent you did not ask
  for.

**Agent-launching flows are confirm-only.** ``mode = "auto"`` is accepted on
a ``command =`` flow and rejected on a ``start =`` flow. Your own skills move
tickets through Jira with your own token, so nothing ephor can see tells your
move from an agent's — and an automatic agent launch keyed on an agent's own
move is how one ticket becomes six sessions.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import string
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from ephor.config import config_dir, ticket_projects
from ephor.launcher import valid_tmux_session
from ephor.providers import PROVIDERS
from ephor.work_items import WorkItem

BOARD_FILE = "board.toml"

# The lane for a ticket whose status no column claims. Always drawn after the
# configured columns, so a newly-added workflow status is visible (in Other)
# the day it appears rather than vanishing until someone edits the config.
OTHER_LANE = "Other"
# The TUI's lane for sessions bound to no ticket at all. Not a Jira status
# lane, but it shares the fold/grouping namespace, so no column may take it.
UNTICKETED_LANE = "Unticketed"

DEFAULT_JQL = "assignee = currentUser() AND (statusCategory != Done OR updated >= -7d)"
DEFAULT_COLUMNS: tuple[str, ...] = ("To Do", "In Progress", "Done")

# Jira project keys: a letter, then letters/digits, two to ten characters —
# the same shape :mod:`ephor.launcher` accepts in a ticket key. A key that
# passes this needs no quoting inside JQL.
_PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
_FLOW_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)([smhd])")
_DURATION_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

# The only names a ``start =`` / ``command =`` template may interpolate.
TEMPLATE_FIELDS = frozenset({"ticket", "title", "status"})


class Trigger(StrEnum):
    """What a flow watches. Exactly one per flow."""

    ON_ENTER = "on_enter"  # ticket's status became X
    ON_EXIT = "on_exit"  # ticket's status stopped being X
    ON_PR = "on_pr"  # opened | merged
    ON_SESSION = "on_session"  # died | idle


PR_EVENTS = frozenset({"opened", "merged"})
SESSION_EVENTS = frozenset({"died", "idle"})


class Mode(StrEnum):
    CONFIRM = "confirm"  # a run waits in awaiting_confirm until approved
    AUTO = "auto"  # a run is claimed and dispatched on the tick that made it


class Assignee(StrEnum):
    ME = "me"  # only tickets assigned to the configured Jira user
    ANY = "any"


@dataclass(frozen=True)
class Column:
    """One lane: a display name and the status names it gathers (lowercased)."""

    name: str
    statuses: tuple[str, ...]


@dataclass(frozen=True)
class BoardSettings:
    """The ``[board]`` table: which tickets, and in which lanes."""

    projects: tuple[str, ...] = ()
    jql: str = DEFAULT_JQL
    columns: tuple[Column, ...] = tuple(Column(n, (n.lower(),)) for n in DEFAULT_COLUMNS)

    def search_jql(self) -> str:
        """The board's JQL, scoped to its projects.

        The project clause is ANDed on by ephor rather than left to the user's
        ``jql``, so a board can never accidentally become a search of the
        whole site.
        """
        scope = f"project in ({', '.join(self.projects)})"
        where, order = split_order_by(self.jql)
        # JQL forbids ORDER BY inside parentheses, and a query pasted from a
        # saved filter usually ends in one — so it is lifted out of the
        # wrapped clause and re-attached after the scope.
        query = f"{scope} AND ({where})" if where else scope
        return f"{query} ORDER BY {order}" if order else query

    def column_for(self, status: str) -> str:
        """The lane ``status`` belongs in — matched case-insensitively — or Other."""
        wanted = (status or "").strip().lower()
        if wanted:
            for column in self.columns:
                if wanted in column.statuses:
                    return column.name
        return OTHER_LANE

    def lane_names(self) -> tuple[str, ...]:
        """Every lane in display order: the configured columns, then Other."""
        return (*(c.name for c in self.columns), OTHER_LANE)


@dataclass(frozen=True)
class FlowSettings:
    """The ``[flows]`` table: guard rails shared by every rule. Durations in seconds."""

    tmux_session: str = ""
    max_active_agents: int = 4
    max_runs_per_hour: int = 6
    ticket_breaker: int = 3
    max_event_age: float = 30 * 60.0
    confirm_ttl: float = 24 * 3600.0
    pr_interval: float = 5 * 60.0
    settle: float = 10 * 60.0


@dataclass(frozen=True)
class FlowRule:
    """One ``[[flow]]``: a trigger and the thing to run when it fires."""

    name: str
    trigger: Trigger
    value: str  # the status name, PR event or session event the trigger names
    start: str = ""
    command: str = ""
    assignee: Assignee = Assignee.ME
    provider: str = "claude"
    worktree: bool = False
    mode: Mode = Mode.CONFIRM

    @property
    def launches_agent(self) -> bool:
        """True for a ``start =`` flow, which opens a coding-agent session."""
        return bool(self.start)

    @property
    def template(self) -> str:
        return self.start or self.command

    def matches_status(self, status: str) -> bool:
        """True when ``status`` is the one an on_enter/on_exit rule names."""
        return (status or "").strip().lower() == self.value.strip().lower()

    @property
    def fingerprint(self) -> str:
        """Identity of *which tickets this rule fires on*, for adopt mode.

        A rule whose fingerprint the snapshot has not seen is new, and a new
        rule adopts the board as it stands rather than firing on every ticket
        already sitting in its status. The fingerprint covers the name and
        the matching half of the rule only — trigger, value, assignee — so
        editing what a rule *runs* does not re-adopt, while pointing it at a
        different status does.
        """
        canonical = json.dumps(
            [self.name, str(self.trigger), self.value.strip().lower(), str(self.assignee)],
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def render(self, *, ticket: str, title: str = "", status: str = "") -> str:
        """The rule's template with ``{ticket}``, ``{title}`` and ``{status}`` filled in.

        For a ``command =`` flow every value is shell-quoted: a ticket title
        is text anyone with edit rights on the ticket chose, and it must
        arrive in the command as one argument, never as shell syntax. That
        is only sound because validation has already made sure every
        placeholder stands alone as an unquoted shell word (see
        :func:`_command_placeholder_problems`); an author who wants the
        value inside a larger string uses the environment instead — the
        runner exports ``EPHOR_FLOW_TITLE`` and ``EPHOR_FLOW_STATUS`` beside
        ``EPHOR_FLOW_PAYLOAD``, so ``"$EPHOR_FLOW_TITLE"`` is always safe.
        A ``start =`` prompt is quoted as a whole by the launcher instead,
        and validation makes it begin with literal text, so ticket text can
        never be read as a command-line option.
        """
        values = {"ticket": ticket, "title": title, "status": status}
        if self.command:
            values = {k: shlex.quote(v) for k, v in values.items()}
        return self.template.format_map(values)


@dataclass(frozen=True)
class BoardConfig:
    """Everything ``board.toml`` says, plus everything wrong with it."""

    path: Path
    board: BoardSettings = field(default_factory=BoardSettings)
    repos: Mapping[str, str] = field(default_factory=dict)
    flows: FlowSettings = field(default_factory=FlowSettings)
    rules: tuple[FlowRule, ...] = ()
    # Problems in [board] (lanes cannot render) and everywhere else (nothing
    # dispatches). Human-readable, one per problem, each naming where it is.
    board_errors: tuple[str, ...] = ()
    flow_errors: tuple[str, ...] = ()

    @property
    def errors(self) -> tuple[str, ...]:
        return (*self.board_errors, *self.flow_errors)

    @property
    def lanes_ok(self) -> bool:
        """True when the board can be drawn — ``[board]`` itself is sound."""
        return not self.board_errors

    @property
    def dispatch_ok(self) -> bool:
        """True when flows may run — the whole file is sound."""
        return not self.errors

    def rule(self, name: str) -> FlowRule | None:
        return next((r for r in self.rules if r.name == name), None)

    def resolve_repo(self, ticket: str, work: WorkItem | None = None) -> tuple[Path | None, str]:
        """Where a flow for ``ticket`` runs. Returns (path, how it was chosen).

        The ticket's own worktree wins — it is where its commits already are.
        Next is the ``[repos]`` entry for the ticket's project. A path that
        no longer exists is skipped rather than handed to tmux, which would
        open the window in ``$HOME`` and run the flow in the wrong tree. With
        neither, the answer is None and the reason is ``"no repo"``: a flow
        has nowhere sensible to run, and guessing is how an agent ends up
        editing an unrelated checkout.
        """
        if work is not None and work.worktree:
            path = Path(work.worktree).expanduser()
            if path.is_dir():
                return path, "worktree"
        project = (ticket or "").partition("-")[0].upper()
        configured = self.repos.get(project)
        if configured:
            path = Path(configured).expanduser()
            if path.is_dir():
                return path, f"repos.{project}"
            return None, f"no repo: repos.{project} = {configured} does not exist"
        return None, "no repo"


_ORDER_BY_RE = re.compile(r"(?is)^(.*?)\s*\border\s+by\b(.*)$")


def split_order_by(jql: str) -> tuple[str, str | None]:
    """``jql`` split into (filter, ORDER BY field list).

    The second half is None when there is no ORDER BY at all, and "" when
    there is one with nothing after it — which Jira rejects, so the
    validator reports it.
    """
    text = (jql or "").strip()
    match = _ORDER_BY_RE.match(text)
    if match is None:
        return text, None
    return match.group(1).strip(), match.group(2).strip()


def board_path() -> Path:
    """Where ``board.toml`` lives (``~/.config/ephor/board.toml``)."""
    return config_dir() / BOARD_FILE


def parse_duration(raw: object) -> float | None:
    """Seconds in ``raw`` — ``"90s"``, ``"30m"``, ``"1h30m"``, ``"2d"`` or a number.

    None for anything else, including zero and negatives: every duration in
    the config is a window or an interval, and a zero-length one is a typo.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw) if raw > 0 else None
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower().replace(" ", "")
    if not text:
        return None
    total = 0.0
    pos = 0
    for match in _DURATION_PART.finditer(text):
        if match.start() != pos:
            return None
        total += float(match.group(1)) * _DURATION_UNITS[match.group(2)]
        pos = match.end()
    if pos != len(text) or total <= 0:
        return None
    return total


def load(path: Path | None = None) -> BoardConfig | None:
    """Read and validate ``board.toml``. None when there is no file at all.

    No file means the board is switched off — the normal state for anyone
    who has not opted in — and is not an error. A file that exists but does
    not parse is a config whose every part is in error.
    """
    target = path or board_path()
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return BoardConfig(path=target, board_errors=(f"{target}: unreadable ({exc})",))
    try:
        data = tomllib.loads(raw.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return BoardConfig(path=target, board_errors=(f"{target}: not valid TOML ({exc})",))
    return parse(data, path=target)


def parse(data: Mapping[str, Any], *, path: Path | None = None) -> BoardConfig:
    """Validate an already-decoded ``board.toml`` document."""
    board_errors: list[str] = []
    flow_errors: list[str] = []

    for key in data:
        if key not in {"board", "repos", "flows", "flow"}:
            flow_errors.append(f"unknown top-level key {key!r}")

    board = _parse_board(data.get("board", {}), board_errors)
    repos = _parse_repos(data.get("repos", {}), flow_errors)
    flows = _parse_flow_settings(data.get("flows", {}), flow_errors)
    rules = _parse_rules(data.get("flow", []), flow_errors)

    return BoardConfig(
        path=path or board_path(),
        board=board,
        repos=repos,
        flows=flows,
        rules=rules,
        board_errors=tuple(board_errors),
        flow_errors=tuple(flow_errors),
    )


def _parse_board(raw: object, errors: list[str]) -> BoardSettings:
    if not isinstance(raw, dict):
        errors.append("[board] must be a table")
        return BoardSettings()
    for key in raw:
        if key not in {"projects", "jql", "columns"}:
            errors.append(f"[board]: unknown key {key!r}")

    projects: tuple[str, ...]
    if "projects" in raw:
        value = raw["projects"]
        if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
            errors.append("[board] projects must be a list of project keys")
            projects = ()
        else:
            projects = tuple(dict.fromkeys(p.strip().upper() for p in value if p.strip()))
    else:
        projects = tuple(sorted(ticket_projects()))
    for project in projects:
        if not _PROJECT_RE.match(project):
            errors.append(f"[board] projects: {project!r} is not a Jira project key")
    if not projects:
        errors.append(
            "[board] projects is empty and $EPHOR_TICKET_PROJECTS is unset — "
            "the board needs at least one project to scope its search"
        )

    jql = raw.get("jql", DEFAULT_JQL)
    if not isinstance(jql, str):
        errors.append("[board] jql must be a string")
        jql = DEFAULT_JQL
    elif split_order_by(jql)[1] == "":
        errors.append("[board] jql: ORDER BY has no field list")

    columns = _parse_columns(raw.get("columns", list(DEFAULT_COLUMNS)), errors)
    return BoardSettings(projects=projects, jql=jql, columns=columns)


def _parse_columns(raw: object, errors: list[str]) -> tuple[Column, ...]:
    if not isinstance(raw, list) or not raw:
        errors.append("[board] columns must be a non-empty list")
        return BoardSettings().columns
    columns: list[Column] = []
    names: set[str] = set()
    claimed: dict[str, str] = {}
    for index, entry in enumerate(raw):
        where = f"[board] columns[{index}]"
        if isinstance(entry, str):
            name, statuses = entry.strip(), [entry]
        elif isinstance(entry, dict):
            extra = set(entry) - {"name", "statuses"}
            if extra:
                errors.append(f"{where}: unknown key(s) {', '.join(sorted(extra))}")
            name = str(entry.get("name") or "").strip()
            raw_statuses = entry.get("statuses", [name])
            if not isinstance(raw_statuses, list) or not all(
                isinstance(s, str) for s in raw_statuses
            ):
                errors.append(f"{where}: statuses must be a list of status names")
                continue
            statuses = raw_statuses
        else:
            errors.append(f"{where}: must be a status name or {{ name, statuses }}")
            continue
        if not name:
            errors.append(f"{where}: column has no name")
            continue
        if name.lower() == OTHER_LANE.lower():
            errors.append(f"{where}: {OTHER_LANE!r} is reserved for unmatched statuses")
            continue
        if name.lower() == UNTICKETED_LANE.lower():
            errors.append(f"{where}: {UNTICKETED_LANE!r} is reserved for sessions with no ticket")
            continue
        if name.lower() in names:
            errors.append(f"{where}: duplicate column {name!r}")
            continue
        names.add(name.lower())
        normalized = tuple(dict.fromkeys(s.strip().lower() for s in statuses if s.strip()))
        if not normalized:
            errors.append(f"{where}: column {name!r} matches no statuses")
            continue
        for status in normalized:
            owner = claimed.get(status)
            if owner is not None:
                errors.append(f"{where}: status {status!r} is already in column {owner!r}")
            else:
                claimed[status] = name
        columns.append(Column(name=name, statuses=normalized))
    return tuple(columns) or BoardSettings().columns


def _parse_repos(raw: object, errors: list[str]) -> dict[str, str]:
    if not isinstance(raw, dict):
        errors.append("[repos] must be a table of PROJECT = path")
        return {}
    repos: dict[str, str] = {}
    for key, value in raw.items():
        project = str(key).strip().upper()
        if not _PROJECT_RE.match(project):
            errors.append(f"[repos]: {key!r} is not a Jira project key")
            continue
        if not isinstance(value, str) or not value.strip():
            errors.append(f"[repos] {project}: must be a path")
            continue
        repos[project] = value.strip()
    return repos


_FLOW_INT_FIELDS = ("max_active_agents", "max_runs_per_hour", "ticket_breaker")
_FLOW_DURATION_FIELDS = ("max_event_age", "confirm_ttl", "pr_interval", "settle")


def _parse_flow_settings(raw: object, errors: list[str]) -> FlowSettings:
    if not isinstance(raw, dict):
        errors.append("[flows] must be a table (flow rules are [[flow]], singular)")
        return FlowSettings()
    known = {"tmux_session", *_FLOW_INT_FIELDS, *_FLOW_DURATION_FIELDS}
    for key in raw:
        if key not in known:
            errors.append(f"[flows]: unknown key {key!r}")
    defaults = FlowSettings()
    values: dict[str, Any] = {}

    session = raw.get("tmux_session", "")
    if not isinstance(session, str):
        errors.append("[flows] tmux_session must be a string")
    elif session.strip() and not valid_tmux_session(session.strip()):
        errors.append(f"[flows] tmux_session {session!r} is not a usable tmux session name")
    else:
        values["tmux_session"] = session.strip()

    for name in _FLOW_INT_FIELDS:
        if name not in raw:
            continue
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            errors.append(f"[flows] {name} must be a whole number of at least 1")
        else:
            values[name] = value

    for name in _FLOW_DURATION_FIELDS:
        if name not in raw:
            continue
        seconds = parse_duration(raw[name])
        if seconds is None:
            errors.append(
                f'[flows] {name} = {raw[name]!r} is not a duration (e.g. "90s", "30m", "24h")'
            )
        else:
            values[name] = seconds

    return replace(defaults, **values)


_E = TypeVar("_E", bound=StrEnum)

_RULE_KEYS = frozenset(
    {"name", "start", "command", "assignee", "provider", "worktree", "mode", *map(str, Trigger)}
)


def _parse_rules(raw: object, errors: list[str]) -> tuple[FlowRule, ...]:
    if isinstance(raw, dict):
        # `[flow]` instead of `[[flow]]` — one rule written as a table.
        raw = [raw]
    if not isinstance(raw, list):
        errors.append("flow rules must be [[flow]] tables")
        return ()
    rules: list[FlowRule] = []
    names: set[str] = set()
    for index, entry in enumerate(raw):
        rule = _parse_rule(index, entry, errors)
        if rule is None:
            continue
        if rule.name in names:
            errors.append(f"[[flow]] {rule.name}: duplicate flow name")
            continue
        names.add(rule.name)
        rules.append(rule)
    return tuple(rules)


def _parse_rule(index: int, entry: object, errors: list[str]) -> FlowRule | None:
    if not isinstance(entry, dict):
        errors.append(f"[[flow]] #{index + 1}: must be a table")
        return None
    name = entry.get("name")
    where = f"[[flow]] {name}" if isinstance(name, str) and name else f"[[flow]] #{index + 1}"
    count = len(errors)

    for key in entry:
        if key not in _RULE_KEYS:
            errors.append(f"{where}: unknown key {key!r}")

    if not isinstance(name, str) or not _FLOW_NAME_RE.match(name):
        errors.append(f"{where}: name must be lowercase letters, digits and hyphens")

    triggers = [t for t in Trigger if str(t) in entry]
    trigger: Trigger | None = None
    value = ""
    if len(triggers) != 1:
        errors.append(f"{where}: needs exactly one of on_enter, on_exit, on_pr, on_session")
    else:
        trigger = triggers[0]
        raw_value = entry[str(trigger)]
        value = raw_value.strip() if isinstance(raw_value, str) else ""
        if not value:
            errors.append(f"{where}: {trigger} must be a non-empty string")
        elif trigger is Trigger.ON_PR and value.lower() not in PR_EVENTS:
            errors.append(f"{where}: on_pr must be one of {', '.join(sorted(PR_EVENTS))}")
        elif trigger is Trigger.ON_SESSION and value.lower() not in SESSION_EVENTS:
            errors.append(f"{where}: on_session must be one of {', '.join(sorted(SESSION_EVENTS))}")
        if trigger in (Trigger.ON_PR, Trigger.ON_SESSION):
            value = value.lower()

    start = entry.get("start", "")
    command = entry.get("command", "")
    if not isinstance(start, str) or not isinstance(command, str):
        errors.append(f"{where}: start and command must be strings")
        start, command = "", ""
    start, command = start.strip(), command.strip()
    if bool(start) == bool(command):
        errors.append(f"{where}: needs exactly one of start or command")
    start_problem = _template_problem(start)
    command_problem = _template_problem(command)
    for problem in (start_problem, command_problem):
        if problem:
            errors.append(f"{where}: {problem}")
    if start and not start_problem and _starts_with_placeholder_or_dash(start):
        errors.append(
            f"{where}: start= must begin with literal text "
            "(e.g. /review-pr {ticket}), not a placeholder or '-'"
        )
    if command and not command_problem:
        errors.extend(f"{where}: {p}" for p in _command_placeholder_problems(command))

    assignee = _enum_field(entry, "assignee", Assignee, Assignee.ME, where, errors)
    mode = _enum_field(entry, "mode", Mode, Mode.CONFIRM, where, errors)

    provider = entry.get("provider", "claude")
    worktree = entry.get("worktree", False)
    if command and ("provider" in entry or "worktree" in entry):
        errors.append(f"{where}: provider and worktree apply only to start= flows")
    if not isinstance(provider, str) or provider not in PROVIDERS:
        errors.append(f"{where}: unknown provider {provider!r}")
        provider = "claude"
    if not isinstance(worktree, bool):
        errors.append(f"{where}: worktree must be true or false")
        worktree = False
    if start and mode is Mode.AUTO:
        errors.append(
            f'{where}: mode = "auto" is not allowed on a start= flow — '
            "agent-launching flows are confirm-only"
        )
    if start and trigger is Trigger.ON_SESSION and value == "idle":
        errors.append(
            f'{where}: on_session = "idle" cannot use start= — the idle session is itself '
            "live on the ticket, so the launch guard would hold the run until it ends; "
            'use command= (e.g. to message the session) or on_session = "died"'
        )

    if len(errors) != count or trigger is None or not isinstance(name, str):
        return None
    return FlowRule(
        name=name,
        trigger=trigger,
        value=value,
        start=start,
        command=command,
        assignee=assignee,
        provider=provider,
        worktree=worktree,
        mode=mode,
    )


def _enum_field(
    entry: dict[str, Any],
    key: str,
    kind: type[_E],
    default: _E,
    where: str,
    errors: list[str],
) -> _E:
    raw = entry.get(key, str(default))
    try:
        return kind(str(raw).strip().lower()) if isinstance(raw, str) else kind(raw)
    except ValueError:
        allowed = ", ".join(str(member) for member in kind)
        errors.append(f"{where}: {key} must be one of {allowed}")
        return default


def _template_problem(template: str) -> str:
    """Why ``template`` cannot be rendered, or "" when it can.

    Only bare ``{ticket}``, ``{title}`` and ``{status}`` are allowed. Format
    specs, attribute access and indexing are rejected: they are never what a
    flow author meant, and ``{ticket.__class__}`` is not something a config
    file should be able to evaluate.
    """
    if not template:
        return ""
    try:
        parts = list(string.Formatter().parse(template))
    except ValueError as exc:
        return f"bad template {template!r} ({exc}; write {{{{ and }}}} for literal braces)"
    for _, field_name, spec, conversion in parts:
        if field_name is None:
            continue
        if field_name not in TEMPLATE_FIELDS or spec or conversion:
            return (
                f"template placeholder {{{field_name}}} is not one of "
                "{ticket}, {title}, {status} (write {{ and }} for literal braces)"
            )
    return ""


def _command_placeholder_problems(template: str) -> list[str]:
    """Placeholders in a ``command =`` template that are not whole shell words.

    :meth:`FlowRule.render` shell-quotes each value, which is only safe when
    the placeholder *is* a word. Inside double quotes the added single quotes
    are literal characters and ``$(...)`` still expands; inside single quotes
    they cancel out and the value is bare shell; glued to other characters
    (``--x={title}``, ``{title};ls``, ``$(cat {title})``) the shell decides
    where the word ends. Each placeholder is swapped for a unique sentinel and
    the result tokenised with quotes kept (``posix=False``): a placeholder is
    acceptable only when its sentinel comes back as a token on its own.
    """
    if not template:
        return []
    try:
        parts = list(string.Formatter().parse(template))
    except ValueError:
        return []  # _template_problem already reported it
    rendered: list[str] = []
    slots: list[tuple[str, str]] = []
    for literal, field_name, _, _ in parts:
        rendered.append(literal)
        if field_name is not None:
            sentinel = f"EPHORSLOT{len(slots)}"
            slots.append((sentinel, field_name))
            rendered.append(sentinel)
    if not slots:
        return []
    try:
        tokens = set(shlex.split("".join(rendered), posix=False))
    except ValueError as exc:
        return [f"command= is not valid shell quoting ({exc})"]
    problems: list[str] = []
    for sentinel, name in slots:
        if sentinel not in tokens:
            problem = (
                f"placeholder {{{name}}} must be a separate, unquoted word in command= "
                "(ephor quotes it for you; inside a quoted string use "
                '"$EPHOR_FLOW_TITLE", "$EPHOR_FLOW_STATUS" or "$EPHOR_TICKET")'
            )
            if problem not in problems:
                problems.append(problem)
    return problems


def _starts_with_placeholder_or_dash(template: str) -> bool:
    """True when a ``start =`` prompt could begin with text from the ticket, or a dash.

    The prompt is the agent CLI's first positional argument and no ``--``
    precedes it (not every provider's CLI is known to honour one). A prompt
    that begins with ticket text lets a title such as
    ``--dangerously-skip-permissions`` arrive as an option.
    """
    try:
        parts = list(string.Formatter().parse(template))
    except ValueError:
        return False  # _template_problem already reported it
    for literal, field_name, _, _ in parts:
        head = literal.lstrip()
        if head:
            return head.startswith("-")
        if field_name is not None:
            return True
    return False

"""``ephor flows ...``: the board and its flows from the command line.

Kept out of :mod:`ephor.cli`, which is already long, and grown one
subcommand per phase:

* ``board`` prints the status lanes from the last snapshot — the same lanes
  the dashboard's ``status`` grouping draws — with each ticket's flow badge.
* ``tick [--dry-run] [--force]`` runs one tick (:mod:`ephor.flows.tick`);
  ``watch`` runs one every ``--interval`` seconds until Ctrl-C, which is how
  flows keep running while the dashboard is closed.
* ``ls [--all] [--ticket K] [--flow F]`` lists runs, newest first; ``show``
  prints one in full (payload, attempts, timestamps, its output's tail);
  ``approve`` / ``deny`` answer a run that is awaiting confirmation, by id
  or unique prefix; ``retry`` re-arms a stuck, failed or expired one.
* ``explain <KEY> [flow]`` says, rule by rule, whether it would fire on a
  ticket now, and the first gate that stops it — read-only.
* ``pause`` / ``resume`` set and clear the flag that stops every dispatch.
* ``check`` validates board.toml and asks Jira who you are.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime

from ephor import board, jira_api, work_items
from ephor.constants import AgentStatus
from ephor.flows import printable, runs, ticket_sort_key
from ephor.flows import snapshot as board_snapshot
from ephor.flows import tick as flows_tick
from ephor.flows.snapshot import Snapshot, TicketState
from ephor.jira import confident_ticket_for_agent
from ephor.state.manager import StateManager


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Attach ``flows`` and its subcommands to the top-level parser."""
    flows_p = subparsers.add_parser("flows", help="Status lanes and ticket flows (board.toml)")
    sub = flows_p.add_subparsers(dest="flows_command", metavar="<command>")
    board_p = sub.add_parser("board", help="Print the board's lanes from the last snapshot")
    board_p.add_argument(
        "--refresh",
        action="store_true",
        help="Run one flows tick first: search Jira, update the snapshot, run the flow rules",
    )
    tick_p = sub.add_parser("tick", help="Search the board once and run the flow rules")
    tick_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen; write nothing and launch nothing",
    )
    tick_p.add_argument(
        "--force",
        action="store_true",
        help=f"Tick even if the snapshot is younger than {flows_tick.COALESCE_SEC:g}s",
    )
    watch_p = sub.add_parser("watch", help="Run a tick every --interval seconds until Ctrl-C")
    watch_p.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_WATCH_INTERVAL_SEC,
        help=f"Seconds between ticks (default {DEFAULT_WATCH_INTERVAL_SEC:g}, minimum "
        f"{MIN_WATCH_INTERVAL_SEC:g})",
    )
    ls_p = sub.add_parser("ls", help="List flow runs, newest first")
    ls_p.add_argument(
        "--all", action="store_true", help="Include every finished run, not just recent ones"
    )
    ls_p.add_argument("--ticket", metavar="KEY", help="Only runs for this ticket")
    ls_p.add_argument("--flow", metavar="NAME", help="Only runs of this flow")
    show_p = sub.add_parser("show", help="Print one run: payload, attempts, timestamps, output")
    show_p.add_argument("run", help="Run id, or a unique prefix of one")
    approve_p = sub.add_parser("approve", help="Approve a run that is awaiting confirmation")
    approve_p.add_argument("run", help="Run id, or a unique prefix of one")
    deny_p = sub.add_parser("deny", help="Deny a run that is awaiting confirmation")
    deny_p.add_argument("run", help="Run id, or a unique prefix of one")
    retry_p = sub.add_parser(
        "retry", help="Re-arm a stuck, failed or expired run (back to awaiting confirmation)"
    )
    retry_p.add_argument("run", help="Run id, or a unique prefix of one")
    explain_p = sub.add_parser(
        "explain", help="Say why each flow would, or would not, fire on a ticket now"
    )
    explain_p.add_argument("ticket", help="Ticket key, e.g. DR-8222")
    explain_p.add_argument("flow", nargs="?", help="Only this flow")
    sub.add_parser("pause", help="Stop every flow from dispatching until `resume`")
    sub.add_parser("resume", help="Let flows dispatch again")
    sub.add_parser("check", help="Validate board.toml and probe Jira")


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch ``ephor flows <command>``."""
    command = getattr(args, "flows_command", None)
    if command == "board":
        return cmd_board(refresh=bool(args.refresh))
    if command == "tick":
        return cmd_tick(dry_run=bool(args.dry_run), force=bool(args.force))
    if command == "watch":
        return cmd_watch(interval=float(args.interval))
    if command == "ls":
        return cmd_ls(show_all=bool(args.all), ticket=args.ticket, flow=args.flow)
    if command == "show":
        return cmd_show(str(args.run))
    if command == "approve":
        return cmd_answer(str(args.run), approve=True)
    if command == "deny":
        return cmd_answer(str(args.run), approve=False)
    if command == "retry":
        return cmd_retry(str(args.run))
    if command == "explain":
        return cmd_explain(str(args.ticket), args.flow)
    if command == "pause":
        return cmd_pause()
    if command == "resume":
        return cmd_resume()
    if command == "check":
        return cmd_check()
    parser.parse_args(["flows", "--help"])
    return 0


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "undated"
    if seconds < 60:
        return f"{int(seconds)}s old"
    if seconds < 3600:
        return f"{int(seconds // 60)}m old"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h old"
    return f"{int(seconds // 86400)}d old"


def _live_sessions_by_ticket(snap: Snapshot) -> dict[str, int]:
    """How many live sessions each board ticket has, from state files and work records."""
    live = [a for a in StateManager().scan() if a.status is not AgentStatus.DEAD]
    live_ids = {a.session_id for a in live}
    # The facts only (pins, EPHOR_TICKET, branch name …), as the flows tick
    # counts them: a session that merely mentions a ticket is not on it.
    by_agent = {a.session_id: confident_ticket_for_agent(a) for a in live}
    counts: dict[str, int] = {}
    for key in snap.on_board():
        sids = {sid for sid, ticket in by_agent.items() if ticket == key}
        item = work_items.load(key)
        if item is not None:
            sids.update(s for s in item.session_ids if s in live_ids)
        if sids:
            counts[key] = len(sids)
    return counts


def cmd_board(*, refresh: bool) -> int:
    """Print the lanes: column → tickets, with status, title and live sessions."""
    config = board.load()
    if config is None:
        print(
            f"ephor: no board at {board.board_path()} — create one to turn the board on",
            file=sys.stderr,
        )
        return 1
    for error in config.errors:
        print(f"board.toml: {error}", file=sys.stderr)

    status = 0
    snap: Snapshot | None
    if refresh:
        # A tick, never the rule-less refresh: a refresh would consume the
        # moves it saw, and the flow rules would never get to fire on them.
        # Asked for by name: a fresh snapshot is no reason to skip it.
        result = flows_tick.run(config, force=True).refresh
        if not result.ok:
            print(f"ephor: refresh: {result.message}", file=sys.stderr)
            status = 1
        snap = result.snapshot if result.snapshot is not None else board_snapshot.load()
    else:
        snap = board_snapshot.load()

    settings = config.board if config.lanes_ok else board.BoardSettings()
    if not config.lanes_ok:
        print("ephor: [board] has errors — showing the default lanes", file=sys.stderr)
        status = 1
    if snap is None:
        print(
            "ephor: no snapshot yet — run `ephor flows board --refresh` or open the dashboard",
            file=sys.stderr,
        )
        return 1

    tickets = snap.on_board()
    lanes: dict[str, list[tuple[str, TicketState]]] = {}
    for key in sorted(tickets, key=ticket_sort_key):
        record = tickets[key]
        lane = settings.column_for(record.status, record.status_category)
        lanes.setdefault(lane, []).append((key, record))
    sessions = _live_sessions_by_ticket(snap)
    badges = runs.badges_by_ticket(runs.list_runs())
    configured = {column.name for column in settings.columns}

    print(
        f"board: {config.path} · snapshot {_age(snap.age_sec(datetime.now(UTC)))} · "
        f"{len(tickets)} ticket(s)"
    )
    for lane in settings.lane_names():
        rows = lanes.get(lane, [])
        if lane not in configured and not rows:
            continue
        print()
        print(f"{printable(lane)} ({len(rows)})")
        for key, record in rows:
            live = sessions.get(key, 0)
            tail = f"  · {live} session(s)" if live else ""
            if key in badges:
                tail += f"  [{badges[key]}]"
            print(
                f"  {key:<10} {printable(record.status, 18) or '?':<18} "
                f"{printable(record.title, 80)}{tail}"
            )
    unseen = len(snap.tickets) - len(tickets)
    if unseen:
        print()
        print(f"({unseen} ticket(s) missing from the last search are not shown)")
    return status


# --- tick ---------------------------------------------------------------------------


def _load_config() -> board.BoardConfig | None:
    config = board.load()
    if config is None:
        print(
            f"ephor: no board at {board.board_path()} — create one to turn the board on",
            file=sys.stderr,
        )
        return None
    for error in config.errors:
        print(f"board.toml: {error}", file=sys.stderr)
    return config


def cmd_tick(*, dry_run: bool, force: bool = False) -> int:
    """Run one tick and say what it did (or, with --dry-run, would do)."""
    config = _load_config()
    if config is None:
        return 1
    result = flows_tick.run(config, dry_run=dry_run, force=force)
    prefix = "dry run: " if dry_run else ""
    if result.status in (flows_tick.TickStatus.LOCKED, flows_tick.TickStatus.SKIPPED):
        print(f"ephor: {result.message}")
        return 0
    if not result.ok:
        print(f"ephor: tick: {result.message}", file=sys.stderr)
        return 1
    print(f"{prefix}{result.message}")
    _print_details(result)
    if dry_run:
        print("dry run: nothing was written and nothing was launched")
    return 0


def _print_details(result: flows_tick.TickResult) -> None:
    for move in result.transitions:
        print(f"  moved     {move.key:<10} {printable(move.detail, 60)}")
    for event in result.events:
        print(f"  {event.trigger.removeprefix('on_'):<9} {event.key:<10} {event.detail}")
    for name in result.adopted:
        print(f"  adopted   {name} — records the board as it stands; fires on the next move")
    if result.dispatch_note:
        print(f"  dispatch  {result.dispatch_note}")
    for action in result.actions:
        # Run ids and verbs are ours; the detail can carry ticket text.
        detail = f"  — {printable(action.detail, 120)}" if action.detail else ""
        print(f"  {action.verb:<10} {action.run_id}{detail}")


# --- watch --------------------------------------------------------------------------

DEFAULT_WATCH_INTERVAL_SEC = 60.0
# Below this a watch would mostly be asking Jira the same question again.
MIN_WATCH_INTERVAL_SEC = 10.0


def cmd_watch(
    *,
    interval: float,
    max_ticks: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Tick every ``interval`` seconds until Ctrl-C (or ``max_ticks``, for tests).

    board.toml is re-read before every tick, so an edit takes effect on the
    next one without a restart; a file that disappears, or stops parsing, is
    reported and waited out rather than ending the watch. A tick that finds
    ``tick.lock`` held (the dashboard is mid-tick) is the normal case and is
    not reported. A line is printed when something happened, or when the
    outcome differs from the previous tick's — not once a minute for nothing.
    """
    if not math.isfinite(interval) or interval < MIN_WATCH_INTERVAL_SEC:
        print(
            "ephor: --interval must be a finite number of at least "
            f"{MIN_WATCH_INTERVAL_SEC:g} seconds",
            file=sys.stderr,
        )
        return 2
    if board.load() is None:
        print(
            f"ephor: no board at {board.board_path()} — create one to turn the board on",
            file=sys.stderr,
        )
        return 1
    print(f"watching the board every {interval:g}s — Ctrl-C to stop", flush=True)
    last = ""
    count = 0
    try:
        while max_ticks is None or count < max_ticks:
            count += 1
            last = _watch_once(last)
            if max_ticks is not None and count >= max_ticks:
                break
            sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def _watch_once(last: str) -> str:
    """One watched tick. Returns what it amounted to, for the next one to compare with.

    That key is the outcome without its counts — "ok" plus any dispatch
    note, or the error — so a quiet board prints nothing, a tick that did
    something always prints, and a change of state (paused, Jira down,
    board.toml broken) prints once.
    """
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    config = board.load()
    if config is None:
        line = f"no board at {board.board_path()}"
    elif not config.lanes_ok:
        line = f"board.toml [board] has {len(config.board_errors)} error(s) — `ephor flows check`"
    else:
        result = flows_tick.run(config)
        if result.status in (flows_tick.TickStatus.LOCKED, flows_tick.TickStatus.SKIPPED):
            # Another ticker got there first (or very recently): normal.
            return last
        if not result.ok:
            line = f"tick: {result.message}"
        else:
            note = f" · {result.dispatch_note}" if result.dispatch_note else ""
            key = f"ok{note}"
            busy = result.transitions or result.events or result.actions or result.adopted
            if busy or key != last:
                print(f"{stamp} {result.message}{note}", flush=True)
                _print_details(result)
                sys.stdout.flush()
            return key
    if line != last:
        print(f"{stamp} {line}", flush=True)
    return line


# --- runs ---------------------------------------------------------------------------

# Without --all, finished runs drop out of `ls` after this long.
_LS_RECENT_SEC = 24 * 3600.0


def cmd_ls(*, show_all: bool, ticket: str | None = None, flow: str | None = None) -> int:
    """Runs newest first: pending ones always, finished ones for a day (or --all)."""
    now = datetime.now(UTC)
    wanted_ticket = (ticket or "").strip().upper()
    wanted_flow = (flow or "").strip().lower()
    listed = [
        r
        for r in runs.list_runs()
        if (show_all or not r.terminal or (runs.age_sec(r.updated_at, now) or 0.0) < _LS_RECENT_SEC)
        and (not wanted_ticket or r.ticket == wanted_ticket)
        and (not wanted_flow or r.flow == wanted_flow)
    ]
    if runs.is_paused():
        print("flows are PAUSED — `ephor flows resume` to dispatch again")
    if not listed:
        print("no runs" + ("" if show_all else " in the last day (--all for every run)"))
        return 0
    for run in listed:
        age = _age(runs.age_sec(run.updated_at, now)).replace(" old", "")
        # A stale run's reason usually says so already; say it once.
        stale = " stale" if run.stale and "stale" not in run.reason.lower() else ""
        reason = f"  {printable(run.reason, 70)}" if run.reason else ""
        print(f"{run.state:<16} {run.run_id:<40} {age:>4}{stale}{reason}")
    awaiting = sum(1 for r in listed if r.state is runs.RunState.AWAITING_CONFIRM)
    if awaiting:
        print()
        print(f"{awaiting} awaiting confirmation — `ephor flows approve <id>` or `deny <id>`")
    return 0


def cmd_answer(ref: str, *, approve: bool) -> int:
    """Approve or deny one awaiting run, by id or unique prefix."""
    target, problem = runs.resolve(ref)
    if target is None:
        print(f"ephor: {problem}", file=sys.stderr)
        return 1
    updated, message = runs.approve(target.run_id) if approve else runs.deny(target.run_id)
    if updated is None:
        print(f"ephor: {message}", file=sys.stderr)
        return 1
    print(message)
    return 0


def cmd_retry(ref: str) -> int:
    """Re-arm one stuck, failed or expired run: it waits for confirmation again."""
    target, problem = runs.resolve(ref)
    if target is None:
        print(f"ephor: {problem}", file=sys.stderr)
        return 1
    updated, message = runs.retry(target.run_id)
    if updated is None:
        print(f"ephor: {message}", file=sys.stderr)
        return 1
    print(message)
    print(f"  `ephor flows approve {updated.run_id}` to launch it on the next tick")
    return 0


# The run fields `show` prints, in order: identity, what fired it, where it went.
_SHOW_FIELDS = (
    "state",
    "reason",
    "flow",
    "ticket",
    "title",
    "trigger",
    "event",
    "generation",
    "kind",
    "from_status",
    "to_status",
    "pr_number",
    "pr_url",
    "head_sha",
    "session_id",
    "idle_since",
    "stale",
    "attempts",
    "retries",
    "created_at",
    "approved_at",
    "rearmed_at",
    "attempt_at",
    "retry_at",
    "launched_at",
    "updated_at",
    "window",
    "pane",
    "exit_code",
    "payload_path",
)


def cmd_show(ref: str, *, log_lines: int = 20) -> int:
    """Print one run in full: its fields, its payload, and the tail of its output log."""
    target, problem = runs.resolve(ref)
    if target is None:
        print(f"ephor: {problem}", file=sys.stderr)
        return 1
    print(target.run_id)
    for name in _SHOW_FIELDS:
        value = getattr(target, name)
        if value in ("", None, 0, False) and name not in ("state", "attempts"):
            continue
        shown = printable(str(value), 200)
        print(f"  {name + ':':<14}{shown}")
    payload_file = runs.payload_path(target.run_id)
    try:
        payload = json.loads(payload_file.read_text())
    except (OSError, ValueError):
        payload = None
    print()
    if payload is None:
        print("payload: none written yet (it is written when the run launches)")
    else:
        print(f"payload ({payload_file}):")
        for line in json.dumps(payload, indent=2, sort_keys=True).splitlines():
            print(f"  {printable(line, 200)}")
    tail = runs.read_log_tail(target.run_id, log_lines)
    if tail is not None:
        print()
        print(f"output, last {len(tail)} line(s) ({runs.log_path(target.run_id)}):")
        for line in tail:
            print(f"  {printable(line, 200)}")
    return 0


def cmd_explain(ticket: str, flow: str | None = None) -> int:
    """Say, for each flow (or ``flow``), whether it would fire on ``ticket`` now."""
    config = _load_config()
    if config is None:
        return 1
    found, problem = flows_tick.explain(config, ticket, flow)
    if problem:
        print(f"ephor: {problem}", file=sys.stderr)
        return 1
    key = ticket.strip().upper()
    snap = board_snapshot.load()
    record = snap.tickets.get(key) if snap is not None else None
    if record is not None:
        title = printable(record.title, 60)
        print(f"{key}  {printable(record.status, 30) or '?'}  {title}")
    for item in found:
        print()
        verdict = "would fire" if item.fires else f"does not fire — {item.gate}"
        print(f"{item.flow}: {verdict}")
        for check in item.passed:
            print(f"  ✓ {printable(check, 120)}")
        mark = "✓" if item.fires else "✗"
        print(f"  {mark} {printable(item.detail, 160)}")
        for note in item.notes:
            print(f"  · {printable(note, 160)}")
    return 0


def cmd_pause() -> int:
    if not runs.pause():
        print(f"ephor: could not write {runs.paused_path()}", file=sys.stderr)
        return 1
    print("flows paused — nothing dispatches until `ephor flows resume`")
    return 0


def cmd_resume() -> int:
    if not runs.resume():
        print(f"ephor: could not remove {runs.paused_path()}", file=sys.stderr)
        return 1
    # A pause holds runs rather than dropping them; say what is about to go.
    held = [
        r for r in runs.list_runs() if r.state in (runs.RunState.CLAIMED, runs.RunState.BLOCKED)
    ]
    if held:
        print(
            f"flows resumed — {len(held)} held run(s) dispatch on the next tick "
            "(any held past confirm_ttl expire instead; `ephor flows ls` to see them)"
        )
    else:
        print("flows resumed")
    return 0


def cmd_check() -> int:
    """Validate board.toml, then ask Jira who the configured user is."""
    config = board.load()
    if config is None:
        print(f"ephor: no board at {board.board_path()}", file=sys.stderr)
        return 1
    status = 0
    print(f"board: {config.path}")
    if config.errors:
        status = 1
        for error in config.errors:
            print(f"  error: {error}")
        if config.lanes_ok:
            print("  lanes render; no flow dispatches until every error is fixed")
        else:
            print("  [board] is invalid: the default lanes are drawn and no flow dispatches")
    else:
        print(f"  ok: {len(config.board.columns)} column(s), {len(config.rules)} flow(s)")
        print(f"  search: {config.board.search_jql()}")
        for rule in config.rules:
            what = f"start={rule.start!r}" if rule.start else f"command={rule.command!r}"
            extra = "".join(
                f" {name}={value!r}"
                for name, value in (
                    ("from", list(rule.from_statuses)),
                    ("jql", rule.jql),
                    ("repo", rule.repo),
                )
                if value
            )
            print(
                f"  flow {rule.name}: {rule.trigger}={rule.value!r} assignee={rule.assignee} "
                f"mode={rule.mode} {what}{extra}"
            )
    for warning in check_warnings(config):
        print(f"  warn: {warning}")
    # Before the Jira probe, which can end the check early.
    if runs.is_paused():
        print("flows: PAUSED — `ephor flows resume` to dispatch again")
    missing = jira_api.missing_credentials()
    if missing:
        print(f"jira: not configured — missing {missing}")
        return 1
    account = jira_api.myself()
    if account is None:
        print("jira: /myself did not answer — check the URL and token")
        return 1
    print(f"jira: ok — accountId {account}")
    if config.lanes_ok and config.board.projects:
        # The board's own search, one result: a JQL Jira rejects (a field the
        # site does not have) otherwise only shows up as a tick that fails.
        found = jira_api.search(config.board.search_jql(), ("status",), page_size=1, max_pages=1)
        if found is None:
            failure = jira_api.last_error()
            why = f" ({failure.summary})" if failure is not None else ""
            print(f"jira: the board search failed{why} — check [board] jql and projects")
            return 1
        print("jira: board search ok")
        for rule in config.rules:
            if not rule.jql:
                continue
            # The board's scope AND the rule's condition: does Jira accept it?
            query = f"project in ({', '.join(config.board.projects)}) AND ({rule.jql})"
            if jira_api.search(query, ("status",), page_size=1, max_pages=1) is None:
                failure = jira_api.last_error()
                why = f" ({failure.summary})" if failure is not None else ""
                print(f"jira: flow {rule.name}: its jql was rejected{why}")
                status = 1
            else:
                print(f"jira: flow {rule.name}: jql ok")
    return status


def check_warnings(config: board.BoardConfig) -> list[str]:
    """board.toml's warnings, plus what only a look at the snapshot and the environment shows.

    Warnings never disable anything (see :mod:`ephor.board`). Shared with
    ``ephor doctor``, which shows them as warn.
    """
    found = list(config.warnings)
    snap = board_snapshot.load()
    if config.lanes_ok and snap is not None:
        seen = frozenset(t.status.strip().lower() for t in snap.on_board().values() if t.status)
        # Only the "in a column, but no ticket is in it" half is new here.
        found.extend(
            w
            for w in board.status_warnings(config.board, config.rules, seen)
            if w not in config.warnings
        )
    if config.rules and not config.flows.tmux_session and not os.environ.get("TMUX"):
        found.append(
            "[flows] tmux_session is empty and this shell is not inside tmux: a tick run "
            "from here (a timer, a plain terminal) has no session to open flow windows in — "
            'set tmux_session = "main" (any session that exists)'
        )
    if (held := flows_tick.backoff_until()) is not None:
        until, why = held
        found.append(
            f"Jira rate-limited the board search ({why}); ticks wait until "
            f"{until.astimezone().strftime('%H:%M:%S')}"
        )
    return found

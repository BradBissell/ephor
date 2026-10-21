"""``ephor flows ...``: the board and its flows from the command line.

Kept out of :mod:`ephor.cli`, which is already long, and grown one
subcommand per phase:

* ``board`` prints the status lanes from the last snapshot — the same lanes
  the dashboard's ``status`` grouping draws — with each ticket's flow badge.
* ``tick [--dry-run]`` runs one tick (:mod:`ephor.flows.tick`).
* ``ls [--all]`` lists runs, newest first; ``approve`` / ``deny`` answer a
  run that is awaiting confirmation, by id or unique prefix.
* ``pause`` / ``resume`` set and clear the flag that stops every dispatch.
* ``check`` validates board.toml and asks Jira who you are.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from ephor import board, jira_api, work_items
from ephor.constants import AgentStatus
from ephor.flows import printable, runs, ticket_sort_key
from ephor.flows import snapshot as board_snapshot
from ephor.flows import tick as flows_tick
from ephor.flows.snapshot import Snapshot, TicketState
from ephor.jira import ticket_for_agent
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
    ls_p = sub.add_parser("ls", help="List flow runs, newest first")
    ls_p.add_argument(
        "--all", action="store_true", help="Include every finished run, not just recent ones"
    )
    approve_p = sub.add_parser("approve", help="Approve a run that is awaiting confirmation")
    approve_p.add_argument("run", help="Run id, or a unique prefix of one")
    deny_p = sub.add_parser("deny", help="Deny a run that is awaiting confirmation")
    deny_p.add_argument("run", help="Run id, or a unique prefix of one")
    sub.add_parser("pause", help="Stop every flow from dispatching until `resume`")
    sub.add_parser("resume", help="Let flows dispatch again")
    sub.add_parser("check", help="Validate board.toml and probe Jira")


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch ``ephor flows <command>``."""
    command = getattr(args, "flows_command", None)
    if command == "board":
        return cmd_board(refresh=bool(args.refresh))
    if command == "tick":
        return cmd_tick(dry_run=bool(args.dry_run))
    if command == "ls":
        return cmd_ls(show_all=bool(args.all))
    if command == "approve":
        return cmd_answer(str(args.run), approve=True)
    if command == "deny":
        return cmd_answer(str(args.run), approve=False)
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
    # The dashboard's resolver (pins, branch name, the state file's ticket),
    # so this command and the TUI's lanes agree on which session is whose.
    by_agent = {a.session_id: (ticket_for_agent(a) or "").upper() for a in live}
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
        result = flows_tick.run(config).refresh
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
        lanes.setdefault(settings.column_for(tickets[key].status), []).append((key, tickets[key]))
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


def cmd_tick(*, dry_run: bool) -> int:
    """Run one tick and say what it did (or, with --dry-run, would do)."""
    config = _load_config()
    if config is None:
        return 1
    result = flows_tick.run(config, dry_run=dry_run)
    prefix = "dry run: " if dry_run else ""
    if result.status is flows_tick.TickStatus.LOCKED:
        print(f"ephor: {result.message}")
        return 0
    if not result.ok:
        print(f"ephor: tick: {result.message}", file=sys.stderr)
        return 1
    print(f"{prefix}{result.message}")
    for move in result.transitions:
        print(f"  moved     {move.key:<10} {printable(move.detail, 60)}")
    for name in result.adopted:
        print(f"  adopted   {name} — records the board as it stands; fires on the next move")
    if result.dispatch_note:
        print(f"  dispatch  {result.dispatch_note}")
    for action in result.actions:
        # Run ids and verbs are ours; the detail can carry ticket text.
        detail = f"  — {printable(action.detail, 120)}" if action.detail else ""
        print(f"  {action.verb:<10} {action.run_id}{detail}")
    if dry_run:
        print("dry run: nothing was written and nothing was launched")
    return 0


# --- runs ---------------------------------------------------------------------------

# Without --all, finished runs drop out of `ls` after this long.
_LS_RECENT_SEC = 24 * 3600.0


def cmd_ls(*, show_all: bool) -> int:
    """Runs newest first: pending ones always, finished ones for a day (or --all)."""
    now = datetime.now(UTC)
    listed = [
        r
        for r in runs.list_runs()
        if show_all or not r.terminal or (runs.age_sec(r.updated_at, now) or 0.0) < _LS_RECENT_SEC
    ]
    if runs.is_paused():
        print("flows are PAUSED — `ephor flows resume` to dispatch again")
    if not listed:
        print("no runs" + ("" if show_all else " in the last day (--all for every run)"))
        return 0
    for run in listed:
        age = _age(runs.age_sec(run.updated_at, now)).replace(" old", "")
        stale = " stale" if run.stale else ""
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
            pending = (
                "" if rule.trigger in flows_tick.EVALUATED_TRIGGERS else " (not yet evaluated)"
            )
            print(
                f"  flow {rule.name}: {rule.trigger}={rule.value!r} assignee={rule.assignee} "
                f"mode={rule.mode} {what}{pending}"
            )
    if not jira_api.is_configured():
        print("jira: not configured (set EPHOR_JIRA_URL, EPHOR_JIRA_EMAIL, EPHOR_JIRA_TOKEN)")
        return 1
    account = jira_api.myself()
    if account is None:
        print("jira: /myself did not answer — check the URL and token")
        return 1
    print(f"jira: ok — accountId {account}")
    if runs.is_paused():
        print("flows: PAUSED")
    return status

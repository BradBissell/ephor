"""``ephor flows ...``: the board and its flows from the command line.

Kept out of :mod:`ephor.cli`, which is already long, and grown one
subcommand per phase. Today: ``board``, which prints the status lanes from
the last snapshot — the same lanes the dashboard's ``status`` grouping
draws, for a terminal without the TUI, or a script.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from ephor import board, work_items
from ephor.constants import AgentStatus
from ephor.flows import printable, ticket_sort_key
from ephor.flows import snapshot as board_snapshot
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
        help="Search Jira and update the snapshot first (needs Jira credentials)",
    )


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Dispatch ``ephor flows <command>``."""
    command = getattr(args, "flows_command", None)
    if command == "board":
        return cmd_board(refresh=bool(args.refresh))
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
        result = board_snapshot.refresh(config)
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
            print(
                f"  {key:<10} {printable(record.status, 18) or '?':<18} "
                f"{printable(record.title, 80)}{tail}"
            )
    unseen = len(snap.tickets) - len(tickets)
    if unseen:
        print()
        print(f"({unseen} ticket(s) missing from the last search are not shown)")
    return status

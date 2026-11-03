from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ephor import board, start_prompt
from ephor.flows import runs
from ephor.flows.snapshot import PrState, Snapshot, TicketState

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _config(tmp_path: Path, extra: str = "") -> board.BoardConfig:
    path = tmp_path / "board.toml"
    path.write_text(
        '[board]\nprojects = ["DR"]\nstart_prompt = "/next-step {ticket} {context}"\n'
        + extra
        + 'columns = ["To Do", { name = "Review", statuses = ["Peer Review"] }, "Done"]\n'
    )
    config = board.load(path)
    assert config is not None and config.errors == ()
    return config


def _snap(**ticket: object) -> Snapshot:
    record = TicketState(
        status="Peer Review",
        title="Fix the thing",
        entered_at=runs.iso(NOW - timedelta(days=3)),
        **ticket,  # type: ignore[arg-type]
    )
    prs = (
        PrState(number=800, state="MERGED", url="https://gh/pr/800"),
        PrState(number=812, state="OPEN", url="https://gh/pr/812", ci="fail"),
    )
    return Snapshot(taken_at=runs.iso(NOW), tickets={"DR-1": record}, prs={"DR-1": prs})


def test_the_prompt_carries_what_the_board_knows(tmp_path: Path) -> None:
    prompt = start_prompt.prompt_for(_config(tmp_path), "dr-1", _snap(flagged=True), now=NOW)
    assert prompt == (
        "/next-step DR-1 status: Peer Review (lane Review, 3d in status); flagged; "
        "PR #812 open, CI fail https://gh/pr/812"
    )


def test_values_cover_every_field(tmp_path: Path) -> None:
    values = start_prompt.values_for(_config(tmp_path), "DR-1", _snap(), repo="/r", now=NOW)
    assert set(values) == board.START_PROMPT_FIELDS
    assert values["pr_number"] == "812" and values["ci"] == "fail" and values["age"] == "3d"
    assert values["lane"] == "Review" and values["repo"] == "/r"


def test_a_ticket_the_snapshot_does_not_know_still_gets_a_prompt(tmp_path: Path) -> None:
    assert start_prompt.prompt_for(_config(tmp_path), "DR-9", None, now=NOW) == "/next-step DR-9"


def test_no_start_prompt_means_no_prompt(tmp_path: Path) -> None:
    path = tmp_path / "board.toml"
    path.write_text('[board]\nprojects = ["DR"]\n')
    config = board.load(path)
    assert start_prompt.prompt_for(config, "DR-1", _snap(), now=NOW) == ""
    assert start_prompt.prompt_for(None, "DR-1", _snap(), now=NOW) == ""

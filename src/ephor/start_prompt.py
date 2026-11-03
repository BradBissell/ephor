"""The opening prompt of a session started from a ticket row.

``N`` on a ticket row opens the agent with ``board.toml``'s
``start_prompt``: a column's own, else the board's. Its placeholders (:data:`ephor.board.START_PROMPT_FIELDS`) come from
the last flows snapshot, so the agent starts from what the board already
knows instead of rediscovering it. ``{context}`` is all of that as one line::

    status: Peer Review (lane Review, 3d in status); flagged; PR #812 open, CI fail https://…

Nothing here asks Jira or GitHub: a stale or missing snapshot just means
fewer facts, never a slower start.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ephor.board import BoardConfig, render_start_prompt
from ephor.flows import runs
from ephor.flows.snapshot import PrState, Snapshot, TicketState


def _age(entered_at: str, now: datetime) -> str:
    seconds = runs.age_sec(entered_at, now) if entered_at else None
    if seconds is None or seconds < 0:
        return ""
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _pick_pr(prs: tuple[PrState, ...]) -> PrState | None:
    """The PR worth naming: an open one first, else the newest."""
    if not prs:
        return None
    open_prs = [pr for pr in prs if pr.is_open]
    return max(open_prs or prs, key=lambda pr: pr.number)


def values_for(
    config: BoardConfig,
    ticket: str,
    snap: Snapshot | None,
    *,
    repo: str = "",
    now: datetime | None = None,
) -> dict[str, str]:
    """Every start_prompt placeholder for ``ticket``, from the snapshot ("" when unknown)."""
    moment = now or datetime.now(UTC)
    key = ticket.strip().upper()
    record: TicketState | None = snap.tickets.get(key) if snap is not None else None
    pr = _pick_pr(snap.prs_for(key)) if snap is not None else None
    status = record.status if record is not None else ""
    lane = (
        config.board.column_for(record.status, record.status_category) if record is not None else ""
    )
    age = _age(record.entered_at, moment) if record is not None else ""
    if age and record is not None and record.entered_estimated:
        age = f"~{age}"

    facts: list[str] = []
    if status:
        where = [f"lane {lane}"] if lane and lane.lower() != status.lower() else []
        if age:
            where.append(f"{age} in status")
        facts.append(f"status: {status}" + (f" ({', '.join(where)})" if where else ""))
    if record is not None and record.flagged:
        facts.append("flagged")
    if pr is not None:
        state = (pr.state or "?").lower()
        ci = f", CI {pr.ci}" if pr.ci else ""
        facts.append(f"PR #{pr.number} {state}{ci} {pr.url}".strip())
    elif snap is not None and key in snap.prs:
        facts.append("no PR")

    return {
        "ticket": key,
        "title": record.title if record is not None else "",
        "status": status,
        "lane": lane,
        "age": age,
        "pr_url": pr.url if pr is not None else "",
        "pr_number": str(pr.number) if pr is not None else "",
        "ci": pr.ci if pr is not None else "",
        "context": "; ".join(facts),
        "repo": repo,
    }


def prompt_for(
    config: BoardConfig | None,
    ticket: str,
    snap: Snapshot | None,
    *,
    repo: str = "",
    now: datetime | None = None,
) -> str:
    """``ticket``'s opening prompt from board.toml, or "" when none is configured."""
    if config is None or not config.lanes_ok:
        return ""
    values = values_for(config, ticket, snap, repo=repo, now=now)
    template = config.board.start_prompt_for(values["lane"])
    return render_start_prompt(template, values) if template else ""

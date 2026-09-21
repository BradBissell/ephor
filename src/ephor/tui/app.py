"""Textual TUI dashboard for ephor.

Live list of every Claude Code session, refreshed from the state dir every
500ms. j/k or arrows navigate; Enter jumps the user's tmux client to the
selected session's pane; q quits.

Layout (matches docs/stitch-handoff.md):

  Header (Textual)
  HeaderBar (PERM/WAIT/ERR/WORK/IDLE/DEAD counters)
  Sessions (ListView of SessionRow rows, 1 line each)
  Summary line (active count / token total / aggregate spark)
  StatusToast (ephemeral last-action message)
  Footer (key hints)

Keep this file under ~400 lines — split widgets out the moment it grows.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.css.query import NoMatches
from textual.widgets import Footer, Header, Input, ListItem, ListView, Static

from ephor import eventlog, gitinfo, jira, launcher, permissions, ticket_pins, work_items
from ephor.account import AccountConfig, load_account_config
from ephor.account_usage import (
    AccountFingerprint,
    AccountUsage,
    caps_for,
    read_active_fingerprint,
)
from ephor.account_usage import (
    compute_usage as compute_account_usage,
)
from ephor.account_usage import (
    format_account_usage_segment as account_usage_segment,
)
from ephor.account_usage import (
    load_store as load_account_store,
)
from ephor.account_usage import (
    needs_refresh as account_anchor_needs_refresh,
)
from ephor.account_usage import (
    record_anchor as record_account_anchor,
)
from ephor.account_usage import (
    save_store as save_account_store,
)
from ephor.constants import WORK_ATTENTION_DISPLAY, AgentStatus, WorkAttention
from ephor.github import PrResolver, PullRequest
from ephor.jira import clear_cwd_cache, match_for_agent, ticket_for_agent
from ephor.jira_api import IssueResolver
from ephor.jira_api import drift as jira_drift
from ephor.notify import Notifier
from ephor.speech import SpeechWatcher
from ephor.speech_player import SpeechPlayer
from ephor.speech_settings import load as load_speech_settings
from ephor.speech_settings import save as save_speech_settings
from ephor.state.manager import StateManager
from ephor.state.models import AgentState, StatusSummary
from ephor.state.reconciler import reconcile
from ephor.summarizer import (
    summarize_agy,
    summarize_text,
    summarize_transcript,
    unavailable_reason,
)
from ephor.summary_store import SummaryStore
from ephor.tmux.discover import enrich_state_files
from ephor.tmux.navigator import (
    JumpResult,
    detect_focused_external_pane,
    jump_to,
    kill_session,
)
from ephor.tui.activity import ActivitySampler
from ephor.tui.tokens import TokenTracker, format_tokens, transcript_path
from ephor.tui.widgets import HeaderBar, SessionRow, SpeechBar
from ephor.tui.widgets.session_row import RowContext, render_sparkline
from ephor.usage import (
    DEFAULT_REFRESH_INTERVAL_SEC,
    UsageSnapshot,
    fetch_oauth_usage,
    fetch_usage,
    format_usage_segment,
    load_cached_snapshot,
    merge_with_previous,
    write_cached_snapshot,
)

REFRESH_INTERVAL = 0.5  # seconds
RECONCILE_INTERVAL = 30.0  # how often to sweep dead state files / reset stuck waits
KILL_CONFIRM_WINDOW_SEC = 3.0  # second-press window to actually fire the kill
# ccusage is local-transcript-driven (no rate limit), but each invocation
# rescans hundreds of MB of JSONL — typical 3-5s. The transcripts only
# update on assistant turn completion, so refreshing more often than this
# is wasted I/O.
USAGE_REFRESH_INTERVAL = DEFAULT_REFRESH_INTERVAL_SEC
# How often the background worker sweeps sessions looking for a pull
# request to attach to their Jira cell. Each miss costs a `gh` round trip,
# and PrResolver's own TTL already throttles re-probes, so this only needs
# to be often enough that a PR opened mid-session lights up promptly.
PR_REFRESH_INTERVAL = 20.0
# Ticket status changes on a human's clock. IssueResolver's own 10-minute
# TTL does the real throttling; this only has to be often enough to notice
# a ticket that moved while you were watching a different row.
JIRA_REFRESH_INTERVAL = 60.0


# Prefix marking a ListView row as a group header rather than a session.
# Starts with NUL so it can never collide with a session id, which the hook
# handler anchors to [A-Za-z0-9_-].
_GROUP_PREFIX = "\x00grp:"


def is_group_row(row_id: str | None) -> bool:
    """True when a row id names a group header instead of a session."""
    return bool(row_id) and row_id.startswith(_GROUP_PREFIX)


def group_key_of(row_id: str) -> str:
    """The group name inside a header row id."""
    return row_id[len(_GROUP_PREFIX) :]


class StatusToast(Static):
    """One-line ephemeral message at the bottom (jump result, errors, etc.)."""


class GroupHeader(Static):
    """A non-session row naming a group and summarising what is inside it."""


class EphorApp(App[int]):
    """Live dashboard for Claude Code sessions."""

    CSS_PATH = Path(__file__).parent / "theme.tcss"

    TITLE = "ephor"
    SUB_TITLE = "live session dashboard"

    # Register our command-palette provider alongside the built-in
    # system commands (theme picker, help, quit). Ctrl+P opens the
    # palette; EphorCommands surfaces every keybinding action.
    from ephor.tui.commands import EphorCommands as _EphorCommands

    COMMANDS = App.COMMANDS | {_EphorCommands}

    BINDINGS: ClassVar[Sequence[Binding]] = [  # type: ignore[assignment]
        Binding("q", "quit", "quit"),
        Binding("ctrl+c", "quit", "quit", show=False),
        # priority=True so the ListView's own Enter handler doesn't swallow it.
        # Backstopped by on_list_view_selected below.
        Binding("enter", "jump", "jump to selected session", priority=True),
        Binding("r", "refresh", "refresh now"),
        Binding("x", "kill", "kill selected session"),
        Binding("s", "summarize", "summarize selected session"),
        Binding("o", "open_pr", "open the selected session's pull request"),
        Binding("p", "pin_ticket", "pin a ticket to the selected session"),
        Binding("t", "jump_speaking", "jump to TTS speaking session"),
        Binding("m", "toggle_mute", "mute / unmute TTS playback"),
        Binding("M", "toggle_speak_mode", "TTS: full reply ↔ summary"),
        Binding("n", "next_attention", "jump cursor to next row needing a human"),
        Binding("slash", "filter", "filter sessions by substring"),
        Binding("escape", "clear_filter", "clear filter", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        # Permission inbox — answer a blocked session without leaving the board.
        Binding("a", "allow", "allow the selected session's permission request"),
        Binding("d", "deny", "deny the selected session's permission request"),
        Binding("A", "allow_standing", "allow everything this session asks, until revoked"),
        Binding("D", "revoke", "revoke a queued or standing decision"),
        # Batch operations — everything above acts on the marked set when
        # there is one, and on the cursor row when there is not.
        Binding("space", "toggle_select", "mark / unmark this row", priority=True),
        Binding("c", "clear_selection", "unmark every row"),
        # Fleet shape.
        Binding("g", "cycle_grouping", "group rows by ticket / repo / not at all"),
        Binding("z", "toggle_collapse", "collapse or expand the group at the cursor"),
        # Launching, not just watching.
        Binding("N", "new_session", "start another session on this row's ticket"),
        Binding("R", "resume_session", "reopen this session in a new tmux window"),
    ]

    # Grouping modes, in the order `g` cycles them.
    GROUPINGS: ClassVar[tuple[str, ...]] = ("none", "ticket", "repo")

    def __init__(self, manager: StateManager | None = None) -> None:
        super().__init__()
        self._manager = manager or StateManager()
        self._sid_by_row: list[str] = []  # row index → session_id
        self._rows_by_sid: dict[str, SessionRow] = {}  # for in-place updates
        self._headers_by_id: dict[str, GroupHeader] = {}  # group rows, same list
        self._items_by_sid: dict[str, ListItem] = {}  # for in-place reorder
        self._agents_by_sid: dict[str, AgentState] = {}  # last painted snapshot
        self._toast: StatusToast | None = None
        self._header_bar: HeaderBar | None = None
        self._summary_line: Static | None = None
        self._speech_bar: SpeechBar | None = None
        # ephor owns TTS playback when running. The watcher tails the
        # speech log; the player is the FIFO queue + subprocess manager.
        # The persisted "muted" setting (env > file > default) decides
        # whether audio is gated at startup; the `m` hotkey flips it
        # live and writes back.
        self._speech_settings = load_speech_settings()
        self._speech_player = SpeechPlayer(
            watcher=SpeechWatcher(),
            muted=not self._speech_settings.enabled,
            speak_mode=self._speech_settings.speak_mode,
        )
        self._activity = ActivitySampler()
        self._tokens = TokenTracker()
        self._account: AccountConfig = load_account_config()
        self._summaries = SummaryStore()
        self._summarizing: set[str] = set()  # in-flight session_ids
        # Jira key -> GitHub PR, resolved off the render path. `cached()` is
        # what SessionRow reads; the worker below fills it in.
        self._prs = PrResolver()
        self._pr_lookup_in_flight: set[str] = set()
        # Jira's own view of each ticket, resolved off the render path like
        # the PR is. Stays empty — and costs nothing — until the user has
        # configured credentials; see ephor.jira_api.
        self._issues = IssueResolver()
        self._issue_lookup_in_flight: set[str] = set()
        # Rows the user has marked for a batch action. Every action that can
        # act on many sessions reads this first and falls back to the cursor.
        self._selected: set[str] = set()
        # Fleet shape: how rows are grouped, and which groups are folded away.
        self._group_by: str = "none"
        self._collapsed: set[str] = set()
        # session_id -> the other sessions sharing its worktree. Recomputed
        # each refresh from cwds we already have; see gitinfo.collisions.
        self._collisions: dict[str, tuple[str, ...]] = {}
        # Queued permission answers, refreshed once per tick (see _refresh_table).
        self._pending_decisions: dict[str, permissions.PendingDecision] = {}
        # Previous status / PR state per session, so the refresh loop can
        # tell a *change* from a repaint and log only the former.
        self._prev_status: dict[str, str] = {}
        self._prev_pr: dict[str, tuple[str, str, str]] = {}
        self._notifier = Notifier(desktop_fallback=True)
        self._kill_armed_sid: str | None = None
        self._kill_armed_at: float = 0.0
        self._filter: str = ""  # case-insensitive substring filter; "" = show all
        self._filter_input: Input | None = None
        self._ticket_input: Input | None = None
        # Session the pin prompt is editing, so a scroll mid-typing can't
        # land the answer on whichever row happens to be selected on submit.
        self._pin_target: str | None = None
        # Re-entrancy guard for _refresh_table. Cold-path DOM rebuilds await
        # ListView.clear()/mount(); without this guard a 500ms timer tick
        # firing mid-rebuild could interleave clears and mounts, leaving
        # _sid_by_row out of sync with the actual children. action_jump would
        # then map list_view.index → a stale or missing sid (the visible
        # symptom: pressing Enter does nothing or jumps to the wrong pane).
        self._refreshing: bool = False
        # Most recently observed "external" tmux pane — i.e. the active
        # pane in any tmux client OTHER than the one running ephor. We move
        # the cursor to the matching session whenever this changes, so a
        # user switching to a Ghostty window that's hosting session X gets
        # X auto-highlighted on the dashboard. We only react to CHANGES
        # so user navigation (j/k) is never overridden mid-session.
        self._last_external_pane: str | None = None
        # Cache of TMUX_PANE so we don't re-read env every tick. Empty
        # string → ephor was launched outside tmux; the follow logic still
        # works, it just can't exclude "us."
        self._self_pane: str = os.environ.get("TMUX_PANE", "")
        # Latest snapshot from ccusage. Hydrated from disk cache at
        # construction time so reopening the TUI within ~2h shows numbers
        # immediately instead of waiting 3-5s on the first ccusage scan.
        # Refresh worker is fired in on_mount and on USAGE_REFRESH_INTERVAL.
        self._usage: UsageSnapshot | None = load_cached_snapshot()
        # Per-account anchor store + fingerprint of the current account.
        # Loaded from disk so per-account percentages are immediate on
        # TUI reopen — same warm-start rationale as the ccusage cache.
        # ``_account_usage`` is the display-ready (anchor + delta) result;
        # the worker recomputes it each tick.
        self._account_store = load_account_store()
        self._account_fingerprint: AccountFingerprint | None = None
        self._account_usage: AccountUsage | None = None

    # ---- compose --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="main"):
            self._header_bar = HeaderBar()
            yield self._header_bar
            yield ListView(id="session-list")
            self._summary_line = Static("", id="summary-line")
            yield self._summary_line
            self._filter_input = Input(placeholder="filter (esc to clear)…", id="filter-input")
            self._filter_input.display = False
            yield self._filter_input
            self._ticket_input = Input(
                placeholder="ticket for this session (empty to unpin, esc to cancel)…",
                id="ticket-input",
            )
            self._ticket_input.display = False
            yield self._ticket_input
            self._toast = StatusToast("")
            yield self._toast
            # SpeechBar mirrors the TTS engine. Sits above the Footer so
            # the karaoke text is the last thing visible before the key
            # hints — eyes naturally fall there mid-listening.
            self._speech_bar = SpeechBar(self._manager, self._speech_player)
            yield self._speech_bar
        yield Footer()

    # ---- lifecycle ------------------------------------------------------

    async def on_mount(self) -> None:
        await self._refresh_table()
        self.set_interval(REFRESH_INTERVAL, self._refresh_table)
        # Reconcile less often — disk I/O cost, and the state it cleans up
        # accumulates slowly. Run once at startup so a freshly opened TUI
        # immediately reflects the cleaned-up world.
        self._reconcile_now()
        self.set_interval(RECONCILE_INTERVAL, self._reconcile_now)
        # Skip the immediate fetch when we already have a fresh-enough
        # cached snapshot (loaded in __init__). Each ccusage scan is 3-5s
        # of CPU and disk I/O, so warm-starting from cache cuts perceived
        # TUI latency to zero. The hourly cache TTL is much longer than
        # the 2-minute refresh — that's intentional: the cache is just for
        # avoiding the cold scan on rapid TUI reopens, not the source of
        # truth.
        if self._usage is None or self._usage.error is not None:
            self._fetch_usage_now()
        self.set_interval(USAGE_REFRESH_INTERVAL, self._fetch_usage_now)
        # 200ms tick: poll the speech log for new start/stop records,
        # route them into the player's queue, and reap finished playback
        # so the next queued item starts. Same cadence as the SpeechBar
        # refresh — they read the same underlying state.
        self.set_interval(0.2, self._tick_speech_player)
        # Warm the PR cache immediately so the first paint after startup
        # already carries links for sessions with review in flight.
        self._refresh_prs()
        self.set_interval(PR_REFRESH_INTERVAL, self._refresh_prs)
        # Jira is read on a slower clock than GitHub: a ticket's status moves
        # when a human drags a card, not when a build finishes. Costs nothing
        # until credentials are configured.
        self.set_interval(JIRA_REFRESH_INTERVAL, self._refresh_issues)

    def _tick_speech_player(self) -> None:
        """Drive the speech player. Wrapped in suppress so a transient
        OSError tailing the log can never crash the dashboard."""
        with contextlib.suppress(Exception):
            self._speech_player.tick()

    async def on_unmount(self) -> None:
        # Tear down playback so closing the TUI doesn't leave an orphan
        # kokoro+paplay running in the background.
        with contextlib.suppress(Exception):
            self._speech_player.stop_all()

    def _reconcile_now(self) -> None:
        """Sweep orphaned state files and reset stuck WAITING_* statuses.

        Best-effort: any failure is swallowed so a transient permission or
        FS issue doesn't crash the dashboard.
        """
        with contextlib.suppress(OSError):
            reconcile(self._manager.directory)

    # ---- data -----------------------------------------------------------

    async def _refresh_table(self) -> None:
        # DEAD sessions are hidden from the dashboard. The on-disk state file
        # is left alone; scan() will mark it DEAD again next tick if the
        # agent_pid is still missing, and a future cleanup pass can sweep it.
        if self._refreshing:
            return
        self._refreshing = True
        try:
            agents = [a for a in self._manager.scan() if a.status != AgentStatus.DEAD]
            # StateManager.scan() sorts by last_event_time desc, which makes
            # the list reorder every time any session fires a hook event —
            # disorienting when you're trying to keep your eye on a row. The
            # dashboard sorts by (started_at, session_id) ascending instead:
            # existing rows never shift, and new sessions append at the
            # bottom. session_id breaks ties for sessions started in the
            # same second so the order is fully deterministic.
            agents.sort(key=lambda a: (a.started_at, a.session_id))
            if self._filter:
                agents = [a for a in agents if _agent_matches_filter(a, self._filter)]

            # Sample CPU activity for every live session before rendering, so
            # the SessionRow gets fresh sparkline data this tick. Prune
            # buffers for pids that vanished so dead sessions don't leak.
            live_pids = [a.agent_pid for a in agents if a.agent_pid is not None]
            for pid in live_pids:
                self._activity.sample(pid)
            self._activity.prune(live_pids)

            # Two sessions in one worktree will interleave edits and neither
            # agent can tell. gitinfo memoizes the underlying git probe, so
            # this costs a dict build per tick.
            self._collisions = gitinfo.collisions([(a.session_id, a.cwd) for a in agents])
            # Read the decision queue once per tick rather than once per row:
            # it is a directory scan, and the render path walks every row.
            self._pending_decisions = permissions.pending()
            # Everything that watches for *changes* — the event log, the
            # notifier, ticket promotion — runs here, once per tick, before
            # anything is drawn.
            self._observe(agents)

            try:
                list_view = self.query_one(ListView)
            except NoMatches:
                # A refresh tick can land before compose has mounted the list,
                # or after the screen has started tearing down. Textual lets
                # the exception escape the timer callback and kills the app,
                # so treat a missing list as "nothing to draw" and let the
                # next tick catch up.
                return
            visible = self._visible_order(agents)
            self._agents_by_sid = {a.session_id: a for a in agents}
            new_sids = [row_id for row_id, _ in visible]

            # If the user just switched their terminal focus (e.g. clicked a
            # different Ghostty window hosting session X) auto-highlight that
            # session. follow_sid is None when nothing changed; we apply it
            # on top of whatever cursor each path computes, so user-driven
            # j/k navigation is preserved between focus switches.
            follow_sid = self._compute_follow_target(agents)

            # Path 1 — identical order. Just refresh row text in place.
            if new_sids == self._sid_by_row and self._rows_by_sid:
                self._update_rows(agents)
                self._apply_follow_target(list_view, follow_sid, new_sids)
                self._maybe_summarize_new(agents)
                self._update_chrome(agents)
                return

            # Path 2 — same set, different order. Refresh text and reorder rows
            # via move_child so the widget tree isn't torn down. This is the
            # dominant refresh case: sort-by-last_event_time means any hook
            # fire rearranges the list, which used to fall through to the
            # cold path's clear()+rebuild and produce a visible flash every
            # few seconds.
            new_set = set(new_sids)
            prev_set = set(self._sid_by_row)
            if self._rows_by_sid and new_set == prev_set and len(new_sids) == len(self._sid_by_row):
                cursor_sid = self._cursor_sid(list_view)
                self._update_rows(agents)
                self._reorder_list_view(list_view, new_sids)
                self._sid_by_row = list(new_sids)
                if cursor_sid is not None:
                    with contextlib.suppress(ValueError):
                        list_view.index = new_sids.index(cursor_sid)
                self._apply_follow_target(list_view, follow_sid, new_sids)
                self._maybe_summarize_new(agents)
                self._update_chrome(agents)
                return

            # Cold path — sessions added or removed. Build the new widget
            # tree first, THEN swap the DOM and the index/state mappings
            # together so action_jump never sees a half-updated dashboard.
            #
            # Why awaits matter here: ListView.clear()/mount() return
            # AwaitRemove/AwaitMount and the actual DOM mutation happens on
            # the next event-loop tick. The previous code fired both calls
            # synchronously and then set _sid_by_row + list_view.index
            # immediately, leaving a window in which list_view.index was
            # None (clear sets it None) but _sid_by_row was the new list —
            # so an Enter keypress landing in that window read index=None
            # and toasted "no row selected" even though the list looked
            # populated. With awaits + atomic post-swap, that window is
            # gone: either both see the OLD state or both see the NEW.
            prev_sid: str | None = self._cursor_sid(list_view)

            new_rows: dict[str, SessionRow] = {}
            new_headers: dict[str, GroupHeader] = {}
            new_items: dict[str, ListItem] = {}
            items: list[ListItem] = []
            cursor_target = 0
            speaking_sid = self._speaking_sid()
            members = self._group_members(agents)
            for i, (row_id, agent) in enumerate(visible):
                if agent is None:
                    key = group_key_of(row_id)
                    header = GroupHeader(self._group_header_markup(key, members.get(key, [])))
                    item = ListItem(header)
                    new_headers[row_id] = header
                else:
                    row = SessionRow()
                    self._paint_row(row, agent, speaking_sid)
                    item = ListItem(row)
                    new_rows[row_id] = row
                items.append(item)
                new_items[row_id] = item
                if prev_sid and row_id == prev_sid:
                    cursor_target = i

            await list_view.clear()
            if items:
                await list_view.mount(*items)

            # DOM is consistent — swap state mappings + index atomically.
            self._sid_by_row = list(new_sids)
            self._rows_by_sid = new_rows
            self._headers_by_id = new_headers
            self._items_by_sid = new_items
            if items:
                list_view.index = cursor_target
            else:
                list_view.index = None

            self._apply_follow_target(list_view, follow_sid, new_sids)
            self._maybe_summarize_new(agents)
            self._update_chrome(agents)
        finally:
            self._refreshing = False

    # --- change observation ----------------------------------------------

    def _observe(self, agents: list[AgentState]) -> None:
        """Notice what changed since the last tick, and act on it once.

        The dashboard repaints twice a second; almost nothing it draws is
        *new*. This is the one place that tells the difference, so the event
        log records transitions rather than frames, the notifier fires on the
        edge rather than continuously, and a confirmed ticket is promoted the
        first time it is seen rather than on every repaint.
        """
        seen: set[str] = set()
        for agent in agents:
            sid = agent.session_id
            seen.add(sid)
            match = match_for_agent(agent, self._summaries.get(sid), self._prs.cached(agent.cwd))
            ticket = match.key if match else ""
            # Promotion: write a fact down the first time a fact-tier probe
            # produces it, so a deleted branch can never demote it later.
            jira.promote(match, agent)

            previous = self._prev_status.get(sid)
            current = str(agent.status)
            if previous is None:
                eventlog.append(
                    eventlog.EventKind.SESSION_STARTED,
                    session_id=sid,
                    ticket=ticket,
                    detail=f"{agent.provider or 'agent'} in {agent.project_name or agent.cwd}",
                )
            elif previous != current:
                eventlog.append(
                    eventlog.EventKind.STATUS_CHANGED,
                    session_id=sid,
                    ticket=ticket,
                    detail=f"{previous} → {current}",
                )
            self._prev_status[sid] = current
            self._observe_pr(agent, ticket)
            self._maybe_notify(agent, ticket)

        for sid in [s for s in self._prev_status if s not in seen]:
            eventlog.append(eventlog.EventKind.SESSION_ENDED, session_id=sid)
            self._prev_status.pop(sid, None)
            self._prev_pr.pop(sid, None)
            self._selected.discard(sid)
            self._notifier.clear(sid)

    def _observe_pr(self, agent: AgentState, ticket: str) -> None:
        """Log PR/CI/review transitions and keep the work record current."""
        pr = self._prs.cached(agent.cwd)
        if pr is None:
            return
        sid = agent.session_id
        current = (pr.state, str(pr.ci), str(pr.review))
        previous = self._prev_pr.get(sid)
        if previous == current:
            return
        self._prev_pr[sid] = current
        if previous is None:
            eventlog.append(
                eventlog.EventKind.PR_LINKED,
                session_id=sid,
                ticket=ticket,
                detail=f"#{pr.number} {pr.state.lower()}",
                url=pr.url,
            )
        else:
            for kind, before, after in (
                (eventlog.EventKind.PR_STATE_CHANGED, previous[0], current[0]),
                (eventlog.EventKind.CI_CHANGED, previous[1], current[1]),
                (eventlog.EventKind.REVIEW_CHANGED, previous[2], current[2]),
            ):
                if before != after:
                    eventlog.append(
                        kind,
                        session_id=sid,
                        ticket=ticket,
                        detail=f"#{pr.number}: {before} → {after}",
                    )
        if ticket:
            work_items.record(
                ticket,
                session_id=sid,
                pr_number=pr.number,
                pr_url=pr.url,
                pr_state=pr.state,
            )

    def _maybe_notify(self, agent: AgentState, ticket: str) -> None:
        """Push a notification when this session starts needing a human.

        Deduping lives in the Notifier; this only decides *whether* the
        session currently wants something, and says so in one line.
        """
        from ephor.constants import ATTENTION_STATUSES

        label = ticket or agent.project_name or agent.session_id[:8]
        if agent.status in ATTENTION_STATUSES:
            reason = str(agent.status)
            self._notifier.notify(
                agent.session_id,
                reason,
                title=f"ephor: {label}",
                body=f"{reason.replace('_', ' ').lower()} — {agent.project_name or agent.cwd}",
                priority="high" if agent.status is AgentStatus.WAITING_PERMISSION else "default",
            )
            return
        work = self._work_attention(agent)
        if work is not None:
            self._notifier.notify(
                agent.session_id,
                str(work),
                title=f"ephor: {label}",
                body=WORK_ATTENTION_DISPLAY[work][1] + f" — {agent.project_name or agent.cwd}",
            )
            return
        # Nothing wanted any more: re-arm so the next block notifies at once
        # rather than waiting out the dedupe window.
        self._notifier.clear(agent.session_id)

    def _group_members(self, agents: list[AgentState]) -> dict[str, list[AgentState]]:
        """Sessions per group key, for the header counts."""
        if self._group_by == "none":
            return {}
        members: dict[str, list[AgentState]] = {}
        for agent in agents:
            members.setdefault(self._group_key(agent), []).append(agent)
        return members

    def _update_rows(self, agents: list[AgentState]) -> None:
        """Refresh in-place row content for every cached row and header."""
        speaking_sid = self._speaking_sid()
        for agent in agents:
            row = self._rows_by_sid.get(agent.session_id)
            if row is not None:
                self._paint_row(row, agent, speaking_sid)
        if not self._headers_by_id:
            return
        members = self._group_members(agents)
        for row_id, header in self._headers_by_id.items():
            key = group_key_of(row_id)
            header.update(self._group_header_markup(key, members.get(key, [])))

    def _speaking_sid(self) -> str | None:
        """Session id whose response is currently being read aloud, if any.

        Reads through the SpeechBar so we don't double-decode the speech
        log: the bar already maintains a fresh _state on its 200ms tick.
        """
        if self._speech_bar is None:
            return None
        return self._speech_bar.speaking_session_id

    # --- grouping ---------------------------------------------------------
    #
    # Group headers live in the same ListView as sessions, as rows whose id
    # is a sentinel no session id can collide with (state files key on a
    # UUID, and this starts with a NUL). Keeping them in `_sid_by_row` means
    # the existing index bookkeeping — cursor restore, reorder, follow-focus
    # — keeps working unchanged; every action that needs a *session* asks
    # `_cursor_session_sid`, which reads a header as "nothing selected".

    def _group_key(self, agent: AgentState) -> str:
        """Which group ``agent`` belongs to under the current mode."""
        if self._group_by == "ticket":
            return ticket_for_agent(agent, self._summaries.get(agent.session_id)) or "no ticket"
        if self._group_by == "repo":
            root = gitinfo.worktree_of(agent.cwd)
            return Path(root).name if root else (agent.project_name or "no repo")
        return ""

    def _visible_order(self, agents: list[AgentState]) -> list[tuple[str, AgentState | None]]:
        """The rows to draw, in order: (row id, agent or None for a header).

        Grouping sorts by group first so a ticket's sessions sit together,
        then preserves the stable (started_at, session_id) order inside each
        group — the property that keeps a row from jumping under the cursor
        is worth more than any ordering *between* groups.
        """
        if self._group_by == "none":
            return [(a.session_id, a) for a in agents]
        grouped: dict[str, list[AgentState]] = {}
        for agent in agents:
            grouped.setdefault(self._group_key(agent), []).append(agent)
        rows: list[tuple[str, AgentState | None]] = []
        # "no ticket" / "no repo" last: a named group is something you are
        # working on, the catch-all is everything else.
        for key in sorted(grouped, key=lambda k: (k.startswith("no "), k)):
            rows.append((f"{_GROUP_PREFIX}{key}", None))
            if key in self._collapsed:
                continue
            rows.extend((a.session_id, a) for a in grouped[key])
        return rows

    def _group_header_markup(self, key: str, members: list[AgentState]) -> str:
        """One header line: the group, how many sessions, how many need you."""
        from ephor.constants import ATTENTION_STATUSES

        collapsed = key in self._collapsed
        caret = "▸" if collapsed else "▾"
        needs = sum(1 for a in members if a.status in ATTENTION_STATUSES)
        working = sum(1 for a in members if a.status is AgentStatus.WORKING)
        parts = [f"[bold #58a6ff]{caret} {key}[/]", f"[dim]{len(members)} session(s)[/]"]
        if working:
            parts.append(f"[#A3BE8C]{working} working[/]")
        if needs:
            parts.append(f"[bold #BF616A]{needs} need you[/]")
        return "  " + "  ·  ".join(parts)

    # --- per-row context --------------------------------------------------

    def _row_context(self, agent: AgentState, pr: PullRequest | None) -> RowContext:
        """Assemble everything the row renderer needs beyond the agent itself."""
        ticket = ticket_for_agent(agent, self._summaries.get(agent.session_id))
        issue = self._issues.cached(ticket) if ticket else None
        queued = self._pending_decisions.get(agent.session_id)
        decision = ""
        if queued is not None:
            decision = f"{queued.decision}{' (standing)' if queued.standing else ''}"
        return RowContext(
            selected=agent.session_id in self._selected,
            collisions=self._collisions.get(agent.session_id, ()),
            jira_status=issue.status if issue else "",
            jira_title=issue.title if issue else "",
            drift=jira_drift(issue, pr.state if pr else "") or "",
            decision_queued=decision,
            failing_checks=pr.failing_checks if pr else (),
        )

    def _paint_row(self, row: SessionRow, agent: AgentState, speaking_sid: str | None) -> None:
        """Render one session row from the current caches."""
        pr = self._prs.cached(agent.cwd)
        row.update_agent(
            agent,
            samples=self._activity.samples_for(agent.agent_pid),
            summary=self._summaries.get(agent.session_id),
            tokens=self._tokens.total_for(agent),
            speaking=(agent.session_id == speaking_sid),
            pr=pr,
            context=self._row_context(agent, pr),
        )

    def _work_attention(self, agent: AgentState) -> WorkAttention | None:
        """Why this session's *work* needs a human, or None.

        Distinct from its agent status: a session can be perfectly idle and
        still be the most urgent thing on the board because its PR is red.
        """
        pr = self._prs.cached(agent.cwd)
        found = pr.attention() if pr is not None else None
        if found is not None:
            return found
        ticket = ticket_for_agent(agent, self._summaries.get(agent.session_id))
        issue = self._issues.cached(ticket) if ticket else None
        if issue is not None and pr is not None and jira_drift(issue, pr.state):
            return WorkAttention.TICKET_DRIFT
        return None

    def _reorder_list_view(self, list_view: ListView, target_order: list[str]) -> None:
        """Reorder ListView children to match target_order without rebuilding.

        Uses Widget.move_child to swap items into place. Children that are
        already in their target position are skipped.
        """
        for target_idx, sid in enumerate(target_order):
            item = self._items_by_sid.get(sid)
            if item is None:
                continue
            try:
                current_idx = list_view.children.index(item)
            except (ValueError, AttributeError):
                continue
            if current_idx == target_idx:
                continue
            list_view.move_child(item, before=target_idx)

    def _cursor_sid(self, list_view: ListView) -> str | None:
        """Resolve ListView's highlighted index to a session_id (or None)."""
        try:
            idx = list_view.index
        except AttributeError:
            return None
        if idx is None:
            return None
        try:
            return self._sid_by_row[idx]
        except IndexError:
            return None

    def _cursor_session_sid(self, list_view: ListView | None = None) -> str | None:
        """The session under the cursor, or None when it is on a group header."""
        view = list_view if list_view is not None else self.query_one(ListView)
        row_id = self._cursor_sid(view)
        if row_id is None or is_group_row(row_id):
            return None
        return row_id

    def _target_sids(self) -> list[str]:
        """Sessions the next action applies to: the marked set, else the cursor.

        Marking is what makes an action a batch action, and falling back to
        the cursor is what keeps every single-row keystroke working exactly
        as it did before anything could be marked.
        """
        if self._selected:
            # Preserve on-screen order so the toast reads the way the board
            # looks, and drop anything that has since disappeared.
            visible = [s for s in self._sid_by_row if not is_group_row(s)]
            return [sid for sid in visible if sid in self._selected]
        sid = self._cursor_session_sid()
        return [sid] if sid else []

    def _compute_follow_target(self, agents: list[AgentState]) -> str | None:
        """Return the sid to auto-highlight this tick, or None for no change.

        Detects the active pane in any tmux client OTHER than ours and, if
        it changed since last tick, returns the sid of the matching agent
        (or None if no agent owns that pane). We only react to changes so
        the user's j/k navigation isn't clobbered every 500ms.
        """
        # Tmux query is blocking I/O; swallow any failure. The dashboard
        # should keep working even if tmux flaps.
        try:
            external_pane = detect_focused_external_pane(self._self_pane or None)
        except Exception:  # noqa: BLE001 — tmux subprocess is best-effort
            return None
        if external_pane is None or external_pane == self._last_external_pane:
            # Either tmux had nothing useful to report, or the user hasn't
            # switched terminals since the last tick. Either way, leave the
            # cursor alone — j/k presses must not be silently overridden.
            return None
        self._last_external_pane = external_pane
        for agent in agents:
            if agent.tmux_pane == external_pane:
                return agent.session_id
        # External focus changed but no ephor session lives in that pane
        # (e.g. user pulled up a shell window). Don't move the cursor;
        # we still updated _last_external_pane so we won't keep retrying
        # against the same pane on every tick.
        return None

    def _apply_follow_target(
        self, list_view: ListView, follow_sid: str | None, new_sids: list[str]
    ) -> None:
        """Move the cursor to follow_sid if it's in the visible list.

        Silent on failure: the filter may have hidden the sid, or the row
        may not exist yet. The next refresh tick will retry.
        """
        if follow_sid is None:
            return
        with contextlib.suppress(ValueError):
            list_view.index = new_sids.index(follow_sid)

    def _maybe_summarize_new(self, agents: list[AgentState]) -> None:
        """Lazy-once: kick off a summary for any session we haven't summarized yet.

        Caller's responsibility to call after a refresh — this is cheap when
        all sessions are already cached or in-flight.
        """
        for agent in agents:
            sid = agent.session_id
            if sid in self._summarizing:
                continue
            if self._summaries.has(sid):
                continue
            # Skip until there's material to summarize — a captured reply, a
            # user prompt, or a transcript — so content-less sessions don't
            # trigger an LLM call on every refresh (empty results aren't
            # cached). agy has an out-of-band transcript (read by session id),
            # so it's never gated on the Claude-convention transcript path.
            has_material = (
                agent.provider == "agy"
                or agent.last_reply
                or agent.last_summary
                or transcript_path(agent.cwd, sid).exists()
            )
            if not has_material:
                continue
            self._summarize(
                sid,
                agent.cwd,
                manual=False,
                last_reply=agent.last_reply,
                provider=agent.provider,
                prompt=agent.last_summary,
            )

    def _update_chrome(self, agents: list[AgentState]) -> None:
        """Refresh the parts outside the session list (header / summary / title).

        These are cheap text-only Static.update() calls; they don't flash.
        Memoize the rendered strings so identical-content ticks are no-ops —
        Textual still repaints on update() regardless.
        """
        summary = StatusSummary.from_agents(agents)
        if self._header_bar is not None:
            self._header_bar.update_summary(summary)
        if self._summary_line is not None:
            self._summary_line.update(
                _render_summary_line(
                    summary,
                    agents,
                    self._activity,
                    self._tokens,
                    weekly_cap=self._account.weekly_cap_tokens,
                    usage=self._usage,
                    account_usage=self._account_usage,
                )
            )
        self.sub_title = f"{len(agents)} session(s)"

    # ---- actions --------------------------------------------------------

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Backstop for Enter: ListView's own Selected event also fires the jump."""
        del event  # unused
        await self.action_jump()

    async def action_refresh(self) -> None:
        """Redraw, and force a fresh ticket + PR harvest. Bound to `r`.

        Both caches exist to keep subprocesses off the render path, which
        means a long-lived session can outlast the truth: the branch moved,
        the PR merged, or a PR was opened after we last looked. `r` is the
        escape hatch — it drops every memoized answer so the next sweep
        re-probes `git` and `gh` from scratch, rather than making the user
        restart ephor or wait out an hour-long TTL.
        """
        clear_cwd_cache()
        self._prs.invalidate()
        await self._refresh_table()
        self._refresh_prs()
        self._set_toast("refreshed — re-resolving tickets and pull requests")

    def action_filter(self) -> None:
        """Reveal the filter input and focus it. '/' enters this state."""
        if self._filter_input is None:
            return
        self._filter_input.display = True
        self._filter_input.value = self._filter
        self._filter_input.focus()

    async def action_clear_filter(self) -> None:
        """Dismiss whichever prompt is open, and reset the filter. Bound to escape.

        Escape is the universal "get me out of here" key, so it has to
        abandon the ticket prompt as well — leaving it open and focused
        would swallow j/k exactly the way the filter input used to.
        """
        if self._ticket_input is not None and self._ticket_input.display:
            self._close_ticket_input()
            return
        self._filter = ""
        if self._filter_input is not None:
            self._filter_input.value = ""
            self._filter_input.display = False
        self.query_one(ListView).focus()
        await self._refresh_table()

    async def on_input_changed(self, event: Input.Changed) -> None:
        """Live-filter as the user types in the filter input."""
        if self._filter_input is None or event.input is not self._filter_input:
            return
        self._filter = event.value
        await self._refresh_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Backstop for Enter while the filter input is focused.

        The priority `enter` binding (action_jump) currently consumes the
        Enter keystroke before this fires, but action_jump now hides the
        input itself. This handler is left in place as belt-and-braces so
        that any future binding change (or platform where the priority
        binding doesn't fire first) still gets the filter dismissed.
        """
        if self._ticket_input is not None and event.input is self._ticket_input:
            self.run_worker(self._submit_ticket_pin(event.value), exclusive=False)
            return
        if self._filter_input is None or event.input is not self._filter_input:
            return
        self._filter_input.display = False
        self.query_one(ListView).focus()

    def action_pin_ticket(self) -> None:
        """Prompt for the ticket this session is on. Bound to `p`.

        Every other probe in the chain is inference; this is the one place
        the user gets to simply say. The answer outranks all of them and
        persists across restarts, so a session on a shared checkout with a
        generic branch name stops being a permanent "—".
        """
        if self._ticket_input is None:
            return
        try:
            list_view = self.query_one(ListView)
        except NoMatches:
            return
        sid = self._cursor_session_sid(list_view)
        if sid is None:
            self._set_toast("select a session row first")
            return
        self._pin_target = sid
        self._ticket_input.value = ticket_pins.get(sid) or ""
        self._ticket_input.display = True
        self._ticket_input.focus()

    def _close_ticket_input(self) -> None:
        """Dismiss the pin prompt and hand focus back to the list.

        Focus matters more than the hiding: a prompt left focused swallows
        j/k, which reads to the user as the dashboard freezing.
        """
        self._pin_target = None
        if self._ticket_input is not None:
            self._ticket_input.value = ""
            self._ticket_input.display = False
        with contextlib.suppress(NoMatches):
            self.query_one(ListView).focus()

    async def _submit_ticket_pin(self, raw: str) -> None:
        """Apply (or clear) the pin the user just typed."""
        sid = self._pin_target
        self._close_ticket_input()
        if sid is None:
            return
        ticket = raw.strip()
        if not ticket:
            ticket_pins.unpin(sid)
            self._set_toast(f"unpinned {sid[:8]} — back to the inferred ticket")
        else:
            ticket_pins.pin(sid, ticket)
            self._set_toast(f"pinned {ticket} to {sid[:8]}")
        # A pin changes which PR we would search for, so drop this
        # session's cached answer and let the next sweep re-probe.
        agent = next((a for a in self._manager.scan() if a.session_id == sid), None)
        if agent is not None:
            self._prs.invalidate(agent.cwd)
        await self._refresh_table()
        self._refresh_prs()

    def action_cursor_down(self) -> None:
        self.query_one(ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(ListView).action_cursor_up()

    def action_next_attention(self) -> None:
        """Move cursor to the next row that needs a human, wrapping.

        "Needs a human" is deliberately broader than the agent-status machine:
        alongside PERM / WAIT / ERR it walks work-level attention — failing
        CI, a conflict, requested changes, a review nobody has done, a ticket
        that has drifted from its PR. Those rows are usually sitting in IDLE,
        which is exactly why they were invisible before: the agent finished,
        so the status column has nothing left to say, and the work is still
        not done.
        """
        from ephor.constants import ATTENTION_STATUSES

        list_view = self.query_one(ListView)
        # Iterate _sid_by_row (the dashboard's view of the world) rather than
        # scan() to stay in sync with what the user actually sees on screen.
        if not self._sid_by_row:
            self._set_toast("no sessions")
            return

        # Pull current statuses from the latest scan so we don't reuse a
        # stale snapshot; map sid → status for visible rows only.
        agents_by_sid = {a.session_id: a for a in self._manager.scan()}
        reasons: dict[int, str] = {}
        for i, sid in enumerate(self._sid_by_row):
            agent = agents_by_sid.get(sid) if not is_group_row(sid) else None
            if agent is None:
                continue
            if agent.status in ATTENTION_STATUSES:
                reasons[i] = str(agent.status)
                continue
            work = self._work_attention(agent)
            if work is not None:
                reasons[i] = WORK_ATTENTION_DISPLAY[work][1]
        if not reasons:
            self._set_toast("no rows need attention")
            return

        attention_indices = sorted(reasons)
        cursor_idx = list_view.index if list_view.index is not None else -1
        target = next(
            (i for i in attention_indices if i > cursor_idx),
            attention_indices[0],
        )
        list_view.index = target
        self._set_toast(f"→ {self._sid_by_row[target][:8]} ({reasons[target]})")

    # --- selection & grouping --------------------------------------------

    def action_toggle_select(self) -> None:
        """Mark or unmark the row under the cursor for a batch action."""
        list_view = self.query_one(ListView)
        row_id = self._cursor_sid(list_view)
        if row_id is None:
            self._set_toast("no row selected")
            return
        if is_group_row(row_id):
            # Space on a header marks the whole group — the reason to group
            # rows in the first place is to act on them together.
            key = group_key_of(row_id)
            members = [
                sid
                for sid in self._sid_by_row
                if not is_group_row(sid)
                and self._agents_by_sid.get(sid) is not None
                and self._group_key(self._agents_by_sid[sid]) == key
            ]
            if all(sid in self._selected for sid in members) and members:
                self._selected.difference_update(members)
                self._set_toast(f"unmarked {len(members)} in {key}")
            else:
                self._selected.update(members)
                self._set_toast(f"marked {len(members)} in {key}")
            return
        if row_id in self._selected:
            self._selected.discard(row_id)
        else:
            self._selected.add(row_id)
        self._set_toast(f"{len(self._selected)} marked")
        list_view.action_cursor_down()

    def action_clear_selection(self) -> None:
        """Unmark every row."""
        count = len(self._selected)
        self._selected.clear()
        self._set_toast(f"cleared {count} mark(s)" if count else "nothing was marked")

    def action_cycle_grouping(self) -> None:
        """Cycle none → ticket → repo. A flat list is hostile at 25 rows."""
        index = self.GROUPINGS.index(self._group_by) if self._group_by in self.GROUPINGS else 0
        self._group_by = self.GROUPINGS[(index + 1) % len(self.GROUPINGS)]
        # Group names differ per mode, so folds from the previous mode would
        # hide nothing and confuse everything.
        self._collapsed.clear()
        self._set_toast(f"grouping: {self._group_by}")

    def action_toggle_collapse(self) -> None:
        """Fold or unfold the group at the cursor."""
        if self._group_by == "none":
            self._set_toast("not grouped — press g first")
            return
        row_id = self._cursor_sid(self.query_one(ListView))
        if row_id is None:
            self._set_toast("no row selected")
            return
        if is_group_row(row_id):
            key = group_key_of(row_id)
        else:
            agent = self._agents_by_sid.get(row_id)
            if agent is None:
                self._set_toast("no group here")
                return
            key = self._group_key(agent)
        if key in self._collapsed:
            self._collapsed.discard(key)
            self._set_toast(f"expanded {key}")
        else:
            self._collapsed.add(key)
            self._set_toast(f"collapsed {key}")

    # --- permission inbox -------------------------------------------------

    def action_allow(self) -> None:
        """Answer "allow" for the marked sessions (or the cursor row)."""
        self._decide(permissions.Decision.ALLOW, standing=False)

    def action_deny(self) -> None:
        """Answer "deny" for the marked sessions (or the cursor row)."""
        self._decide(permissions.Decision.DENY, standing=False)

    def action_allow_standing(self) -> None:
        """Allow everything these sessions ask, until revoked with D."""
        self._decide(permissions.Decision.ALLOW, standing=True)

    def action_revoke(self) -> None:
        """Withdraw any queued or standing decision for the target sessions."""
        sids = self._target_sids()
        if not sids:
            self._set_toast("no row selected")
            return
        revoked = sum(1 for sid in sids if permissions.revoke(sid))
        self._set_toast(f"revoked {revoked} decision(s)")

    def _decide(self, decision: permissions.Decision, *, standing: bool) -> None:
        """Write permission answers, and say plainly when they will take effect.

        The honesty matters. The hook runs *before* the agent draws its
        prompt, so unless the user has opted into a wait window
        (EPHOR_PERMISSION_WAIT_SEC) a decision written now answers the
        session's *next* request, not the one on screen. Reporting that as
        "unblocked" would be a lie the user only discovers by watching a
        session stay stuck.
        """
        sids = self._target_sids()
        if not sids:
            self._set_toast("no row selected")
            return
        written = 0
        for sid in sids:
            if permissions.decide(sid, decision, standing=standing):
                written += 1
                eventlog.append(
                    eventlog.EventKind.PERMISSION_DECIDED,
                    session_id=sid,
                    ticket=self._ticket_for_sid(sid),
                    detail=f"{decision}{' (standing)' if standing else ''}",
                )
        scope = "standing " if standing else ""
        if permissions.hook_wait_sec() > 0:
            when = "now"
        elif standing:
            when = "applies from this session's next request"
        else:
            when = "queued for the next request (set EPHOR_PERMISSION_WAIT_SEC to answer live)"
        self._set_toast(f"{scope}{decision} x{written} — {when}")

    def _ticket_for_sid(self, sid: str) -> str:
        """Ticket for a session id, from the last painted snapshot."""
        agent = self._agents_by_sid.get(sid)
        if agent is None:
            return ""
        return ticket_for_agent(agent, self._summaries.get(sid)) or ""

    # --- launching --------------------------------------------------------

    def action_new_session(self) -> None:
        """Start another session on the cursor row's ticket.

        The second attempt at a ticket is a normal thing to want — the first
        went down a dead end, or you want a second agent on an independent
        part of it — and until now it meant leaving the dashboard.
        """
        sid = self._cursor_session_sid()
        if sid is None:
            self._set_toast("select a session row first")
            return
        ticket = self._ticket_for_sid(sid)
        if not ticket:
            self._set_toast("no ticket on this row — pin one with p")
            return
        agent = self._agents_by_sid.get(sid)
        self._start_work(ticket, agent.provider if agent else "claude", agent.cwd if agent else "")

    def action_resume_session(self) -> None:
        """Reopen the cursor row's session in a fresh tmux window."""
        sid = self._cursor_session_sid()
        if sid is None:
            self._set_toast("select a session row first")
            return
        agent = self._agents_by_sid.get(sid)
        if agent is None:
            self._set_toast("that session is gone")
            return
        self._resume_worker(sid, agent.provider or "claude", agent.cwd, self._ticket_for_sid(sid))

    @work(thread=True, exit_on_error=False, group="launch")
    def _start_work(self, ticket: str, provider: str, cwd: str) -> None:
        """Create the worktree and window off the event loop — git is slow."""
        result = launcher.start(ticket, provider=provider or "claude", cwd=cwd or None)
        self.call_from_thread(self._set_toast, f"start {ticket}: {result.message}")

    @work(thread=True, exit_on_error=False, group="launch")
    def _resume_worker(self, sid: str, provider: str, cwd: str, ticket: str) -> None:
        result = launcher.resume(sid, provider=provider, cwd=cwd, ticket=ticket)
        self.call_from_thread(self._set_toast, result.message)

    async def action_kill(self) -> None:
        """Two-press kill: first press arms, second within 3s fires.

        Killing tears down the claude process, the tmux window, and the
        on-disk state file. No undo — the confirmation window is the only
        guard against accidental presses. Marked rows are killed together,
        and the arming key is the whole marked set, so arming on one row and
        then changing the selection cannot fire against the wrong sessions.
        """
        sids = self._target_sids()
        if not sids:
            self._set_toast("no row selected")
            return

        by_sid = {a.session_id: a for a in self._manager.scan()}
        agents = [by_sid[sid] for sid in sids if sid in by_sid]
        if not agents:
            self._set_toast("those sessions disappeared between refreshes")
            self._kill_armed_sid = None
            return

        arm_key = "\x00".join(sids)
        label = (
            f"{len(agents)} sessions"
            if len(agents) > 1
            else (agents[0].project_name or agents[0].session_id[:8])
        )
        now = time.monotonic()
        armed = (
            self._kill_armed_sid == arm_key and now - self._kill_armed_at < KILL_CONFIRM_WINDOW_SEC
        )

        if not armed:
            self._kill_armed_sid = arm_key
            self._kill_armed_at = now
            self._set_toast(f"press x again within 3s to kill {label}")
            return

        self._kill_armed_sid = None
        killed = 0
        failures: list[str] = []
        for agent in agents:
            outcome = kill_session(agent, self._manager.directory)
            # Drop any cached summary so a future session reusing the sid
            # doesn't display stale text. Best-effort.
            self._summaries.delete(agent.session_id)
            self._selected.discard(agent.session_id)
            if outcome.ok:
                killed += 1
            else:
                failures.append(outcome.detail)
        if failures:
            self._set_toast(f"killed {killed}/{len(agents)} — {failures[0]}")
        else:
            self._set_toast(f"killed {label}")
        await self._refresh_table()

    def action_summarize(self) -> None:
        """Force-refresh the summary for the highlighted session.

        Useful when the conversation has progressed beyond what was cached
        on first sight. Rate-limited via the in-flight set: pressing `s`
        repeatedly is a no-op while a previous call is still pending.
        """
        sids = self._target_sids()
        if not sids:
            self._set_toast("no row selected")
            return
        by_sid = {a.session_id: a for a in self._manager.scan()}
        started = 0
        for sid in sids:
            agent = by_sid.get(sid)
            if agent is None or sid in self._summarizing:
                continue
            self._summarize(
                sid,
                agent.cwd,
                manual=True,
                last_reply=agent.last_reply,
                provider=agent.provider,
                prompt=agent.last_summary,
            )
            started += 1
        if not started:
            self._set_toast("already summarizing…")
        elif started > 1:
            self._set_toast(f"summarizing {started} sessions…")

    async def action_jump(self) -> None:
        list_view = self.query_one(ListView)

        # Enter inside the ticket prompt means "save this pin", not "jump".
        # Same priority-binding quirk as the filter input below: the `enter`
        # binding fires before Input.Submitted, so the prompt has to be
        # handled here or the keystroke is swallowed by a jump.
        if (
            self._ticket_input is not None
            and self._ticket_input.display
            and self.focused is self._ticket_input
        ):
            await self._submit_ticket_pin(self._ticket_input.value)
            return

        # If the user pressed Enter while the filter input was focused, we
        # interpret that as "commit the filter and jump to the highlighted
        # match." The priority `enter` binding fires this action BEFORE
        # Input.Submitted gets a chance, so unless we close the input here
        # the filter stays open with focus, and j/k stop navigating —
        # exactly the symptom the user reports as "Enter is broken".
        if (
            self._filter_input is not None
            and self._filter_input.display
            and self.focused is self._filter_input
        ):
            self._filter_input.display = False
            list_view.focus()

        # Enter on a group header folds it. There is nothing to jump to, and
        # fold/unfold is the only thing a header can usefully do.
        row_id = self._cursor_sid(list_view)
        if is_group_row(row_id):
            self.action_toggle_collapse()
            return

        sid = row_id
        if sid is None:
            self._set_toast("no row selected")
            return

        agent = next(
            (a for a in self._manager.scan() if a.session_id == sid),
            None,
        )
        if agent is None:
            self._set_toast(f"session {sid[:8]} disappeared between refreshes")
            return

        outcome = jump_to(agent)
        if outcome.ok:
            self._set_toast(f"→ jumped to {agent.project_name or sid[:8]}")
            return

        # Retry with re-discovery when the recorded tmux ref is stale:
        #   - NO_TMUX_INFO: state file never had tmux fields (started outside ephor)
        #   - FAILED with "can't find {pane,window,session}": claude moved to a
        #     new pane (e.g. user closed the window and `claude --resume`d,
        #     leaving the recorded pane_id pointing at a dead pane).
        # enrich_state_files re-walks /proc + tmux and overwrites the state
        # file when it finds the live agent_pid in a different pane.
        if _is_stale_tmux_ref(outcome):
            self._set_toast("looking up tmux pane…")
            updated = enrich_state_files(self._manager.directory)
            if updated:
                refreshed = next(
                    (a for a in self._manager.scan() if a.session_id == sid),
                    None,
                )
                if refreshed is not None:
                    outcome = jump_to(refreshed)
                    if outcome.ok:
                        await self._refresh_table()
                        self._set_toast(
                            f"→ jumped to {refreshed.project_name or sid[:8]} (auto-discovered)"
                        )
                        return
        self._set_toast(_jump_error(outcome.result, outcome.detail))

    async def action_jump_speaking(self) -> None:
        """Jump to whichever session is currently being read aloud by TTS.

        Reads the SpeechBar's current speaker — a small indirection that
        keeps the speech-state computation out of this module. If no
        session is speaking, surface that as a toast rather than silently
        no-oping; users press `t` precisely when they expect a jump.
        """
        sid: str | None = None
        if self._speech_bar is not None:
            sid = self._speech_bar.speaking_session_id
        if not sid:
            self._set_toast("no session speaking")
            return
        agent = next(
            (a for a in self._manager.scan() if a.session_id == sid),
            None,
        )
        if agent is None:
            self._set_toast(f"speaking session {sid[:8]} not in dashboard yet")
            return
        outcome = jump_to(agent)
        if outcome.ok:
            self._set_toast(f"→ jumped to {agent.project_name or sid[:8]} (speaking)")
            return
        self._set_toast(_jump_error(outcome.result, outcome.detail))

    def action_toggle_mute(self) -> None:
        """Flip the TTS mute state. Saves to ~/.config/ephor/
        speech.json so the choice persists across ephor restarts.

        The EPHOR_TTS_ENABLED env var, if set, will still override this
        on next launch — we surface that fact in the toast so the user
        isn't surprised when their hotkey toggle "doesn't stick."
        """
        new_muted = not self._speech_player.is_muted
        self._speech_player.set_muted(new_muted)
        try:
            save_speech_settings(enabled=not new_muted)
        except OSError as exc:
            # Best-effort persistence — keep the in-memory toggle even
            # if disk write fails (e.g. read-only home dir).
            self._set_toast(f"muted (couldn't save: {exc})")
            return

        msg = "🔇 TTS muted" if new_muted else "🔊 TTS unmuted"

        from ephor.speech_settings import ENV_VAR, SettingsSource

        if self._speech_settings.source == SettingsSource.ENV:
            msg += f" (note: {ENV_VAR} env will reapply on next launch)"
        self._set_toast(msg)
        # Force the bar to redraw immediately so the user sees the icon
        # change without waiting for the 200ms tick.
        if self._speech_bar is not None:
            self._speech_bar.refresh_now()

    def action_toggle_speak_mode(self) -> None:
        """Flip the TTS speak mode (full ↔ summary) and persist.

        Summary mode reuses the dashboard's `claude -p` summarizer, so
        the user's subscription auth carries through — no API key
        configuration required. The change applies to the NEXT Stop
        event; anything already queued / playing keeps its original
        text so the user isn't surprised by a mid-sentence swap.
        """
        from ephor.speech_settings import (
            MODE_ENV_VAR,
            SPEAK_MODE_FULL,
            SPEAK_MODE_SUMMARY,
            SettingsSource,
        )

        new_mode = (
            SPEAK_MODE_SUMMARY
            if self._speech_player.speak_mode == SPEAK_MODE_FULL
            else SPEAK_MODE_FULL
        )
        self._speech_player.set_speak_mode(new_mode)
        try:
            save_speech_settings(speak_mode=new_mode)
        except OSError as exc:
            # In-memory toggle still applies; only the disk write failed.
            self._set_toast(f"speak mode → {new_mode} (couldn't save: {exc})")
            return

        # Reload so the cached mode_source on the app stays accurate for
        # the next env-override toast.
        self._speech_settings = load_speech_settings()

        if new_mode == SPEAK_MODE_SUMMARY:
            msg = "📝 TTS: summary mode (brief notifications)"
        else:
            msg = "📜 TTS: full mode (whole reply)"
        if self._speech_settings.mode_source == SettingsSource.ENV:
            msg += f" (note: {MODE_ENV_VAR} env will reapply on next launch)"
        self._set_toast(msg)

    # ---- utilities ------------------------------------------------------

    def _set_toast(self, text: str) -> None:
        if self._toast is not None:
            self._toast.update(text)

    def _fetch_usage_now(self) -> None:
        """Schedule a usage fetch in a worker thread (fire-and-forget)."""
        self._fetch_usage()

    @work(thread=True, exit_on_error=False, group="usage", exclusive=True)
    def _fetch_usage(self) -> None:
        """Background worker: ccusage scan + per-account anchor management.

        ``exclusive=True`` collapses overlapping ticks — ccusage runs are
        3-5s so a slow tick can still be in flight when the next fires.

        Pipeline each tick:
          1. ccusage blocks + weekly (always — fast feedback for the strip).
          2. Read active fingerprint from ``.credentials.json``.
          3. If fingerprint changed since last tick OR the latest anchor
             for this account is stale, hit ``/api/oauth/usage`` once and
             record a new anchor (rate limit is ~1/hr/account so this is
             well within budget).
          4. Compute display-ready per-account usage = anchor + ccusage
             delta extrapolation.

        All the disk I/O (cache writes, store saves) runs here on the
        worker thread so the UI thread only sees the final dataclass.
        """
        snapshot = fetch_usage(five_hour_cap_tokens=self._account.five_hour_cap_tokens)
        fp = read_active_fingerprint()
        account_usage = self._refresh_account_anchor(fp, snapshot)
        self.call_from_thread(self._on_usage_done, snapshot, fp, account_usage)

    def _refresh_account_anchor(
        self,
        fp: AccountFingerprint | None,
        ccusage: UsageSnapshot,
    ) -> AccountUsage | None:
        """Anchor management on the worker thread. Returns the display-ready
        AccountUsage (or None when API-key user / no anchor possible).

        Side-effects: may add an entry to ``self._account_store`` and
        persist via ``save_account_store``. We mutate the store in-place
        because the TUI's only reader is the UI-thread render path which
        reads ``self._account_usage`` (a frozen dataclass), not the store.
        """
        if fp is None:
            return None
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        now = _dt.now(_UTC)
        state = self._account_store.get(fp.fp)
        # Refresh trigger: fingerprint not yet seen, anchor stale, or its
        # window rolled over. The endpoint is per-account rate-limited so
        # this is naturally bounded — switching accounts only fires one
        # extra request total.
        if account_anchor_needs_refresh(state, now=now):
            oauth = fetch_oauth_usage()
            if oauth.error is None and oauth.five_hour_pct is not None:
                state = record_account_anchor(
                    self._account_store,
                    fp,
                    server_5h_pct=oauth.five_hour_pct,
                    server_7d_pct=oauth.seven_day_pct or 0.0,
                    server_5h_resets_at=oauth.five_hour_resets_at,
                    server_7d_resets_at=oauth.seven_day_resets_at,
                    ccusage_5h_tokens=ccusage.five_hour.tokens if ccusage.five_hour else 0,
                    ccusage_7d_tokens=ccusage.seven_day.tokens if ccusage.seven_day else 0,
                    now=now,
                )
                save_account_store(self._account_store)
        # Resolve per-profile config caps; user intent beats inference.
        cfg_5h, cfg_7d = caps_for(fp, self._account.profiles)
        return compute_account_usage(
            state,
            ccusage_5h_tokens=ccusage.five_hour.tokens if ccusage.five_hour else None,
            ccusage_7d_tokens=ccusage.seven_day.tokens if ccusage.seven_day else None,
            config_5h_cap=cfg_5h or self._account.five_hour_cap_tokens,
            config_7d_cap=cfg_7d or self._account.weekly_cap_tokens,
            now=now,
        )

    def _on_usage_done(
        self,
        snapshot: UsageSnapshot,
        fp: AccountFingerprint | None,
        account_usage: AccountUsage | None,
    ) -> None:
        """Main-thread callback: stash the snapshots for the next render tick.

        Merge rule for the cross-account strip: a transient ccusage failure
        shouldn't blank a good reading. Per-account usage is replaced
        wholesale because its anchor + delta math is internally consistent.
        """
        self._usage = merge_with_previous(snapshot, self._usage)
        write_cached_snapshot(snapshot)
        self._account_fingerprint = fp
        self._account_usage = account_usage

    @work(thread=True, exit_on_error=False, group="summarize")
    def _summarize(
        self,
        sid: str,
        cwd: str,
        manual: bool,
        last_reply: str = "",
        provider: str = "",
        prompt: str = "",
    ) -> None:
        """Background-thread worker: summarize the session, store the result.

        Each agent is summarized from the richest source it offers:
        - Claude reads its Claude-format transcript (multi-turn context).
        - Antigravity (agy) has no hook-supplied transcript path, so
          ``summarize_agy`` locates its brain transcript by session id.
        - Every other agent falls back to the reply captured at turn-end
          (``last_reply``), paired with the latest user prompt (``prompt``,
          the session's ``last_summary``) so the model has task context rather
          than a lone stray sentence.

        Lazy-once policy: the refresh path only schedules when there's no cached
        summary; manual=True force-refreshes via `s`. De-duped via
        ``self._summarizing``.
        """
        if sid in self._summarizing:
            return
        self._summarizing.add(sid)
        try:
            if manual:
                # Show progress toast on the UI thread.
                self.call_from_thread(self._set_toast, f"summarizing {sid[:8]}…")
            if provider == "agy":
                # agy: rich transcript read straight from its brain dir.
                had_material = True
                text = summarize_agy(sid, cwd=cwd)
            else:
                # summarize_transcript returns "" for a missing/foreign
                # transcript (non-Claude), so try it first (rich for Claude).
                path = transcript_path(cwd, sid)
                had_material = path.exists() or bool(last_reply) or bool(prompt)
                text = summarize_transcript(path, cwd=cwd)
            if not text and (last_reply or prompt):
                # Fall back to the captured reply + user-prompt context.
                text = summarize_text(last_reply, cwd=cwd, prompt=prompt)
            self.call_from_thread(self._on_summary_done, sid, text, manual, had_material)
        finally:
            self._summarizing.discard(sid)

    def _on_summary_done(
        self, sid: str, text: str, manual: bool, had_material: bool = True
    ) -> None:
        """Main-thread callback: persist the result and repaint the row."""
        if text:
            self._summaries.set(sid, text)
            row = self._rows_by_sid.get(sid)
            if row is not None:
                # Re-render just this row in place — no flash, no full rebuild.
                agent = next(
                    (a for a in self._manager.scan() if a.session_id == sid),
                    None,
                )
                if agent is not None:
                    row.update_agent(
                        agent,
                        samples=self._activity.samples_for(agent.agent_pid),
                        summary=text,
                        pr=self._prs.cached(agent.cwd),
                    )
            if manual:
                self._set_toast(f"summary updated for {sid[:8]}")
        elif manual:
            # Manual press deserves an accurate explanation. Distinguish "there
            # was nothing to summarize yet" (a new/mid-turn non-Claude session
            # whose reply hasn't been captured) from a real backend failure —
            # the latter is what unavailable_reason() addresses.
            if not had_material:
                self._set_toast(
                    f"no summary yet for {sid[:8]} — waiting for the session's "
                    "first completed reply"
                )
            else:
                self._set_toast(unavailable_reason())

    # ---- pull requests --------------------------------------------------

    def action_open_pr(self, sid: str | None = None) -> None:
        """Open the pull request for a session in the browser.

        Bound to ``o`` for the highlighted row, and dispatched by clicking
        the Jira cell (which passes the row's session id). When the PR
        isn't cached yet we kick off a lookup and open it when it lands, so
        a click always does *something* — the alternative (a dead-looking
        key until the background sweep catches up) is worse.
        """
        if sid is None:
            # No explicit row (a keypress, not a click): open every marked
            # row's review at once, which is how a batch of finished work
            # actually gets looked at.
            marked = self._target_sids()
            if len(marked) > 1:
                self._open_many_prs(marked)
                return
            sid = marked[0] if marked else None
        if sid is None:
            self._set_toast("no row selected")
            return
        agent = next((a for a in self._manager.scan() if a.session_id == sid), None)
        if agent is None:
            self._set_toast(f"session {sid[:8]} disappeared between refreshes")
            return
        pr = self._prs.cached(agent.cwd)
        if pr is not None:
            self._open_pr(pr)
            return
        # No cached PR. Drop any memoized miss first: the usual reason a
        # user presses `o` twice is that they just pushed the branch, and
        # re-serving the 90s-old "nothing here" would make the key look
        # broken. An explicit keypress is worth one fresh `gh` call.
        clear_cwd_cache()
        self._prs.invalidate(agent.cwd)
        ticket = ticket_for_agent(agent, self._summaries.get(sid), pr)
        self._set_toast(f"looking up pull request for {ticket or agent.project_name or sid[:8]}…")
        self._lookup_pr(sid, agent.cwd, ticket, announce=True)

    def _open_pr(self, pr: PullRequest) -> None:
        self.open_url(pr.url)
        self._set_toast(f"opening PR #{pr.number} — {pr.url}")

    def _open_many_prs(self, sids: list[str]) -> None:
        """Open every marked row's pull request, and say what had none.

        Only cached PRs are opened. Resolving the misses would mean a `gh`
        round trip per session before the first tab appeared, and a batch
        action that stalls for ten seconds is one nobody presses twice.
        """
        opened = 0
        missing = 0
        for sid in sids:
            agent = self._agents_by_sid.get(sid)
            pr = self._prs.cached(agent.cwd) if agent else None
            if pr is None:
                missing += 1
                continue
            self.open_url(pr.url)
            opened += 1
        note = f" ({missing} had no PR yet)" if missing else ""
        self._set_toast(f"opened {opened} pull request(s){note}")

    @work(thread=True, exit_on_error=False, group="pr-lookup")
    def _lookup_pr(self, sid: str, cwd: str, ticket: str | None, announce: bool = False) -> None:
        """Background-thread worker: resolve one session's PR via `gh`.

        ``announce=True`` means a human is waiting on this (they pressed
        ``o`` or clicked the key), so open the result — or explain the
        miss — instead of silently warming the cache.
        """
        if sid in self._pr_lookup_in_flight:
            return
        self._pr_lookup_in_flight.add(sid)
        try:
            pr = self._prs.resolve(cwd, ticket)
        finally:
            self._pr_lookup_in_flight.discard(sid)
        self.call_from_thread(self._on_pr_resolved, sid, pr, ticket, announce)

    def _on_pr_resolved(
        self, sid: str, pr: PullRequest | None, ticket: str | None, announce: bool
    ) -> None:
        """Main-thread callback: repaint the row, and open if asked."""
        if pr is not None:
            row = self._rows_by_sid.get(sid)
            if row is not None:
                agent = next((a for a in self._manager.scan() if a.session_id == sid), None)
                if agent is not None:
                    row.update_agent(
                        agent,
                        samples=self._activity.samples_for(agent.agent_pid),
                        summary=self._summaries.get(sid),
                        pr=pr,
                    )
            if announce:
                self._open_pr(pr)
            return
        if announce:
            label = ticket or sid[:8]
            self._set_toast(
                f"no pull request found for {label} — is the branch pushed, and is `gh` "
                "installed and authenticated?"
            )

    def _refresh_prs(self) -> None:
        """Sweep visible sessions and queue `gh` lookups for stale entries.

        Runs on the UI thread but does no I/O itself: it only decides which
        directories still need probing (PrResolver's TTL does the
        throttling) and hands the batch to a single worker.
        """
        visible = set(self._sid_by_row)
        if not visible:
            return
        pending: list[tuple[str, str, str | None]] = []
        for state in self._manager.scan():
            sid = state.session_id
            if sid not in visible or not state.cwd:
                continue
            if sid in self._pr_lookup_in_flight or not self._prs.needs_refresh(state.cwd):
                continue
            # Feed the cached PR back in: when the branch name says nothing,
            # the PR's own title/body is where the key lives, and that call
            # has already been paid for.
            ticket = ticket_for_agent(state, self._summaries.get(sid), self._prs.cached(state.cwd))
            pending.append((sid, state.cwd, ticket))
        if pending:
            self._sweep_prs(pending)

    @work(thread=True, exit_on_error=False, group="pr-sweep", exclusive=True)
    def _sweep_prs(self, pending: list[tuple[str, str, str | None]]) -> None:
        """Background-thread worker: probe a batch of sessions, one at a time.

        Serial on purpose. Each miss is a `gh` round trip of up to a second;
        firing one worker per session would put a dozen network calls in
        flight every sweep on a busy dashboard for a decoration, not a
        blocker. exclusive=True means a slow sweep is superseded by the
        next one rather than piling up.
        """
        for sid, cwd, ticket in pending:
            if sid in self._pr_lookup_in_flight:
                continue
            self._pr_lookup_in_flight.add(sid)
            try:
                pr = self._prs.resolve(cwd, ticket)
            finally:
                self._pr_lookup_in_flight.discard(sid)
            if pr is not None:
                self.call_from_thread(self._on_pr_resolved, sid, pr, ticket, False)

    def _refresh_issues(self) -> None:
        """Queue Jira lookups for the tickets currently on screen.

        A no-op when no credentials are configured, which is the default —
        ephor stays a local tool until told otherwise. Deduped by ticket
        rather than by session, so five sessions on one ticket cost one
        request.
        """
        tickets: set[str] = set()
        for sid in self._sid_by_row:
            if is_group_row(sid):
                continue
            agent = self._agents_by_sid.get(sid)
            if agent is None:
                continue
            key = ticket_for_agent(agent, self._summaries.get(sid))
            if key and key not in self._issue_lookup_in_flight and self._issues.needs_refresh(key):
                tickets.add(key)
        if tickets:
            self._sweep_issues(sorted(tickets))

    @work(thread=True, exit_on_error=False, group="jira-sweep", exclusive=True)
    def _sweep_issues(self, tickets: list[str]) -> None:
        """Background-thread worker: read a batch of tickets from Jira."""
        for key in tickets:
            if key in self._issue_lookup_in_flight:
                continue
            self._issue_lookup_in_flight.add(key)
            try:
                issue = self._issues.resolve(key)
            finally:
                self._issue_lookup_in_flight.discard(key)
            if issue is not None:
                self.call_from_thread(self._on_issue_resolved, key, issue)

    def _on_issue_resolved(self, key: str, issue: object) -> None:
        """Fold a resolved ticket into its work record."""
        status = getattr(issue, "status", "")
        work_items.record(
            key,
            jira_status=status,
            jira_title=getattr(issue, "title", ""),
            jira_assignee=getattr(issue, "assignee", ""),
        )


# ---------------------------------------------------------------------------


def _human_age(iso_ts: str) -> str:
    if not iso_ts:
        return "-"
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    delta = max(0, int((datetime.now(UTC) - ts).total_seconds()))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _jump_error(result: JumpResult, detail: str) -> str:
    pretty = {
        JumpResult.NO_TMUX_INFO: "session not in tmux — can't jump",
        JumpResult.SESSION_NOT_FOUND: "tmux session gone (closed?)",
        JumpResult.TMUX_MISSING: "tmux not installed",
        JumpResult.FAILED: "jump failed",
    }
    base = pretty.get(result, "jump failed")
    return f"{base}: {detail}" if detail else base


def _is_stale_tmux_ref(outcome: object) -> bool:
    """Whether re-discovery is worth retrying for this jump failure.

    NO_TMUX_INFO means the state file never had tmux fields. FAILED with a
    'can't find …' tmux error means the recorded pane/window/session is gone
    but agent_pid may still be alive in a new pane (think `claude --resume`
    after the original window was closed). Both are recoverable via
    enrich_state_files; other failures aren't.
    """
    result = getattr(outcome, "result", None)
    if result is JumpResult.NO_TMUX_INFO:
        return True
    if result is JumpResult.FAILED:
        detail = (getattr(outcome, "detail", "") or "").lower()
        return "can't find" in detail
    return False


def _render_summary_line(
    summary: StatusSummary,
    agents: list[AgentState] | None = None,
    sampler: ActivitySampler | None = None,
    tokens: TokenTracker | None = None,
    weekly_cap: int | None = None,
    usage: UsageSnapshot | None = None,
    account_usage: AccountUsage | None = None,
) -> str:
    """Bottom strip: active-count + aggregate sparkline + token total + cap.

    When per-account anchor data is available (``account_usage`` non-None)
    the strip displays authoritative server-anchored percentages with
    ``[account-label] 5h: 47% · 47m  7d: 8% · 5d``. Otherwise it falls
    back to the cross-account ccusage strip — useful while we're waiting
    on the first anchor or when the user is on an API-key auth.
    """
    active = summary.working + summary.attention
    aggregate: list[float] = []
    if agents and sampler:
        # Aggregate spark = sum-of-fractions across live sessions, clamped to 1.
        # Stripe-aligns sample positions across sessions by index.
        per_session: list[list[float]] = [
            sampler.samples_for(a.agent_pid) for a in agents if a.agent_pid
        ]
        if per_session:
            width = max(len(s) for s in per_session)
            for col in range(width):
                total = 0.0
                for s in per_session:
                    if len(s) > col:
                        total += s[col]
                aggregate.append(min(total, 1.0))
    spark = render_sparkline(aggregate)
    total_tokens = tokens.total_across(agents) if agents and tokens else 0
    tok_text = format_tokens(total_tokens) if total_tokens else "—"
    # When the user has configured a weekly cap, render `<used> / <cap> (W%)`
    # with a color cue based on consumption — green/yellow/orange/red mirrors
    # the four-bucket pattern from ccboard. Red kicks in past 95% so the user
    # has explicit warning before the cap actually bites.
    if weekly_cap and weekly_cap > 0:
        pct = min(999, round(100 * total_tokens / weekly_cap))
        if pct >= 95:
            cap_color = "#f85149"
        elif pct >= 80:
            cap_color = "#ff8c00"
        elif pct >= 60:
            cap_color = "#EBCB8B"
        else:
            cap_color = "#A3BE8C"
        tok_segment = (
            f"[dim]tokens: {tok_text} / {format_tokens(weekly_cap)} ([{cap_color}]{pct}%[/])[/]"
        )
    else:
        tok_segment = f"[dim]tokens: {tok_text}[/]"
    line = f"[bold #00ffff]●[/] [bold]{active}[/] active   [#00ffff]{spark}[/]   {tok_segment}"
    if account_usage is not None:
        line = f"{line}   {account_usage_segment(account_usage)}"
    else:
        usage_segment = format_usage_segment(usage, weekly_cap=weekly_cap)
        if usage_segment:
            line = f"{line}   {usage_segment}"
    return line


def _agent_matches_filter(agent: AgentState, needle: str) -> bool:
    """Case-insensitive substring match across the user-visible fields."""
    n = needle.lower()
    haystack = " ".join(
        s.lower()
        for s in (
            agent.project_name,
            agent.cwd,
            agent.last_summary,
            agent.session_id,
            agent.last_event,
        )
        if s
    )
    return n in haystack


def run() -> int:
    """Entry point invoked by `ephor tui`. Returns the exit code."""
    return EphorApp().run() or 0

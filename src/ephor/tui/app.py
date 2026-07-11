"""Textual TUI dashboard for ephor.

Live list of every Claude Code session, refreshed from the state dir every
500ms. j/k or arrows navigate; Enter jumps the user's tmux client to the
selected session's pane; q quits.

Layout (matches docs/stitch-handoff.md):

  Header (Textual)
  HeaderBar (PERM/WAIT/ERR/WORK/IDLE/DEAD counters)
  Sessions (ListView of SessionRow cards, 2 lines each)
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
from textual.widgets import Footer, Header, Input, ListItem, ListView, Static

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
from ephor.constants import AgentStatus
from ephor.speech import SpeechWatcher
from ephor.speech_player import SpeechPlayer
from ephor.speech_settings import load as load_speech_settings
from ephor.speech_settings import save as save_speech_settings
from ephor.state.manager import StateManager
from ephor.state.models import AgentState, StatusSummary
from ephor.state.reconciler import reconcile
from ephor.summarizer import summarize_transcript, unavailable_reason
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
from ephor.tui.widgets.session_row import render_sparkline
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


class StatusToast(Static):
    """One-line ephemeral message at the bottom (jump result, errors, etc.)."""


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
        Binding("t", "jump_speaking", "jump to TTS speaking session"),
        Binding("m", "toggle_mute", "mute / unmute TTS playback"),
        Binding("M", "toggle_speak_mode", "TTS: full reply ↔ summary"),
        Binding("n", "next_attention", "jump cursor to next PERM/WAIT/ERR row"),
        Binding("slash", "filter", "filter sessions by substring"),
        Binding("escape", "clear_filter", "clear filter", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
    ]

    def __init__(self, manager: StateManager | None = None) -> None:
        super().__init__()
        self._manager = manager or StateManager()
        self._sid_by_row: list[str] = []  # row index → session_id
        self._rows_by_sid: dict[str, SessionRow] = {}  # for in-place updates
        self._items_by_sid: dict[str, ListItem] = {}  # for in-place reorder
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
        self._kill_armed_sid: str | None = None
        self._kill_armed_at: float = 0.0
        self._filter: str = ""  # case-insensitive substring filter; "" = show all
        self._filter_input: Input | None = None
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

            list_view = self.query_one(ListView)
            new_sids = [a.session_id for a in agents]

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
            new_items: dict[str, ListItem] = {}
            items: list[ListItem] = []
            cursor_target = 0
            speaking_sid = self._speaking_sid()
            for i, agent in enumerate(agents):
                row = SessionRow()
                row.update_agent(
                    agent,
                    samples=self._activity.samples_for(agent.agent_pid),
                    summary=self._summaries.get(agent.session_id),
                    tokens=self._tokens.total_for(agent),
                    speaking=(agent.session_id == speaking_sid),
                )
                item = ListItem(row)
                items.append(item)
                new_rows[agent.session_id] = row
                new_items[agent.session_id] = item
                if prev_sid and agent.session_id == prev_sid:
                    cursor_target = i

            await list_view.clear()
            if items:
                await list_view.mount(*items)

            # DOM is consistent — swap state mappings + index atomically.
            self._sid_by_row = list(new_sids)
            self._rows_by_sid = new_rows
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

    def _update_rows(self, agents: list[AgentState]) -> None:
        """Refresh in-place row content for every cached SessionRow."""
        speaking_sid = self._speaking_sid()
        for agent in agents:
            row = self._rows_by_sid.get(agent.session_id)
            if row is not None:
                row.update_agent(
                    agent,
                    samples=self._activity.samples_for(agent.agent_pid),
                    summary=self._summaries.get(agent.session_id),
                    speaking=(agent.session_id == speaking_sid),
                )

    def _speaking_sid(self) -> str | None:
        """Session id whose response is currently being read aloud, if any.

        Reads through the SpeechBar so we don't double-decode the speech
        log: the bar already maintains a fresh _state on its 200ms tick.
        """
        if self._speech_bar is None:
            return None
        return self._speech_bar.speaking_session_id

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
            # Skip until there's material to summarize — a Claude transcript
            # or a captured reply — so content-less sessions don't trigger an
            # LLM call on every refresh (empty results aren't cached).
            if not agent.last_reply and not transcript_path(agent.cwd, sid).exists():
                continue
            self._summarize(sid, agent.cwd, manual=False, last_reply=agent.last_reply)

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
        await self._refresh_table()
        self._set_toast("refreshed")

    def action_filter(self) -> None:
        """Reveal the filter input and focus it. '/' enters this state."""
        if self._filter_input is None:
            return
        self._filter_input.display = True
        self._filter_input.value = self._filter
        self._filter_input.focus()

    async def action_clear_filter(self) -> None:
        """Hide the filter input and reset the filter. Bound to escape."""
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
        if self._filter_input is None or event.input is not self._filter_input:
            return
        self._filter_input.display = False
        self.query_one(ListView).focus()

    def action_cursor_down(self) -> None:
        self.query_one(ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(ListView).action_cursor_up()

    def action_next_attention(self) -> None:
        """Move cursor to the next row whose status needs attention.

        Cycles through ATTENTION_STATUSES (PERM / WAIT / ERR), wrapping.
        Looks first at rows strictly after the cursor; if none, wraps to top.
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
        status_by_sid: dict[str, AgentStatus] = {
            a.session_id: a.status for a in self._manager.scan()
        }
        attention_indices = [
            i
            for i, sid in enumerate(self._sid_by_row)
            if status_by_sid.get(sid) in ATTENTION_STATUSES
        ]
        if not attention_indices:
            self._set_toast("no rows need attention")
            return

        cursor_idx = list_view.index if list_view.index is not None else -1
        target = next(
            (i for i in attention_indices if i > cursor_idx),
            attention_indices[0],
        )
        list_view.index = target
        target_sid = self._sid_by_row[target]
        target_status = status_by_sid.get(target_sid, AgentStatus.IDLE)
        self._set_toast(f"→ {target_sid[:8]} ({target_status.value})")

    async def action_kill(self) -> None:
        """Two-press kill: first press arms, second within 3s fires.

        Killing tears down the claude process, the tmux window, and the
        on-disk state file. No undo — the confirmation window is the only
        guard against accidental presses.
        """
        list_view = self.query_one(ListView)
        idx = list_view.index
        try:
            sid = self._sid_by_row[idx] if idx is not None else None
        except (IndexError, AttributeError):
            sid = None
        if sid is None:
            self._set_toast("no row selected")
            return

        agent = next(
            (a for a in self._manager.scan() if a.session_id == sid),
            None,
        )
        if agent is None:
            self._set_toast(f"session {sid[:8]} disappeared between refreshes")
            self._kill_armed_sid = None
            return

        label = agent.project_name or sid[:8]
        now = time.monotonic()
        armed = self._kill_armed_sid == sid and now - self._kill_armed_at < KILL_CONFIRM_WINDOW_SEC

        if not armed:
            self._kill_armed_sid = sid
            self._kill_armed_at = now
            self._set_toast(f"press x again within 3s to kill {label}")
            return

        self._kill_armed_sid = None
        outcome = kill_session(agent, self._manager.directory)
        # Drop any cached summary so a future session reusing the sid doesn't
        # display stale text. Best-effort.
        self._summaries.delete(sid)
        if outcome.ok:
            note = f" ({outcome.detail})" if outcome.detail else ""
            self._set_toast(f"killed {label}{note}")
        else:
            self._set_toast(f"kill failed: {outcome.detail}")
        await self._refresh_table()

    def action_summarize(self) -> None:
        """Force-refresh the summary for the highlighted session.

        Useful when the conversation has progressed beyond what was cached
        on first sight. Rate-limited via the in-flight set: pressing `s`
        repeatedly is a no-op while a previous call is still pending.
        """
        list_view = self.query_one(ListView)
        idx = list_view.index
        try:
            sid = self._sid_by_row[idx] if idx is not None else None
        except (IndexError, AttributeError):
            sid = None
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
        if sid in self._summarizing:
            self._set_toast("already summarizing…")
            return
        self._summarize(sid, agent.cwd, manual=True, last_reply=agent.last_reply)

    async def action_jump(self) -> None:
        list_view = self.query_one(ListView)

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

        idx = list_view.index
        try:
            sid = self._sid_by_row[idx] if idx is not None else None
        except (IndexError, AttributeError):
            sid = None

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
    def _summarize(self, sid: str, cwd: str, manual: bool, last_reply: str = "") -> None:
        """Background-thread worker: summarize the session, store the result.

        Claude sessions summarize their transcript (rich, recent-turn context).
        Other agents have no Claude-format transcript, so we fall back to
        condensing the assistant reply captured at turn-end (``last_reply``,
        written to the state file by the hook handler). Lazy-once policy: the
        refresh path only schedules when there's no cached summary; manual=True
        force-refreshes via `s`. De-duped via ``self._summarizing``.
        """
        if sid in self._summarizing:
            return
        self._summarizing.add(sid)
        try:
            if manual:
                # Show progress toast on the UI thread.
                self.call_from_thread(self._set_toast, f"summarizing {sid[:8]}…")
            # summarize_transcript returns "" for a missing/foreign transcript
            # (non-Claude), so try it first (rich for Claude) then fall back to
            # condensing the captured reply.
            path = transcript_path(cwd, sid)
            had_material = path.exists() or bool(last_reply)
            text = summarize_transcript(path, cwd=cwd)
            if not text and last_reply:
                from ephor.summarizer import summarize_text

                text = summarize_text(last_reply, cwd=cwd)
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

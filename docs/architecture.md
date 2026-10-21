# Architecture

`ephor` is a Linux-native dashboard for terminal coding-agent sessions —
Claude Code, Gemini CLI, Antigravity CLI, Codex CLI, Grok CLI, and
OpenCode. It avoids
terminal-automation hacks (AppleScript, iTerm window control) entirely
and instead leans on three primitives that Linux users already have:

1. **Agent lifecycle hooks** — for real-time session state.
2. **tmux** — for window management.
3. **Per-session JSON state files on disk** — for the source of truth.

## Multi-agent design

Five coding agents expose the same fundamental hook contract Claude Code
pioneered: a shell command invoked with event JSON on **stdin**, carrying
a session id, a working directory, and an event name. `ephor` exploits
that: `providers.py` describes each agent (its binary, settings-file path,
event vocabulary, resume flags), and **one** shell handler
(`hooks/event_handler.sh`) normalizes every dialect into a single on-disk
state schema. The installer registers that handler in each agent's own
settings file, tagging the command with `EPHOR_PROVIDER=<name>` so the
handler knows which agent fired it (Antigravity execs the command without
a shell, so its provider and event arrive as positional args instead).
OpenCode is the sixth agent — it has no shell hooks, so a generated JS
plugin translates its bus events and pipes them into the same handler. Everything downstream (TUI, tmux,
reconciler, speech) reads the state files and never cares who wrote them.

Agents differ only at the edges:

| Agent | Config the installer writes | Event-name dialect |
|---|---|---|
| Claude Code | `~/.claude/settings.json` (`hooks`) | `PreToolUse`, `PostToolUse`, `Stop`, … |
| Gemini CLI | `~/.gemini/settings.json` (`hooks`) | `BeforeTool`, `AfterTool`, `AfterAgent`, `Notification(ToolPermission)` |
| Antigravity CLI (`agy`) | `~/.gemini/config/hooks.json` (named group) | `PreInvocation`, `PreToolUse`, `PostToolUse`, `Stop` |
| Codex CLI | `~/.codex/hooks.json` | `PreToolUse`, `PermissionRequest`, `Stop`, … |
| Grok CLI | `~/.grok/hooks/ephor.json` | Claude-like; `UserPromptSubmit` carries `user_prompt`; subagent sessions suppressed |
| OpenCode | `~/.config/opencode/plugins/ephor.js` | JS plugin → canonical events |

### Grok subagent sessions

Grok spawns subagents as *first-class sessions*: each gets its own UUID, its
own `~/.grok/sessions/<encoded-cwd>/<id>/` directory with a full chat history,
and fires the whole hook set under that id. Keyed naively on the payload's
session id, one grok session doing a three-way fan-out renders as four
dashboard rows.

Grok labels them itself — each session's `prompt_context.json` carries
`"audience": "subagent"`, versus `"primary"` for a session the user started —
so the handler reads grok's own declaration rather than inferring from shape.
On every grok event it globs `~/.grok/sessions/*/<session_id>/prompt_context.json`
(the id is already anchored to `[a-zA-Z0-9_-]`, and globbing avoids
reimplementing grok's percent-encoding of the cwd) and exits without writing
state when the audience is `subagent`.

The check runs on *every* event, not just `SessionStart`: grok may not have
written `prompt_context.json` when the first hook fires, so a row can already
exist by the time the label appears — a later event notices and removes it.
Absent the file entirely, the session is kept; a missing label must never
hide a real session.


OpenCode has no shell hooks, only JS/TS plugins, so it uses the
`OPENCODE_PLUGIN` strategy: `ephor init --provider opencode` writes a generated
plugin whose `event` hook (plus `tool.execute.before/after`) maps OpenCode's
bus events — `session.status`/`session.idle`, `permission.*`, tool execution —
onto ephor's canonical events and pipes them, via Bun's `$` shell, straight
into `event_handler.sh` (tagged `EPHOR_PROVIDER=opencode`). So even the
plugin-based agent funnels through the one shared state writer. (Summary-mode
TTS is Claude-transcript-specific and not yet wired for OpenCode sessions.)

## Components

```
~/.claude/settings.json · ~/.gemini/settings.json · ~/.codex/hooks.json · ~/.grok/hooks/ephor.json
  hooks → src/ephor/hooks/event_handler.sh   (command tagged EPHOR_PROVIDER=<name>)
            │ on every PreToolUse / BeforeTool / Notification / Stop / AfterAgent / …
            ▼
            $XDG_STATE_HOME/ephor/sessions/<sid>.json   (mode 0600, carries `provider`)
                                       ▲
                                       │ atomic read
                                       │
src/ephor/
├── providers.py               ← registry: per-agent binary, settings path, events, resume flags
├── hooks/event_handler.sh     ← shell, set -u, sanitized PATH, jq --arg only; all dialects
├── hooks/opencode_plugin.js   ← generated OpenCode plugin (event bus → event_handler.sh)
├── hooks/installer.py         ← installs/uninstalls hooks per provider (+ the opencode plugin)
├── state/manager.py           ← scans + reads state files
├── state/reconciler.py        ← prunes dead sessions, fixes stuck waits
├── tmux/discover.py           ← walks /proc + tmux to find pane → agent pid (all agent binaries)
├── tmux/navigator.py          ← `tmux select-window` to jump to a session
├── tui/app.py                 ← Textual TUI dashboard
├── tui/rows.py                ← row ids: session / group header / sessionless ticket row
├── tui/lanes.py               ← the `status` grouping: board lanes + ticket rows
├── tui/flows_screen.py        ← the `f` screen: flow runs, `y` approve / `n` deny
├── usage.py                   ← ccusage transcript-token aggregation (Claude)
├── account_usage.py           ← server-anchored per-account 5h/7d limits (Claude)
├── summarizer.py              ← one-sentence reply summary via the agent's summarize command
├── speech.py                  ← append-only NDJSON log of TTS start/stop events
├── speech_player.py           ← FIFO speech queue + kokoro subprocess manager
├── speech_settings.py         ← persisted enabled/mode + learned chars/sec rate
├── tui/widgets/speech_bar.py  ← bottom bar mirroring the speech engine
├── work_items.py              ← per-ticket record: branch, worktree, PR, Jira, sessions
├── eventlog.py                ← append-only NDJSON tape of transitions (`ephor log`)
├── audit.py                   ← session cost/speed joined to outcome (`ephor audit`)
├── launcher.py                ← `ephor start` / `ephor resume`: worktree + tmux + $EPHOR_TICKET
├── permissions.py             ← writes the decisions the hook handler emits
├── jira_api.py                ← opt-in Jira read: status, summary, assignee, drift, board search
├── board.py                   ← board.toml: lanes, repos, flow rules (validated, errors collected)
├── flows/snapshot.py          ← board search → diff → flows/snapshot.json (the rule-less tick)
├── flows/tick.py              ← the flows tick: rules, adopt/stale, guards, dispatch
├── flows/triggers.py          ← on_pr (gh per ticket) / on_session (state files) observations
├── flows/runs.py              ← flows/runs/<run_id>.json: O_EXCL claims + compare-and-set states
├── flows/runners.py           ← start= / command= in detached tmux windows, `.exit` wrapper
├── flows/cli.py               ← `ephor flows ...` (board, tick, watch, ls, approve, deny, pause, resume, check)
├── filelock.py                ← sidecar flock for stores written from more than one process
└── notify.py                  ← opt-in push (ntfy/webhook); policy engine that decides
                               what is worth interrupting you for
```

## Work items: the second axis

Everything above is keyed on a *session*. That is the right key for
"what is running right now" and the wrong one for "what happened to
DR-8222", which spans several sessions, some of them long dead.

`work_items.py` is the second axis: one JSON file per ticket under
`$XDG_STATE_HOME/ephor/work/`, merged field-by-field by whichever component
learns something — the launcher at `ephor start`, the PR resolver when `gh`
answers, the Jira probe when it reads a status, the ticket harvester when a
fact-tier probe fires. Every field is last-writer-wins, and `confirmed` is a
ratchet: a guess can never demote a key a branch name already settled.

Two properties fall out of the key being a ticket rather than a session:
grouping (several sessions attempting one ticket fold under one header), and
promotion (a key established once is read back rather than re-derived, so a
deleted branch cannot turn a settled fact back into an amber guess).

### Who writes what

| File | Writer | Lock |
|---|---|---|
| `sessions/<sid>.json` | the hook handler, only | per-session flock |
| `work/<KEY>.json` | ephor (TUI, CLI, launcher, flows tick) | `work/.lock` flock around every load-merge-save |
| `pending/<sid>.json` | ephor; **read + deleted** by the hook | none — create/consume |
| `events.ndjson` | the TUI, the launcher, the flows tick | `events.ndjson.lock` flock around rotate + `O_APPEND` write |
| `flows/snapshot.json` | the flows tick (TUI worker, `ephor flows watch` / `tick`, a systemd timer, `ephor flows board --refresh`) | `flows/tick.lock` (non-blocking — a tick that cannot take it returns and reads the holder's snapshot); atomic rename, 0600, written last |
| `flows/runs/<run_id>.json` | the flows tick; approve/deny (CLI, TUI) | `O_EXCL` create claims a run; every later read-check-write (tick transitions, approve/deny) holds the `flows/runs/.lock` sidecar flock (short, blocking with timeout) and re-reads the state under it; approve/deny refuse unless the state is still `awaiting_confirm` |
| `flows/runs/<run_id>.exit` | the `command =` wrapper in the flow's tmux window | none — written to a temp name and renamed; cleared by the tick before each attempt |
| `flows/payloads/<run_id>.json` | the flows tick | none — written once before launch, 0600 |
| `flows/paused` | `ephor flows pause` / `resume` | none — presence is the flag |

Until the flows tick, ephor itself was effectively a single writer of work
records and event-log lines, so neither needed a lock. The tick runs from a
TUI worker thread, from `ephor flows watch`, or from a systemd timer — all at
once, potentially — so both stores now take a sidecar flock
(`ephor/filelock.py`) around their read-modify-write. A sidecar, because the
data files are replaced by rename and a lock on a renamed-away inode guards
nothing. The locks are fail-open: a writer that cannot get one within a
quarter of a second proceeds unlocked, exactly as before, rather than wedging
the dashboard — the TUI makes these writes on its event-loop thread.

Run files need more than atomic rename: approve can read `awaiting_confirm`
while a tick expires or supersedes the run, and approve's rename would then
bring it back as `claimed`. So every run update re-reads and re-checks the
state under `flows/runs/.lock`. That is a separate lock from `flows/tick.lock`
because the tick holds `tick.lock` across Jira and `gh` calls, and an approve
from the dashboard must not wait seconds behind the network.

The invariant `docs/architecture.md` has always promised holds unchanged:
**the hook is still the only writer of session state files.** `ephor start`
does not write one. It hands the ticket to the agent through the
environment, and the hook records it on the first event exactly as it
records everything else.

## Change observation

The dashboard repaints twice a second and almost nothing it draws is new.
`EphorApp._observe()` is the single place that diffs the current scan
against the previous one, and everything edge-triggered hangs off it: the
event log records transitions rather than frames, the notifier fires once
per (session, reason) rather than continuously, and a confirmed ticket is
promoted the first time it is seen.

## Attention, in two layers

`AgentStatus` says what the *agent* is doing. It cannot say whether the work
is finished, because a session that pushed a branch and exited is `IDLE`
whether its CI is green or red.

So attention is derived from two sources. `ATTENTION_STATUSES` covers the
agent (`WAITING_PERMISSION`, `WAITING_ANSWER`, `ERROR`); `WorkAttention`
covers the work (`CI_FAILED`, `CONFLICT`, `CHANGES_REQUESTED`,
`REVIEW_REQUESTED`, `TICKET_DRIFT`), computed from the PR fields `gh`
returns and, when Jira is configured, from the disagreement between the
ticket and its PR. `n` walks both.

## Speech subsystem (TTS speak-back)

When ephor owns playback (`ephor speech install`), a single FIFO queue reads
back session replies through the local kokoro pipeline. The data flow
mirrors the rest of the app — hooks write, the TUI reads:

```
Stop hook ──► speech.py NDJSON log  ──poll(200ms)──► SpeechPlayer.tick()
                                                          │
                              ┌───────────────────────────┤
                       speak_mode == "full"        speak_mode == "summary"
                              │                           │
                       enqueue raw reply          background thread:
                              │                    summarizer.summarize_transcript()
                              │                    → `claude -p` (subscription auth)
                              │                    → ≤70-char sentence ──► _pending
                              ▼                           ▼
                       FIFO queue (cap 5, same-session dedup, preempt-on-collision)
                              ▼
                       kokoro play-ducked.sh subprocess (Popen, own session group)
                              ▼
                       SpeechBar shows now-playing + waiting queue
```

Design notes:

- **No daemon, no threads on the hot path.** `tick()` is the single
  entry point, driven by Textual's 200ms interval; subprocess lifecycle
  is `Popen` + `poll()` so the event loop owns scheduling. The only
  thread is the short-lived summary worker, so a 2-3s `claude -p` call
  never freezes the TUI — its result rejoins the FIFO via a thread-safe
  `_pending` queue on the next tick.
- **Subscription auth, no API key.** Summary mode shells out to the same
  `claude -p` the dashboard summary column uses. `EPHOR_INTERNAL=1` is set
  in that subprocess so ephor's own hook short-circuits and the summarizer
  call doesn't spawn a ghost session.
- **Queue semantics** (full detail in `speech_player.py`'s docstring):
  different sessions → FIFO; same session queued → replace; same session
  playing → preempt; queue cap drops oldest. Kills target the whole
  process group (`start_new_session=True`) so kokoro + paplay die
  together on preempt — no audible overlap.
- **Self-calibrating rate.** Natural completions feed observed chars/sec
  into an EWMA persisted in `speech_settings.py`, so the progress bar and
  karaoke advance at the user's real kokoro speed instead of a guess.
- **Settings resolution** (`speech_settings.py`): env (`EPHOR_TTS_ENABLED`,
  `EPHOR_TTS_MODE`) > persisted `speech.json` (mode 0600) > default
  (on iff kokoro is detected). Same precedence for enabled and mode.

## Why hook-driven, not scraped

Earlier dashboards in this niche scrape Claude's terminal output (or worse,
control a specific terminal emulator). That breaks the moment you change
terminal, switch to Wayland, or run Claude headless. `ephor` instead reads
the official hook events Claude Code already emits, so there is nothing
to "screen-scrape" and the dashboard works the same in any terminal.

## State file invariants

- **One JSON file per Claude session**, keyed by Claude's session ID.
- **0600 mode, parent dir 0700.** Never world-readable.
- **Atomic writes** — temp file + `rename(2)`. Readers never see a
  half-written file.
- **The hook is the only writer** during a session's lifetime; the
  reconciler is the only writer that can mark a file `DEAD`.

## Tmux integration

`ephor` records the tmux pane each Claude session lives in (set by the
hook on every event, so it self-heals on `claude --resume`). Pressing
Enter on a row runs `tmux select-window -t <pane_id>` against the
user's current tmux client — no terminal-emulator-specific hacks
needed.

## Privacy

Everything stays on your machine. The only network call `ephor` makes is
to `https://api.anthropic.com/api/oauth/usage` for per-account usage
anchoring; see [SECURITY.md](../SECURITY.md) for the full surface area.

## Status lanes

`g` cycles the session list through none → ticket → repo → **status**. The
status grouping draws one lane per `board.toml` column, in column order,
then `Other` (statuses no column claims) and `Unticketed` (sessions with no
ticket). A session goes in the lane of its ticket's status — the board
snapshot when the last search saw the ticket, else the dashboard's own
per-ticket Jira lookup, else the work record. Lanes holding finished work
start folded.

The lanes are fed by `flows/snapshot.json`, which the TUI refreshes on a
worker thread every 60 seconds when a `board.toml` exists and Jira is
configured (`ephor.flows.snapshot.refresh`: one JQL search, a pure diff
against the previous snapshot, `TICKET_TRANSITIONED` events, then the
atomic write). That refresh is a flows tick with no rules; the tick reuses
`fetch` / `advance` / `save` and runs its rules between them.

Board tickets that have no live session are drawn as **ticket rows**, with
their own row-id prefix (`"\x00tkt:"`, beside the group headers'
`"\x00grp:"`; see `tui/rows.py`). Every session action asks
`is_session_row()` of the cursor row, and `_target_sids()` returns nothing
when the cursor is on a header or a ticket row — it never falls back to
the marked set from there, so a keystroke aimed at a ticket can never land
on sessions marked elsewhere on the board.

## Flows tick

`ephor flows tick` (also the TUI's 60-second board worker, which replaces
the rule-less refresh above, and the loop behind `ephor flows watch`) is
that refresh with `board.toml`'s rules run in the middle: search → diff →
observe PRs and sessions → create runs → tend old runs → dispatch → write
the snapshot last. A first snapshot, or a rule whose fingerprint is not in
`rules_seen`, adopts the board and fires nothing; after a gap longer than
`max_event_age` every new run waits for confirmation (`stale`). The user
reference, with examples, is [flows.md](flows.md).

A run is a file named `<KEY>.<flow>.<generation>`, claimed by an exclusive
create, so the same event can never launch twice. The generation names the
event: `<status_id>-<seq>` for `on_enter` / `on_exit` (one visit to a
status), `pr<number>` for `on_pr`, and the session id's first twelve
characters for `on_session`. A run moves `awaiting_confirm → claimed →
launched → done | failed`, or ends `denied`, `expired`, `superseded` or
`stuck`; `blocked` is a guard's "not now" and is retried while the event is
still current. Guards run in a fixed order — pause flag, hourly cap,
per-ticket breaker, then for agent flows the live-agent cap and the launch
guard — followed by an uncached `GET issue` re-check. Flows launch in
detached tmux windows named `flow:<KEY>:<flow>`; `command =` flows run under
a wrapper that writes `runs/<run_id>.exit`, which a later tick folds into
`done` / `failed`.

`on_pr` and `on_session` cannot be read off the board search, so
`flows/triggers.py` keeps an observation of each in the snapshot and diffs
it like the statuses: `prs` (ticket → every PR `gh pr list --search <KEY>`
attributes to it, refreshed at most every `pr_interval`, stamped in
`pr_checked_at`) and `sessions_seen` (session id → ticket, status, pid, and
whether it has been idle past `settle`). A ticket's first PR lookup and a
session's first sighting are baselines and fire nothing. `died` is a session
last seen alive that is now DEAD — or whose state file the reconciler has
already removed, when the pid it recorded is no longer running; a DEAD
row whose process is alive (resume residue) is not a death. Each observation
exists only while a rule reads it: with no `on_pr` (or `on_session`) rule in
effect it is cleared, so a rule added later starts from a fresh baseline.
`opened` and `idle` runs stay current while the PR stays open and the
session stays idle; `merged` and `died` are final.

The tick holds `tick.lock` for the duration of those `gh` calls (one per
eligible ticket, every `pr_interval`), which is why `approve` / `deny` take
the separate `flows/runs/.lock` instead.

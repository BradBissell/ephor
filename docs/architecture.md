# Architecture

`ephor` is a Linux-native dashboard for terminal coding-agent sessions —
Claude Code, Gemini CLI, Codex CLI, and Grok CLI. It avoids
terminal-automation hacks (AppleScript, iTerm window control) entirely
and instead leans on three primitives that Linux users already have:

1. **Agent lifecycle hooks** — for real-time session state.
2. **tmux** — for window management.
3. **Per-session JSON state files on disk** — for the source of truth.

## Multi-agent design

Four coding agents expose the same fundamental hook contract Claude Code
pioneered: a shell command invoked with event JSON on **stdin**, carrying
a session id, a working directory, and an event name. `ephor` exploits
that: `providers.py` describes each agent (its binary, settings-file path,
event vocabulary, resume flags), and **one** shell handler
(`hooks/event_handler.sh`) normalizes every dialect into a single on-disk
state schema. The installer registers that handler in each agent's own
settings file, tagging the command with `EPHOR_PROVIDER=<name>` so the
handler knows which agent fired it. Everything downstream (TUI, tmux,
reconciler, speech) reads the state files and never cares who wrote them.

Agents differ only at the edges:

| Agent | Settings file | Event-name dialect |
|---|---|---|
| Claude Code | `~/.claude/settings.json` | `PreToolUse`, `PostToolUse`, `Stop`, … |
| Gemini CLI | `~/.gemini/settings.json` | `BeforeTool`, `AfterTool`, `AfterAgent`, `Notification(ToolPermission)` |
| Codex CLI | `~/.codex/hooks.json` | `PreToolUse`, `PermissionRequest`, `Stop`, … |
| Grok CLI | `~/.grok/user-settings.json` | Claude-like; `UserPromptSubmit` carries `user_prompt` |

OpenCode is out of scope for now: its plugins are JS/TS rather than shell
hooks, so it would need a plugin shim or an SSE-stream watcher against
`opencode serve`'s HTTP `/event` endpoint.

## Components

```
~/.claude/settings.json · ~/.gemini/settings.json · ~/.codex/hooks.json · ~/.grok/user-settings.json
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
├── hooks/installer.py         ← installs/uninstalls hooks per provider
├── state/manager.py           ← scans + reads state files
├── state/reconciler.py        ← prunes dead sessions, fixes stuck waits
├── tmux/discover.py           ← walks /proc + tmux to find pane → agent pid (all agent binaries)
├── tmux/navigator.py          ← `tmux select-window` to jump to a session
├── tui/app.py                 ← Textual TUI dashboard
├── usage.py                   ← ccusage transcript-token aggregation (Claude)
├── account_usage.py           ← server-anchored per-account 5h/7d limits (Claude)
├── summarizer.py              ← one-sentence reply summary via the agent's summarize command
├── speech.py                  ← append-only NDJSON log of TTS start/stop events
├── speech_player.py           ← FIFO speech queue + kokoro subprocess manager
├── speech_settings.py         ← persisted enabled/mode + learned chars/sec rate
└── tui/widgets/speech_bar.py  ← bottom bar mirroring the speech engine
```

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

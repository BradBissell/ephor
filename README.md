# ephor — coding-agent session orchestrator for Linux

[![CI](https://github.com/BradBissell/claude-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/BradBissell/claude-orchestrator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

A Linux-native TUI that watches every terminal coding-agent session you
have running — **Claude Code, Gemini CLI, Codex CLI, Grok CLI, and
OpenCode** — and tells you, at a glance, which ones need your attention,
which are still working, and which went idle.

> `ephor` (Greek *ἔφορος*, "overseer") began as `cco`, a Claude-Code-only
> dashboard. It now speaks the hook protocol of four coding agents that
> share Claude Code's stdin-JSON hook model.

## Supported agents

| Agent | Settings file | How ephor hooks in |
|---|---|---|
| **Claude Code** | `~/.claude/settings.json` | `hooks` object, command + stdin JSON |
| **Gemini CLI** | `~/.gemini/settings.json` | same `hooks` shape (`Before*`/`After*` events) |
| **Codex CLI** | `~/.codex/hooks.json` | dedicated hooks file, command + stdin JSON |
| **Grok CLI** ([superagent-ai/grok-cli](https://github.com/superagent-ai/grok-cli)) | `~/.grok/user-settings.json` | same `hooks` shape |
| **OpenCode** | `~/.config/opencode/plugins/ephor.js` | bundled JS plugin → shells out to the handler |

The first four share the stdin-JSON hook model, so one shell handler serves
them all — it understands each agent's event-name and field-name dialect and
records which agent a session belongs to. OpenCode has no shell hooks, so
`ephor init --provider opencode` installs a tiny JS **plugin** that translates
OpenCode's bus events and pipes them into the *same* handler — so every agent
funnels through one state writer.

<img width="2806" height="1972" alt="image" src="https://github.com/user-attachments/assets/50f4b678-2da8-4ee1-b52e-3d148ce4ae7c" />


Or use it side by side with the active tmux session:
<img width="3838" height="2136" alt="image" src="https://github.com/user-attachments/assets/0f59b339-95e0-4189-8068-57bc53d1cdfd" />


## Why

Running 10+ coding-agent sessions in parallel is normal now — often a
mix of Claude Code, Gemini, Codex, and Grok. tmux shows you all of them,
but tmux can't tell you that session 7 is blocked on a permission prompt
while the other nine are still working. `ephor` solves exactly that — and
presses Enter to jump to the right tmux window.

## Install

```bash
pipx install ephor-orchestrator
ephor init --provider all   # installs hooks into every agent you use
ephor                       # launches the TUI dashboard (alias: ephor tui)
```

(Or use `pip install --user ephor-orchestrator` / `uv tool install ephor-orchestrator`.)

## Quickstart

1. **`ephor init [--provider <agent>|all]`** — registers ephor's hook in
   the agent's settings file (`--provider` defaults to `claude`; pass
   `gemini`, `codex`, `grok`, or `all`). The original settings file is
   backed up; `ephor uninstall --provider <agent>` cleanly removes them.
2. **`ephor`** (or `ephor tui`) — opens the TUI. Use `j`/`k` or arrow keys to navigate,
   `/` to filter, `Enter` to jump to a session's tmux pane, `x` to
   kill, `?` for the full keymap.
3. **`ephor list`** — script-friendly one-line-per-session status, for
   tmux status-right widgets or shell scripts.
4. **`ephor doctor`** — checks dependencies and, per agent, whether its
   CLI is installed and ephor's hooks are registered.

See [`docs/getting-started.md`](docs/getting-started.md) for a longer
walkthrough.

## Highlights

- **Hook-driven, not scraped.** State comes from each agent's official
  hook events — no terminal-output parsing, no AppleScript, no
  Wayland window-poking. Works the same in Ghostty, Alacritty, kitty,
  GNOME Terminal, or under `mosh`.
- **One handler, four agents.** A single POSIX-shell handler normalizes
  every agent's event vocabulary (e.g. Gemini's `BeforeTool`/`AfterAgent`,
  Grok's `user_prompt`) into one on-disk state schema, tagged with the
  `provider` that produced it.
- **tmux-native navigation.** Every session is mapped to its tmux
  pane on every event, so resuming after a closed window self-heals.
  Pressing Enter does `tmux select-window -t <pane>` against your
  current client.
- **Per-session state on disk.** `$XDG_STATE_HOME/ephor/`,
  mode 0600, atomic writes. Surviving a reboot is a feature.
- **Per-account 5h / 7d usage strip.** (Claude Code) anchors against the
  official `/api/oauth/usage` endpoint, then extrapolates with local
  ccusage deltas — accurate without hammering the API.
- **POSIX-shell hook handler** with `set -u`, sanitized PATH, jq
  `--arg` everywhere, per-session flock, and fail-OPEN error handling
  (a buggy hook never blocks your agent).
- **Auto-approve via hook return value**, not keystroke injection.
  Rules engine answers permission prompts before the dialog renders.
- **Spoken one-line summaries.** When a session finishes, ephor can read
  back a ≤70-char summary of what Claude just did — so you can keep your
  eyes on one window and still know the other nine are done. See
  [Speak-back](#speak-back-tts) below.

## Speak-back (TTS)

Running ten sessions in parallel, the bottleneck isn't compute — it's
*you* noticing which one finished. ephor can speak that for you.

When a session's turn ends, ephor enqueues its reply to a single FIFO
speech queue shared across **all** your sessions, and plays it through
your local [kokoro](https://github.com/hexgrad/kokoro) TTS pipeline. This
works for **Claude Code, Gemini, Codex, and OpenCode** — the reply text is
captured at turn-end from each agent (an event-payload field, or, for
OpenCode, the plugin's SDK). Grok is status-only for now (its reply isn't
exposed at turn-end without parsing its SQLite store). Two modes:

- **`summary` (recommended for parallel work).** ephor shells out to
  `claude -p` to turn the reply into a **single ≤70-character
  sentence** — the same summary the dashboard column shows — and speaks
  only that. You hear *"DR-1423: added retry to the upload client"*
  instead of a three-minute monologue. It's a verbal notification, not
  a read-aloud. If a Jira ticket is resolvable from the branch/path,
  the summary is spoken with its key prefixed so you know *which*
  session is talking.
- **`full`.** Reads the entire assistant message (the legacy behaviour).

Because the summarizer uses the **same Claude Code login as the
dashboard** (`claude -p`, subscription auth), there's **no API key to
configure** and nothing leaves the normal Claude Code auth path.

#### Summarize with any model (local or hosted)

By default summaries are generated by `claude -p`. To use a different model —
e.g. a **local llama-swap / vLLM / Ollama** server, keeping transcripts on your
own hardware — point the summarizer at any **OpenAI-compatible
`/chat/completions`** endpoint via env vars:

```bash
export EPHOR_SUMMARY_API_BASE=http://localhost:8000/v1   # switches to the HTTP backend
export EPHOR_SUMMARY_MODEL=qwen3.6-35b-a3b               # model name (drives llama-swap's swap)
# export EPHOR_SUMMARY_API_KEY=…                         # optional bearer token
# export EPHOR_SUMMARY_TIMEOUT=120                       # optional; raise for cold model loads
```

| Var | Purpose |
|---|---|
| `EPHOR_SUMMARY_BACKEND` | `claude` (default) or `openai` — explicit override |
| `EPHOR_SUMMARY_API_BASE` | OpenAI-compatible base URL; setting it alone selects the HTTP backend |
| `EPHOR_SUMMARY_MODEL` | model id sent in the request (required for the HTTP backend) |
| `EPHOR_SUMMARY_API_KEY` | optional `Authorization: Bearer` token |
| `EPHOR_SUMMARY_TIMEOUT` | request timeout, seconds (default 30) |
| `EPHOR_SUMMARY_MAX_TOKENS` | completion budget (default 128) |
| `EPHOR_SUMMARY_EXTRA_BODY` | JSON merged into the request body (server-specific options) |

**Reasoning models** (Qwen3, etc.) will otherwise burn the token budget
"thinking" and return an empty summary — disable the think phase:

```bash
export EPHOR_SUMMARY_EXTRA_BODY='{"chat_template_kwargs":{"enable_thinking":false}}'
```

Any `<think>…</think>` still present in a reply is stripped automatically. As
with everything else, a failed or misconfigured summary call degrades to "—"
rather than breaking the dashboard.

The queue is smart about overlap so ten sessions finishing at once
don't talk over each other:

- **Different sessions** play one at a time, FIFO. The speech bar at the
  bottom of the TUI shows the current speaker and who's waiting.
- **Same session, newer reply** supersedes the old one — a queued entry
  is replaced; an in-flight one is **preempted** (you never hear a stale
  summary once a fresher one exists).
- The chars/sec rate **self-calibrates** from observed playback, so the
  progress bar matches your kokoro's actual speed.

### Quickstart

```bash
# Let ephor own playback (removes the tts-speak-response Stop hook so the
# FIFO queue is the single source of audio). Needs the kokoro pipeline.
ephor speech install

# Speak a one-line summary instead of the whole reply.
ephor speech mode summary

ephor                       # launch the TUI; replies now speak as they land
```

Controls:

| Where | Action |
|---|---|
| `ephor speech enable` / `disable` | Persistently turn audio on/off |
| `ephor speech mode full` / `summary` | Whole reply ↔ one-sentence brief |
| `ephor speech status` | Show on/off + mode and which layer decided |
| `ephor speech reset-calibration` | Forget the learned rate (after changing voice/speed) |
| TUI `m` | Mute / unmute (bar still shows speech, no audio) |
| TUI `M` | Toggle full ↔ summary live |
| TUI `t` | Jump to the session that's currently speaking |
| TUI `s` | Summarize the selected session on demand |

One-shot overrides (win over the saved settings, no disk write):
`EPHOR_TTS_ENABLED=0 ephor` to silence for one run, `EPHOR_TTS_MODE=summary`,
or `EPHOR_TTS_COMMAND=<path>` to point at a non-default playback command.

Requires a working kokoro TTS pipeline on disk (ephor looks for
`~/.local/share/kokoro-tts/play-ducked.sh`); without it, the speech bar
still mirrors what *would* play but no audio is produced.

## Design

[`docs/architecture.md`](docs/architecture.md) walks through the
components. Short version: the hook script writes JSON state, the
TUI reads it. There is no daemon.

## Privacy & security

`ephor` is a local tool. Nothing leaves your machine except for one
authenticated call to `https://api.anthropic.com/api/oauth/usage` to
compute per-account usage anchors (Claude Code only; skipped for other
agents). Full surface area in [`SECURITY.md`](SECURITY.md).

## Requirements

- Linux (any modern distro; tested on Ubuntu 24.04)
- Python 3.11+
- `jq` and `flock` on PATH (used by the shell hook handler)
- tmux 3.2+ (for jump-to-pane navigation)
- At least one supported coding agent installed (Claude Code, Gemini CLI,
  Codex CLI, or Grok CLI) with a session run after `ephor init`

## Contributing

Bug reports and PRs welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md)
for dev setup, testing conventions, and PR guidelines. Security
issues: see [`SECURITY.md`](SECURITY.md).

## License

MIT. Inspired by [`clorch`](https://github.com/androsovm/clorch) (the
macOS-only ancestor); patches that improve cross-platform support
upstream are encouraged.

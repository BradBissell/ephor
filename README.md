# cco — Claude Code Orchestrator for Linux

[![CI](https://github.com/BradBissell/claude-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/BradBissell/claude-orchestrator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

A Linux-native TUI that watches every Claude Code session you have
running and tells you — at a glance — which ones need your attention,
which are still working, and which went idle.

<img width="2806" height="1972" alt="image" src="https://github.com/user-attachments/assets/50f4b678-2da8-4ee1-b52e-3d148ce4ae7c" />


Or use it side by side with the active tmux session:
<img width="3838" height="2136" alt="image" src="https://github.com/user-attachments/assets/0f59b339-95e0-4189-8068-57bc53d1cdfd" />


## Why

Running 10+ Claude Code sessions in parallel is normal now. tmux shows
you all of them, but tmux can't tell you that session 7 is blocked on
a permission prompt while the other nine are still working. `cco`
solves exactly that — and presses Enter to jump to the right tmux
window.

## Install

```bash
pipx install cco
cco init           # installs the hooks into ~/.claude/settings.json
cco                # launches the TUI dashboard (alias: cco tui)
```

(Or use `pip install --user cco` / `uv tool install cco`.)

## Quickstart

1. **`cco init`** — adds Claude Code hooks to `~/.claude/settings.json`
   so every session reports its state. The original settings file is
   backed up; `cco uninstall` cleanly removes them.
2. **`cco`** (or `cco tui`) — opens the TUI. Use `j`/`k` or arrow keys to navigate,
   `/` to filter, `Enter` to jump to a session's tmux pane, `x` to
   kill, `?` for the full keymap.
3. **`cco list`** — script-friendly one-line-per-session status, for
   tmux status-right widgets or shell scripts.

See [`docs/getting-started.md`](docs/getting-started.md) for a longer
walkthrough.

## Highlights

- **Hook-driven, not scraped.** State comes from official Claude Code
  hook events — no terminal-output parsing, no AppleScript, no
  Wayland window-poking. Works the same in Ghostty, Alacritty, kitty,
  GNOME Terminal, or under `mosh`.
- **tmux-native navigation.** Every session is mapped to its tmux
  pane on every event, so `claude --resume` after a closed window
  self-heals. Pressing Enter does `tmux select-window -t <pane>`
  against your current client.
- **Per-session state on disk.** `$XDG_STATE_HOME/claude-orchestrator/`,
  mode 0600, atomic writes. Surviving a reboot is a feature.
- **Per-account 5h / 7d usage strip.** Anchors against the official
  `/api/oauth/usage` endpoint, then extrapolates with local ccusage
  deltas — accurate without hammering the API.
- **POSIX-shell hook handler** with `set -u`, sanitized PATH, jq
  `--arg` everywhere, per-session flock, and fail-OPEN error handling
  (a buggy hook never blocks Claude).
- **Auto-approve via hook return value**, not keystroke injection.
  Rules engine answers permission prompts before the dialog renders.
- **Spoken one-line summaries.** When a session finishes, cco can read
  back a ≤70-char summary of what Claude just did — so you can keep your
  eyes on one window and still know the other nine are done. See
  [Speak-back](#speak-back-tts) below.

## Speak-back (TTS)

Running ten sessions in parallel, the bottleneck isn't compute — it's
*you* noticing which one finished. cco can speak that for you.

On every `Stop` event, cco enqueues the session's reply to a single
FIFO speech queue shared across **all** your sessions, and plays it
through your local [kokoro](https://github.com/hexgrad/kokoro) TTS
pipeline. Two modes:

- **`summary` (recommended for parallel work).** cco shells out to
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
# Let cco own playback (removes the tts-speak-response Stop hook so the
# FIFO queue is the single source of audio). Needs the kokoro pipeline.
cco speech install

# Speak a one-line summary instead of the whole reply.
cco speech mode summary

cco                       # launch the TUI; replies now speak as they land
```

Controls:

| Where | Action |
|---|---|
| `cco speech enable` / `disable` | Persistently turn audio on/off |
| `cco speech mode full` / `summary` | Whole reply ↔ one-sentence brief |
| `cco speech status` | Show on/off + mode and which layer decided |
| `cco speech reset-calibration` | Forget the learned rate (after changing voice/speed) |
| TUI `m` | Mute / unmute (bar still shows speech, no audio) |
| TUI `M` | Toggle full ↔ summary live |
| TUI `t` | Jump to the session that's currently speaking |
| TUI `s` | Summarize the selected session on demand |

One-shot overrides (win over the saved settings, no disk write):
`CCO_TTS_ENABLED=0 cco` to silence for one run, `CCO_TTS_MODE=summary`,
or `CCO_TTS_COMMAND=<path>` to point at a non-default playback command.

Requires a working kokoro TTS pipeline on disk (cco looks for
`~/.local/share/kokoro-tts/play-ducked.sh`); without it, the speech bar
still mirrors what *would* play but no audio is produced.

## Design

[`docs/architecture.md`](docs/architecture.md) walks through the
components. Short version: the hook script writes JSON state, the
TUI reads it. There is no daemon.

## Privacy & security

`cco` is a local tool. Nothing leaves your machine except for one
authenticated call to `https://api.anthropic.com/api/oauth/usage` to
compute per-account usage anchors. Full surface area in
[`SECURITY.md`](SECURITY.md).

## Requirements

- Linux (any modern distro; tested on Ubuntu 24.04)
- Python 3.11+
- tmux 3.2+
- Claude Code installed and at least one session run

## Contributing

Bug reports and PRs welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md)
for dev setup, testing conventions, and PR guidelines. Security
issues: see [`SECURITY.md`](SECURITY.md).

## License

MIT. Inspired by [`clorch`](https://github.com/androsovm/clorch) (the
macOS-only ancestor); patches that improve cross-platform support
upstream are encouraged.

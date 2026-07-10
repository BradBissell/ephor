# Getting started with `ephor`

A 5-minute walkthrough from zero → live dashboard. Targets Ubuntu 22.04+
with bash, jq, tmux, and Python 3.11+.

## 1. Prerequisites

```bash
# Verify your distro has what's needed.
command -v jq    >/dev/null && echo "✓ jq"    || echo "  apt install jq"
command -v tmux  >/dev/null && echo "✓ tmux"  || echo "  apt install tmux"
command -v flock >/dev/null && echo "✓ flock" || echo "  (part of util-linux; should already be present)"
python3 -c 'import sys; assert sys.version_info >= (3,11)' && echo "✓ python ≥ 3.11"
```

## 2. Install

```bash
pipx install ephor          # or: pip install --user ephor / uv tool install ephor
ephor --version             # → ephor 0.1.1
```

To hack on ephor itself, install editable from a checkout instead:

```bash
cd ~/projects/ephor
pipx install --editable .
```

## 3. Wire up hooks

`ephor init` adds entries to `~/.claude/settings.json` so every Claude Code
event ends up writing to ephor's state dir. Other hooks already in your
`settings.json` (gsd-*, cbm-*, …) are preserved.

```bash
# Preview first — nothing is written.
ephor init --dry-run

# Actually install. Creates a timestamped backup before any change.
ephor init

# Restart any open Claude Code sessions so they pick up the new hooks.
```

To remove later:

```bash
ephor uninstall              # remove just ephor's hook entries
ephor uninstall --dry-run    # preview
ephor uninstall --restore-backup  # rescue mode: restore the most recent backup
```

## 4. Verify it's working

In one terminal, start a Claude Code session normally. In another:

```bash
ephor list      # rich table of every detected session
ephor status    # one-liner: "W:1 I:0 | total:1"
```

State files live at `~/.local/state/ephor/sessions/<sid>.json`
(override with `$EPHOR_STATE_DIR`).

## 5. tmux status-bar widget

Add this to `~/.tmux.conf` to show live session counts in tmux's status bar:

```tmux
# ephor widget — refreshes every 2s
set -g status-interval 2
set -g status-right '#(ephor tmux-widget) | %H:%M %d-%b'

# Optional: highlight tmux windows where activity is happening so you can
# spot which session needs attention even before checking ephor.
setw -g monitor-activity on
setw -g monitor-bell on
set  -g visual-activity off
set  -g visual-bell off
```

Reload tmux config inside an existing session:

```bash
tmux source-file ~/.tmux.conf
```

The widget output is colour-coded:

| Tag | Meaning |
|---|---|
| `PERM:N` | N sessions waiting on permission (red) |
| `WAIT:N` | N sessions waiting on user answer (yellow) |
| `ERR:N`  | N sessions in error state (purple) |
| `W:N`    | N sessions actively working (green) |
| `I:N`    | N idle sessions (dim) |
| `·`      | No sessions detected |

## 6. The dashboard (`ephor`)

Bare `ephor` (or the explicit `ephor tui`) opens the live Textual
dashboard — the daily-driver UI. Use
`j`/`k` or arrows to move, `Enter` to jump to a session's tmux pane,
`/` to filter, `x` to kill, `n` to hop to the next session needing
attention, and `?` for the full keymap.

Prefer the shell? The same state is available script-side:

```bash
# Watch the table refresh (2s default; tweak with -n)
watch -n 2 'ephor list'

# Inspect a specific session's raw JSON
cat ~/.local/state/ephor/sessions/<sid>.json | jq .
```

## 7. Hear your sessions (TTS speak-back)

When you're juggling many sessions, the hard part is noticing which one
just finished. ephor can speak a one-line summary of each reply so you can
stay focused on one window and still track the rest by ear.

Requires a local [kokoro](https://github.com/hexgrad/kokoro) TTS
pipeline (ephor looks for `~/.local/share/kokoro-tts/play-ducked.sh`, or
set `EPHOR_TTS_COMMAND` to your own). No Anthropic API key is needed — the
summarizer reuses your existing Claude Code login via `claude -p`.

```bash
# Hand playback to ephor. Removes the tts-speak-response Stop hook so ephor's
# cross-session FIFO queue is the single source of audio. Preview first:
ephor speech install --dry-run
ephor speech install

# Speak a one-sentence summary instead of the whole reply — ideal for
# parallel work. You'll hear e.g. "DR-1423: added retry to upload client".
ephor speech mode summary

# Confirm what's on and which layer (env / file / default) decided it.
ephor speech status
```

Now launch `ephor` and let a session finish — its summary speaks as soon
as the reply lands. If several sessions finish at once they queue and
play one at a time (newest reply for a given session wins; a stale one
mid-playback is preempted). The bottom speech bar shows who's talking
and who's waiting.

Handy controls while the TUI is open:

| Key | Action |
|---|---|
| `m` | Mute / unmute (the bar still mirrors speech; no audio) |
| `M` | Toggle full reply ↔ one-line summary live |
| `t` | Jump to the session currently speaking |
| `s` | Summarize the selected session on demand |

And from the shell:

```bash
ephor speech mode full          # read the whole reply instead of a summary
ephor speech disable            # silence audio (persisted)
ephor speech reset-calibration  # forget the learned rate after changing voice/speed

# One-shot overrides (no disk write) — e.g. silence ephor for a meeting:
EPHOR_TTS_ENABLED=0 ephor
EPHOR_TTS_MODE=summary ephor
```

Without the kokoro pipeline installed, every `ephor speech` command and
the speech bar still work — they just show what *would* play instead of
producing audio.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `ephor list` shows no sessions despite running Claude Code | `ephor init` not run, or claude was running before `ephor init` and hasn't been restarted |
| State files exist but `ephor list` says "No active sessions" | Check `$EPHOR_STATE_DIR` matches what your hooks write to (env var inheritance from your shell) |
| `ephor init` reports "already installed for: …" | Idempotent — nothing to do. To verify, `grep ephor ~/.claude/settings.json` |
| Hook handler errors in `~/.claude/settings.json.bak.*` | `ephor uninstall --restore-backup` resets to the most recent good backup |
| Want to nuke everything | `ephor uninstall && rm -rf ~/.local/state/ephor ~/.config/ephor` |

## 9. Going deeper

- [`architecture.md`](architecture.md) — how the hook → state-file → TUI
  pipeline fits together, plus the speech subsystem.
- [`../SECURITY.md`](../SECURITY.md) — the full network/disk surface area.
- `ephor doctor` — checks that hooks, paths, and dependencies are wired up
  correctly if something looks off.

# ephor — coding-agent session orchestrator for Linux

[![CI](https://github.com/BradBissell/ephor/actions/workflows/ci.yml/badge.svg)](https://github.com/BradBissell/ephor/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

A Linux-native TUI that watches every terminal coding-agent session you
have running — **Claude Code, Gemini CLI, Antigravity CLI, Codex CLI, Grok
CLI, and OpenCode** — and tells you, at a glance, which ones need your
attention, which are still working, and which went idle.

> `ephor` (Greek *ἔφορος*, "overseer") began as `cco`, a Claude-Code-only
> dashboard. It now speaks the hook protocol of five coding agents that
> share Claude Code's stdin-JSON hook model, plus OpenCode via a plugin.

## Supported agents

| Agent | Settings file | How ephor hooks in |
|---|---|---|
| **Claude Code** | `~/.claude/settings.json` | `hooks` object, command + stdin JSON |
| **Gemini CLI** | `~/.gemini/settings.json` | same `hooks` shape (`Before*`/`After*` events) |
| **Antigravity CLI** (Google `agy`) | `~/.gemini/config/hooks.json` | dedicated hooks file keyed by a named hook group; `PreInvocation`/`PreToolUse`/`PostToolUse`/`Stop` events |
| **Codex CLI** | `~/.codex/hooks.json` | dedicated hooks file, command + stdin JSON |
| **Grok** (xAI Grok Build) | `~/.grok/hooks/ephor.json` | JSON hooks file; camelCase/snake_case dialect |
| **OpenCode** | `~/.config/opencode/plugins/ephor.js` | bundled JS plugin → shells out to the handler |

The first five share the stdin-JSON hook model, so one shell handler serves
them all — it understands each agent's event-name and field-name dialect and
records which agent a session belongs to. Antigravity (the Gemini CLI
successor; its binary is `agy`, not `antigravity`) uses the same stdin JSON but
keys its hooks file by a named group rather than a top-level `hooks` object, so
its config lands at `~/.gemini/config/hooks.json` (the shared config root agy's
backend reads; the legacy `~/.gemini/antigravity-cli/hooks.json` is only loaded
by the TUI). OpenCode has no
shell hooks, so `ephor init --provider opencode` installs a tiny JS **plugin**
that translates OpenCode's bus events and pipes them into the *same* handler —
so every agent funnels through one state writer.

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

`ephor-orchestrator` is not on PyPI yet (the bare name `ephor` is taken by an
unrelated project). Install from GitHub with the TUI extra:

```bash
# pipx
pipx install 'ephor-orchestrator[tui] @ git+https://github.com/BradBissell/ephor'

# or uv
uv tool install 'ephor-orchestrator[tui] @ git+https://github.com/BradBissell/ephor'

ephor init --provider all   # installs hooks into every agent you use
ephor                       # launches the TUI dashboard (alias: ephor tui)
```

Without `[tui]` you still get `ephor list` / `init` / `doctor`; the dashboard
needs Textual, which the extra pulls in.

Editable checkout (for hacking on ephor itself):

```bash
cd /path/to/ephor
pipx install --editable '.[tui]'
# or: uv tool install --editable '.[tui]'
```

## Quickstart

1. **`ephor init [--provider <agent>|all]`** — registers ephor's hook in
   the agent's settings file (`--provider` defaults to `claude`; pass
   `gemini`, `agy`, `codex`, `grok`, `opencode`, or `all`). The original
   settings file is backed up; `ephor uninstall --provider <agent>` cleanly
   removes them.
2. **`ephor`** (or `ephor tui`) — opens the TUI. Use `j`/`k` or arrow keys to navigate,
   `/` to filter, `Enter` to jump to a session's tmux pane, `x` to
   kill, `o` (or a click on the Jira key) to open that session's pull
   request, `p` to pin a ticket to the selected session, `?` for the full
   keymap.
### The Jira column

The tail of each row is the session's Jira key. The best way to get it
right is not to guess at all: export `EPHOR_TICKET=DR-8222` before
launching the agent and the hook records it, which beats every heuristic
below and survives branch switches, sessions sitting on `main`, and
shared checkouts. Failing that, press `p` on a row and type the key —
pins are stored by session id and persist across restarts.

When neither is set, ephor harvests from whichever signal the session
actually offers, in this order of trust:

| Signal | Why it ranks there |
|---|---|
| git branch at the session's cwd | the worktree-per-ticket case |
| the branch's upstream | rescues a detached HEAD, and a local `work` branch pushed as `origin/DR-8222-x` |
| path, from cwd up to the **worktree root** | `~/projects/work/aim-myt/DR-8222`; stopping at the root keeps an unrelated `~/DR-100-scratch/` ancestor from claiming the session |
| the branch's own commit subjects and bodies | catches `fix/some-descriptive-name` whose commits say `feat(DR-8222): …` |
| the pull request's title, body and head ref | free — `gh` was already called for the link |
| tmux window label | set by a start-work flow, but a label a human can rename |
| the latest user prompt | "implement DR-8222" is how most of these start |
| the LLM summary | generated text, and partly circular — it is last for a reason |

The first six are facts; the last three are readings of prose, and a key
that came from one of those is drawn amber and italic rather than green,
so a lucky regex hit never looks like a branch name. Well-known
lookalikes (`UTF-8`, `SHA-256`, `ISO-8601`, …) are never mistaken for
keys — and setting `EPHOR_TICKET_PROJECTS=DR,ABC` replaces that denylist
with an allowlist of your own project prefixes, which is strictly more
precise.

The key is a link. ephor resolves the session's pull request in the
background with `gh` — first the PR whose head is the checked-out branch,
then a repo search for the ticket — and once found the key is underlined
and clicking it (or pressing `o` on the row) opens the PR in your
browser. Color tracks PR state: green open, purple merged, red closed. A
plain green key means "ticket known, no PR yet"; press `o` anyway and
ephor looks it up on the spot.

A session that outlives its answers re-harvests on its own: a "no PR
yet" is re-probed every 90s, so a branch you push mid-session gains its
link without a restart. PRs are cached per checkout *branch*, so
switching branches in a shared checkout gets a fresh lookup rather than
the previous branch's review. Resolved PRs are held for an hour — press `r` to
force the issue. That drops every memoized ticket *and* PR and re-runs
`git` and `gh` from scratch for every visible session, which is what you
want after switching a session's branch or merging its PR. Pressing `o`
on a session with no PR always re-probes that one session, so the
push-then-press-again loop works.

Requires `gh` installed and authenticated. Without it the column still
shows tickets — it just never gains links.

### The work column

Right of the ticket sits what the ticket's pull request is actually doing:

```
DR-8222  #143 v?    CI green, waiting on a reviewer
DR-8231  #147 x     CI failing — and the session went IDLE twenty minutes ago
DR-8199  #139 v!    reviewer asked for changes
DR-8150  #131 v>    branch no longer merges cleanly
DR-8101  #128 v+~   approved and merged, but the ticket still says In Progress
```

`v`/`x`/`~` is the rolled-up CI verdict, `+`/`!`/`?` the review verdict,
`>` a merge conflict, and a trailing `~` means the ticket and its PR have
drifted apart. All of it comes from the same `gh` round trip that already
resolved the link — `statusCheckRollup`, `reviewDecision`,
`mergeStateStatus`.

This is the column that changes what the dashboard is for. The status
machine can only say what the *agent* is doing, so a session that finished
its work and left red CI behind renders as a calm grey IDLE row — the
quietest thing on a board where it is the most urgent. Those rows now light
up, and `n` (next-attention) walks them alongside the sessions blocked on a
permission prompt.

### Work items

A ticket outlives the sessions that work on it, so ephor keeps a record
that does too: one file per key under `$XDG_STATE_HOME/ephor/work/`,
holding the branch, the worktree, the PR, the Jira status and every session
id that has touched it. It is what makes `ephor work` and `ephor log
DR-8222` answerable at all, and it feeds two things back into the board:

- **Promotion.** The moment any fact-tier probe (branch, path, commits, PR)
  establishes a key, it is written down. A session whose branch is later
  deleted — or whose worktree is cleaned up — keeps its settled green key
  instead of decaying back into an amber guess.
- **Grouping.** Because the record is keyed by ticket rather than session,
  `g` can fold the list by ticket (or by repo): several sessions attempting
  one ticket collapse under one header that counts how many are working and
  how many need you.

### Jira, if you want it

Set `EPHOR_JIRA_URL`, `EPHOR_JIRA_EMAIL` and `EPHOR_JIRA_TOKEN` and ephor
reads each ticket's real status, summary and assignee. The subline gains a
`«In Progress»` tag, and ephor starts flagging **drift** — "PR merged,
ticket still In Progress", or "ticket Done, PR still open". That one costs
a week at a time and nothing else in the toolchain notices it.

Unset, no request is made. ephor stays a local tool until told otherwise.

### Managing twenty-five sessions

| Key | Does |
|---|---|
| `space` | mark a row (on a group header: the whole group) |
| `c` | clear every mark |
| `g` / `z` | cycle grouping (none → ticket → repo) / fold the group at the cursor |
| `a` / `d` | allow / deny the marked sessions' permission request |
| `A` / `D` | standing allow for a session / revoke a decision |
| `N` / `R` | start another session on this ticket / reopen this session |
| `x`, `s`, `o` | kill, summarize, open PR — all act on the marked set |

**Marking is what makes an action a batch action.** With nothing marked
every key behaves exactly as it did before, on the cursor row.

#### The permission inbox

`a` and `d` write a decision into `$XDG_STATE_HOME/ephor/pending/`, which
the hook handler has always known how to read and emit — so answering from
the dashboard is the existing auto-approve mechanism, not keystroke
injection into a pane.

There is a caveat worth stating plainly, because ephor states it in the
toast rather than letting you discover it: the hook runs *before* the agent
draws its dialog, so by default your answer lands on that session's **next**
request, not the one on screen. Set `EPHOR_PERMISSION_WAIT_SEC=10` and the
handler will instead write its WAITING_PERMISSION state and then wait up to
ten seconds for you to answer, which is what makes `a` unblock the prompt
you are looking at. The trade is real and that is why it is off by default:
while the handler waits, the agent has not drawn its prompt, so the pane
cannot be answered either.

`A` (standing allow) sidesteps the whole question — "yes, this session may
keep doing what it is doing" is one judgement, made once, instead of
re-litigated at every prompt. It applies until `D` revokes it, and it is
per session, never global.

#### Shared-worktree collisions

Two agents editing one working tree interleave their writes and neither can
tell. ephor already records every session's cwd, so it says so: a row whose
worktree is shared with another live session replaces its subline with a red
warning. It reports rather than prevents — a shared checkout is occasionally
what you meant, and a locked-out agent is a worse failure than a warned one.

#### When you are away from the desk

Speak-back solves "which of these ten finished" when you are at the machine.
`EPHOR_NOTIFY_URL` solves it when you are not: point it at an
[ntfy](https://ntfy.sh) topic (or any webhook) and ephor pushes once when a
session starts needing you — blocked on permission, erroring, or sitting on
red CI. Once, not continuously: a session blocked for forty minutes is one
event. Unset, nothing is sent.

3. **`ephor start DR-8222 --title "add retry"`** — the other direction:
   ephor creates the worktree, opens a tmux window named for the ticket,
   symlinks the repo's `.env*` files in, and launches the agent with
   `EPHOR_TICKET` already set. Nothing is inferred, because nothing has to
   be. `ephor resume <sid>` reopens a finished session in its original
   directory.
4. **`ephor work`** — the ticket-shaped view: one row per piece of work,
   with its branch, its PR, its Jira status and how many of its sessions
   are still alive. Survives every one of those sessions dying.
5. **`ephor log [DR-8222]`** — what *happened*, as opposed to what is true
   now. Status transitions, PR links, CI flips, permission answers.
   `--since 2h` for the overnight recap.
6. **`ephor list`** — script-friendly one-line-per-session status, for
   tmux status-right widgets or shell scripts.
7. **`ephor doctor`** — checks dependencies and, per agent, whether its
   CLI is installed and ephor's hooks are registered.

See [`docs/getting-started.md`](docs/getting-started.md) for a longer
walkthrough.

## Highlights

- **Hook-driven, not scraped.** State comes from each agent's official
  hook events — no terminal-output parsing, no AppleScript, no
  Wayland window-poking. Works the same in Ghostty, Alacritty, kitty,
  GNOME Terminal, or under `mosh`.
- **One handler, five agents.** A single POSIX-shell handler normalizes
  every stdin-JSON agent's event vocabulary (e.g. Gemini's
  `BeforeTool`/`AfterAgent`, Antigravity's `PreInvocation`, Grok's
  `user_prompt`) into one on-disk state schema, tagged with the
  `provider` that produced it. OpenCode's JS plugin funnels into the same
  handler, so all six share one state writer.
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
  Rules engine answers permission prompts before the dialog renders, and
  the dashboard's own `a`/`d` keys ride the same mechanism.
- **Work outlives sessions.** One record per ticket — branch, worktree, PR,
  CI, Jira status, every session that touched it — so `ephor log DR-8222`
  can answer what happened overnight.
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
works for **all six agents** — the reply text is captured at turn-end from
each: an event-payload field (Gemini, Codex), the plugin's SDK (OpenCode), or
the session transcript (Claude's JSONL, Antigravity's transcript, Grok Build's
ACP `session/update` stream). Two modes:

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

`ephor` is a local tool. By default the only thing that leaves your machine
is one authenticated call to `https://api.anthropic.com/api/oauth/usage` to
compute per-account usage anchors (Claude Code only; skipped for other
agents).

Two integrations can add egress, and both are off until you configure them:
the Jira read (`EPHOR_JIRA_*`) talks to the site you name, and push
notification (`EPHOR_NOTIFY_URL`) talks to the URL you name. Neither sends
prompts, replies or transcript content — and the Jira client refuses to send
credentials over plain `http://`. Full surface area in
[`SECURITY.md`](SECURITY.md).

## Requirements

- Linux (any modern distro; tested on Ubuntu 24.04)
- Python 3.11+
- `jq` and `flock` on PATH (used by the shell hook handler)
- tmux 3.2+ (for jump-to-pane navigation)
- At least one supported coding agent installed (Claude Code, Gemini CLI,
  Antigravity CLI, Codex CLI, Grok CLI, or OpenCode) with a session run
  after `ephor init`

## Contributing

Bug reports and PRs welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md)
for dev setup, testing conventions, and PR guidelines. Security
issues: see [`SECURITY.md`](SECURITY.md).

## License

MIT. Inspired by [`clorch`](https://github.com/androsovm/clorch) (the
macOS-only ancestor); patches that improve cross-platform support
upstream are encouraged.

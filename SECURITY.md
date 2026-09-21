# Security Policy

## Reporting a Vulnerability

If you find a security issue in `ephor`, please **do not open a public
GitHub issue**. Instead:

- Use [GitHub's private vulnerability reporting][advisory] on this
  repository (Security tab → "Report a vulnerability"), or
- Email the maintainer at the address listed on the GitHub profile.

You can expect an initial acknowledgement within 7 days. Once the issue
is confirmed, we'll work on a fix and a coordinated disclosure window.

[advisory]: https://github.com/BradBissell/ephor/security/advisories/new

## Supported Versions

Only the latest minor release on `main` receives security fixes during
the `0.x` series. Once `1.0` ships, we'll backport critical fixes to
the previous minor.

## What `ephor` Touches on Your Machine

`ephor` is a local TUI. It runs entirely on your workstation, and makes no
network request at all except the one usage call below and the two opt-in
integrations described under **Network egress**. For full transparency:

- **Read** `~/.claude/.credentials.json` to compute per-account usage
  anchors. Only the OAuth access token is sent (over HTTPS) to the
  hardcoded `https://api.anthropic.com/api/oauth/usage` endpoint.
- **Read** `~/.claude/projects/<encoded-cwd>/<session>.jsonl` transcripts
  to count tokens and produce a one-line summary. Transcript content
  never leaves your machine.
- **Write** per-session state files at
  `$XDG_STATE_HOME/ephor/sessions/<session>.json` (mode
  `0600`, parent dir `0700`). Each file contains the first 70 chars of
  your most recent prompt as a "last summary" hint, plus tmux pane IDs
  and Claude session metadata.
- **Write** per-ticket work records at `$XDG_STATE_HOME/ephor/work/<KEY>.json`
  (mode `0600`, parent dir `0700`). Contains the ticket key, branch,
  worktree path, pull-request number/URL/state, Jira status/summary/assignee
  when configured, and the ids of the sessions that worked on it. No prompts,
  no transcript content.
- **Write** an event log at `$XDG_STATE_HOME/ephor/events.ndjson` (mode
  `0600`). One line per state transition: session id, ticket, and a short
  label such as `IDLE → WORKING` or `#143: PASSING → FAILING`. No prompts,
  no transcript content. Rotated at 8 MiB, one generation kept.
- **Write** audit snapshots at `$XDG_STATE_HOME/ephor/audit.ndjson` (mode
  `0600`, parent dir `0700`). One line per audited session: token counts by
  class, equivalent-cost figures, timings, tool *names* and call counts, the
  PR number and outcome. No prompts, no tool arguments, no transcript
  content. Append-only; `ephor audit --compact` collapses it.
- **Write** permission decisions at
  `$XDG_STATE_HOME/ephor/pending/<session>.json` (mode `0600`). Contains an
  allow/deny verdict and a fixed reason string — never tool arguments.
  One-shot decisions are deleted by the hook that reads them; standing ones
  (`<session>.always.json`) persist until you revoke them.
- **Run** `git worktree add` and `tmux new-window` when you invoke
  `ephor start` / `ephor resume`. Arguments are argv lists, never a shell
  string; the ticket key is validated against `[A-Z][A-Z0-9]{1,9}-\d{1,7}`
  before it reaches a path or a branch name, and user-supplied titles are
  slugified to `[a-z0-9-]`.
- **Write** usage caches at `$XDG_CACHE_HOME/ephor/` (mode
  `0600`, parent dir `0700`). Contains hashed account fingerprints and
  aggregate token counts — no prompts, no responses, no tokens.
- **Run** `tmux` subprocesses to discover panes and switch the user's
  current window. All arguments are passed as argv lists (never a shell
  string); pane and session IDs are validated before use.
- **Install** Claude Code hooks into `~/.claude/settings.json` when you
  run `ephor init`. The hook script lives in this repo at
  `src/ephor/hooks/event_handler.sh` and runs once per
  Claude tool event with `set -u`, a sanitized `PATH`, and unset
  `BASH_ENV`/`ENV`/`PROMPT_COMMAND` so it can't be hijacked by a
  poisoned environment.

If any of the above surprises you, that's a doc bug — please report it.

## Network Egress

Five requests can leave your machine. Three of them do not exist until you
configure them or ask for them:

| When | Where | What is sent |
|---|---|---|
| Always (Claude accounts) | `https://api.anthropic.com/api/oauth/usage` | your Claude Code OAuth token |
| Whenever a PR is resolved | GitHub, via the `gh` CLI | a branch name or ticket key, under **your** existing `gh` auth |
| Only if `EPHOR_JIRA_*` is set | the Jira site **you** name | HTTP Basic auth (your email + API token), and a ticket key in the path |
| Only if `EPHOR_NOTIFY_URL` is set | the URL **you** name | a session's ticket/project name and why it needs you (and a PR URL, when the reason came from one) |
| Only on `ephor audit --judge` | GitHub (`gh`), then Anthropic (`claude -p`) | a merged PR's title, description and changed **file names** |

No prompt text, no assistant replies and no transcript content are sent to
any of them.

`ephor audit` **without** `--judge` makes no network request: it reads
transcripts, work records and the event log off local disk.

Two guards are worth naming:

- The Jira client **refuses to send credentials over plain `http://`**, even
  to a host you configured yourself.
- The notifier sends a ticket key and a project name — not a summary, and
  never the conversation. If your ticket keys or repository names are
  themselves sensitive, leave `EPHOR_NOTIFY_URL` unset; a public ntfy topic
  is readable by anyone who guesses its name. Its dedupe ledger
  (`notify.json`, beside the session state) records session ids and reason
  names so a dashboard restart does not re-announce a board you have already
  seen; it holds no ticket text and is written `0600`.
- `ephor audit --judge` is the only part of auditing that sends anything
  anywhere, it runs **only** when you pass that flag, and it grades merged
  PRs only. It is deliberately never shown the transcript — the agent's own
  account of what it did is the least trustworthy evidence for the question
  being asked — and never the patch, only the list of changed file names. If
  a PR description or a filename is sensitive, do not pass `--judge`.

### `EPHOR_PERMISSION_WAIT_SEC`

Setting this makes the hook handler pause on a permission request, for up to
the configured number of seconds, waiting for a decision from the dashboard.
It is **off by default**. Two consequences to understand before enabling it:

- While the handler waits, the agent has not yet drawn its own prompt, so
  the request cannot be answered in the pane either.
- It is capped at 60 seconds and any non-numeric value disables it, so a
  typo cannot hang an agent indefinitely. The handler still fails OPEN: if
  no decision arrives it emits nothing, and the agent's normal dialog
  appears.

A standing decision (`<session>.always.json`) auto-answers **every**
permission request from that session until revoked. It is scoped to one
session id and is never global, but within that session it is exactly as
permissive as it sounds.

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

`ephor` is a local TUI. It runs entirely on your workstation and never
sends your data to any third party. For full transparency:

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

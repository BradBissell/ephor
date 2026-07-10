# Contributing to ephor

Thanks for your interest! `ephor` is a small, opinionated tool — bug
reports, fixes, and well-scoped features are all welcome.

## Quick start

```bash
git clone https://github.com/BradBissell/claude-orchestrator
cd ephor

# Editable install with dev + TUI extras.
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,tui]"

# Run the test suite.
pytest

# Lint, format, type-check.
ruff check .
ruff format --check .
mypy src

# Shell-lint the hook handler.
shellcheck src/ephor/hooks/event_handler.sh
```

The CI workflow at `.github/workflows/ci.yml` runs all of the above
on every PR, across Python 3.11–3.13. Get them green locally before
opening a PR and you'll save a round-trip.

## What we look for in a PR

- **Tight scope.** Bug fixes don't need surrounding refactors. A
  feature PR should do one thing.
- **Tests for new behavior.** State-file invariants in particular
  (atomic writes, 0600 mode, schema migrations) need explicit tests.
- **Clear PR description.** What problem? Why this approach?
  Screenshots/asciicasts for TUI changes.
- **Conventional commit style** — e.g. `fix(tmux): target $TMUX_PANE
  in display-message`. Not strictly enforced, but it makes the
  changelog easier.

## Areas where help is especially welcome

- **More terminal emulator support.** `ephor` is terminal-agnostic
  today (it only talks to tmux), but if you find a setup where Enter
  doesn't switch panes, a bug report with `tmux info` output is gold.
- **Notification backends** beyond libnotify (mako, dunst-rs, etc).
- **Distro-specific install instructions** for the README.
- **Documentation** — every "I had to figure this out" deserves a
  paragraph in `docs/`.

## Code style

- Python: ruff handles formatting. The strict mypy config is real;
  please don't add `# type: ignore` without a comment explaining why.
- Shell: `shellcheck` clean, `set -u` everywhere, every variable
  flowing into `tmux` / `jq` is quoted and (where it crosses a trust
  boundary) regex-validated. See `src/ephor/hooks/event_handler.sh`
  for the established patterns.

## Reporting security issues

Please do **not** open a public GitHub issue for security problems.
See [`SECURITY.md`](SECURITY.md) for the disclosure process.

## License

By contributing you agree that your contributions are licensed under
the MIT license, the same license that covers the rest of the project.

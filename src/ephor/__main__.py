"""Allow `python -m ephor` to invoke the CLI."""

from __future__ import annotations

from ephor.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

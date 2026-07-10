"""Hook installer and event handler shim.

The real work happens in `event_handler.sh` (POSIX shell, sub-15 ms p95).
This package exists so the .sh file is bundled with the wheel and locatable
via `ephor.config.hook_handler_path()`.
"""

from __future__ import annotations

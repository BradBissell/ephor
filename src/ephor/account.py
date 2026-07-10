"""User-configured account metadata (token cap, etc.).

Read once at startup from ~/.config/ephor/account.toml. The
file is optional; missing → no cap, summary line just shows the total.

Schema (all keys optional):
  weekly_cap_tokens   = 3_000_000_000   # ccusage 7d total ceiling (default)
  five_hour_cap_tokens = 200_000_000    # ccusage active-block ceiling (default)

  # Per-account overrides, keyed by subscriptionType from .credentials.json:
  [profiles.max]
  weekly_cap_tokens   = 3_000_000_000
  five_hour_cap_tokens = 200_000_000

  [profiles.enterprise]
  weekly_cap_tokens   = 10_000_000_000
  five_hour_cap_tokens = 500_000_000

Both caps drive the percentage + green/yellow/orange/red color in the TUI's
bottom strip. When unset, the 5h segment falls back to ccusage's
``--token-limit max`` (highest historical 5h block) so you still get a
percentage signal, just benchmarked against your own peak instead of a
fixed plan ceiling. Anthropic publishes plan limits in messages/hours
rather than tokens, so there's no canonical figure to default to.

Per-profile caps win over top-level caps when the active account's
``subscriptionType`` matches a profile name; this lets you switch between
``claude /login`` orgs and have the colored percentages reflow against
the right ceilings automatically.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ephor.config import _xdg_home

log = logging.getLogger(__name__)


def account_config_path() -> Path:
    """Resolve the account config path, honoring EPHOR_ACCOUNT_CONFIG override
    (used by tests to point at a tmp_path file)."""
    raw = os.environ.get("EPHOR_ACCOUNT_CONFIG")
    if raw:
        return Path(raw).expanduser()
    return _xdg_home("XDG_CONFIG_HOME", ".config") / "ephor" / "account.toml"


@dataclass(frozen=True)
class AccountConfig:
    """Subset of account.toml the dashboard cares about. Empty/None when
    the file is absent or malformed; consumers must handle that."""

    weekly_cap_tokens: int | None = None
    five_hour_cap_tokens: int | None = None
    # Per-subscriptionType overrides. Each value is a dict with optional
    # ``weekly_cap_tokens`` and ``five_hour_cap_tokens`` ints. Missing keys
    # fall back to the top-level defaults above.
    profiles: dict[str, dict[str, int]] = field(default_factory=dict)


def _positive_int_or_none(raw: object) -> int | None:
    """Coerce a TOML value to a positive int, or None for missing/invalid."""
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return None


def _parse_profiles(raw: object) -> dict[str, dict[str, int]]:
    """Validate the optional ``[profiles.<name>]`` blocks.

    Each profile is keyed by subscription_type string. Only known cap
    fields are kept; unknown fields are silently dropped so a typo in
    the config doesn't propagate junk into AccountConfig.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for name, block in raw.items():
        if not isinstance(name, str) or not isinstance(block, dict):
            continue
        clean: dict[str, int] = {}
        for key in ("weekly_cap_tokens", "five_hour_cap_tokens"):
            val = _positive_int_or_none(block.get(key))
            if val is not None:
                clean[key] = val
        if clean:
            out[name] = clean
    return out


def load_account_config(path: Path | None = None) -> AccountConfig:
    """Load and validate account.toml. Returns defaults on any failure —
    the dashboard should never crash because the config is missing or bad.
    """
    target = path if path is not None else account_config_path()
    if not target.is_file():
        return AccountConfig()
    try:
        with target.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("account.toml unreadable, ignoring: %s", exc)
        return AccountConfig()

    return AccountConfig(
        weekly_cap_tokens=_positive_int_or_none(data.get("weekly_cap_tokens")),
        five_hour_cap_tokens=_positive_int_or_none(data.get("five_hour_cap_tokens")),
        profiles=_parse_profiles(data.get("profiles")),
    )

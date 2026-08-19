"""Environment settings that treat "set but empty" as "not set".

GitHub Actions substitutes a MISSING secret as an empty string, so a workflow
line like

    DAILY_PREPARE_TARGET: ${{ secrets.DAILY_PREPARE_TARGET }}

exports `DAILY_PREPARE_TARGET=""` when that secret was never created. Plain
`os.environ.get("K", default)` then returns `""` rather than the default,
because the key genuinely exists. The consequences ranged from loud to nasty:

  int("")            -> ValueError, killing `prepare`/`send` on startup
  SENDER_NAME=""     -> outreach signed with nobody's name
  ORG_NAME="" etc.   -> a broken CAN-SPAM footer on real mail
  PACKET_URL=""      -> "take a look:" followed by nothing

These readers coerce blank to the default, so an unset secret degrades to the
sane built-in instead of breaking. Use them for anything a workflow might pass.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def env_str(key: str, default: str) -> str:
    """Value for `key`, falling back to `default` when unset OR blank."""
    return (os.environ.get(key) or "").strip() or default


def env_int(key: str, default: int) -> int:
    raw = env_str(key, str(default))
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer; using %d", key, raw, default)
        return default


def env_float(key: str, default: float) -> float:
    raw = env_str(key, str(default))
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", key, raw, default)
        return default


def env_flag(key: str, default: bool = False) -> bool:
    raw = (os.environ.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")

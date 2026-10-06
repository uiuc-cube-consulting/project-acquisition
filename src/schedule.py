"""When the daily jobs may do their work.

GitHub's cron is best-effort. Through August 2026 scheduled runs started ~30
minutes late; since September they start 4-8 hours late (prepare due 12:00 UTC
started 17:00-20:00 UTC), which pushed sends to late afternoon Central. The
workflows therefore fire at several slots a day, and these guards make the
extra runs harmless:

- `prepare` drafts once per Central-time day; later slots see today's drafts
  and exit.
- `send` only mails inside the business-hours window, and DAILY_SEND_CAP is a
  per-day total, not per run.

Central time (America/Chicago) handles DST, which a UTC cron cannot.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .env import env_int

CENTRAL = ZoneInfo("America/Chicago")


def now_ct() -> datetime:
    return datetime.now(CENTRAL)


def send_window() -> tuple[int, int]:
    """[start, end) hours, Central time. Default 8am-4pm, so mail lands while
    people are at their desks rather than at the end of the day."""
    return env_int("SEND_WINDOW_START_HOUR", 8), env_int("SEND_WINDOW_END_HOUR", 16)


def in_send_window(now: datetime | None = None) -> bool:
    now = (now or now_ct()).astimezone(CENTRAL)
    start, end = send_window()
    return now.weekday() < 5 and start <= now.hour < end


def describe_send_window() -> str:
    start, end = send_window()
    return f"Mon-Fri {start:02d}:00-{end:02d}:00 Central"


def is_today_ct(ts: object, now: datetime | None = None) -> bool:
    """True if an ISO timestamp from the Sheet falls on today's Central date."""
    raw = str(ts or "").strip()
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # the Sheet stores UTC
    today = (now or now_ct()).astimezone(CENTRAL).date()
    return dt.astimezone(CENTRAL).date() == today

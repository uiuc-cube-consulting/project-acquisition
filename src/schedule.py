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
- Neither job does outreach on dates in config/blackout_dates.yaml unless forced.

Central time (America/Chicago) handles DST, which a UTC cron cannot.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .env import env_int

CENTRAL = ZoneInfo("America/Chicago")
BLACKOUT_PATH = Path(__file__).resolve().parent.parent / "config" / "blackout_dates.yaml"


def _load_blackouts() -> list[tuple[date, date, str]]:
    """Validate the complete calendar before using any entries; never fail open."""
    def invalid(message: str) -> ValueError:
        return ValueError(f"Invalid blackout configuration {BLACKOUT_PATH}: {message}")

    def parse_day(value: object, entry: str) -> date:
        # safe_load turns unquoted ISO dates into date objects.
        if type(value) is date:
            return value
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
                if parsed.isoformat() == value:
                    return parsed
            except ValueError:
                pass
        raise invalid(f"{entry} must be a date in YYYY-MM-DD format; got {value!r}")

    try:
        config = yaml.safe_load(BLACKOUT_PATH.read_text())
    except (yaml.YAMLError, ValueError) as exc:
        raise invalid(f"cannot parse YAML: {exc}") from exc
    if not isinstance(config, dict) or set(config) - {"dates", "ranges"}:
        raise invalid("expected a mapping containing only 'dates' and 'ranges'")
    for key in ("dates", "ranges"):
        if not isinstance(config.get(key, []), list):
            raise invalid(f"'{key}' must be a list")

    blackouts = []
    for index, entry in enumerate(config.get("ranges", [])):
        label = f"ranges[{index}]"
        if (not isinstance(entry, dict) or not {"start", "end"} <= set(entry)
                or set(entry) - {"start", "end", "why"}):
            raise invalid(f"{label} must contain 'start', 'end', and optionally 'why'")
        start = parse_day(entry["start"], f"{label}.start")
        end = parse_day(entry["end"], f"{label}.end")
        if start > end:
            raise invalid(f"{label}.start must be on or before end")
        reason = entry.get("why", "Configured blackout range")
        if not isinstance(reason, str) or not reason.strip():
            raise invalid(f"{label}.why must be a non-empty string")
        blackouts.append((start, end, reason.strip()))
    for index, entry in enumerate(config.get("dates", [])):
        day = parse_day(entry, f"dates[{index}]")
        blackouts.append((day, day, "Configured blackout date"))
    return blackouts


def blackout_reason(day: date) -> str | None:
    """Reason for a Central-time calendar date, or None; ranges are inclusive."""
    for start, end, reason in _load_blackouts():
        if start <= day <= end:
            return reason
    return None


def is_blackout(day: date) -> bool:
    return blackout_reason(day) is not None


def now_ct() -> datetime:
    return datetime.now(CENTRAL)


def send_window() -> tuple[int, int]:
    """[start, end) hours, Central time. Default 8am-4pm, so mail lands while
    people are at their desks rather than at the end of the day."""
    return env_int("SEND_WINDOW_START_HOUR", 8), env_int("SEND_WINDOW_END_HOUR", 16)


def in_send_window(now: datetime | None = None) -> bool:
    now = (now or now_ct()).astimezone(CENTRAL)
    start, end = send_window()
    blackout = is_blackout(now.date())
    return not blackout and now.weekday() < 5 and start <= now.hour < end


def describe_send_window(now: datetime | None = None) -> str:
    start, end = send_window()
    today = (now or now_ct()).astimezone(CENTRAL).date()
    upcoming = [(max(first, today), reason) for first, last, reason in _load_blackouts()
                if last >= today]
    description = f"Mon-Fri {start:02d}:00-{end:02d}:00 Central, excluding blackout dates"
    if upcoming:
        day, reason = min(upcoming, key=lambda entry: entry[0])
        return f"{description}; next blackout: {day.isoformat()} ({reason})"
    return f"{description}; no upcoming blackouts configured"


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

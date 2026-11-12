"""Pure reminder rules for Hot Leads."""

from datetime import date, datetime


def _as_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def business_days_between(start, end, blackout_dates=None) -> int:
    """Count weekdays after start through end, excluding blackout dates."""
    start_date = _as_date(start)
    end_date = _as_date(end)

    if not start_date or not end_date or end_date <= start_date:
        return 0

    blackouts = {_as_date(d) for d in (blackout_dates or [])}
    current = start_date
    count = 0

    while current < end_date:
        current = date.fromordinal(current.toordinal() + 1)
        if current.weekday() < 5 and current not in blackouts:
            count += 1

    return count


def reminder_reasons(lead: dict, today: date, blackout_dates=None) -> list[str]:
    """Return every reminder rule that applies to one Hot Lead."""
    reasons = []
    owner = str(lead.get("owner") or "").strip()
    next_step = str(lead.get("next_step") or "").strip()

    latest_message = lead.get("latest_message_at")
    latest_from_prospect = str(
        lead.get("latest_sender_is_prospect", "")
    ).strip().lower() in ("true", "yes", "1", "y")

    if (
        latest_from_prospect
        and business_days_between(latest_message, today, blackout_dates) > 2
    ):
        reasons.append("Reply owed")

    flagged_at = lead.get("flagged_at")

    if not owner and business_days_between(
        flagged_at, today, blackout_dates
    ) > 1:
        reasons.append("Unowned")

    if owner and not next_step and business_days_between(
        flagged_at, today, blackout_dates
    ) > 3:
        reasons.append("No next step")

    next_touch = _as_date(lead.get("next_touch"))
    if next_touch and next_touch <= today:
        reasons.append("Due")

    return reasons
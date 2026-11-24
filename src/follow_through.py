"""Follow-through reminders for Hot Leads (#54): nag us, never the prospect.

Pure functions only: no Sheets, IMAP or Gmail. `cmd_remind` in main.py reads
the tabs and sends the email; everything that decides *what* to remind about
lives here, so the sponsorships bot can reuse it for its own digest.

Rules, in business days (weekends and config/blackout_dates.yaml skipped):
  Reply owed    the prospect wrote last in the thread, more than N days ago
  Unowned       no `owner` more than 1 day after the lead was flagged
  No next step  an `owner` but no `next_step` more than 3 days after flagging
  Due           `next_touch` is today or earlier
The same lead + reason is reminded about at most once every 2 business days.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")

REPLY_OWED = "Reply owed"
UNOWNED = "Unowned"
NO_NEXT_STEP = "No next step"
DUE = "Due"
REASONS = (REPLY_OWED, DUE, UNOWNED, NO_NEXT_STEP)  # digest order

REPLY_OWED_DAYS = 2
UNOWNED_DAYS = 1
NO_NEXT_STEP_DAYS = 3
REPEAT_AFTER_DAYS = 2

_HINTS = {
    REPLY_OWED: "They wrote last. Reply from the outreach account (or CC it) to clear this.",
    DUE: "The next_touch date has arrived. Do it, then move next_touch forward or clear it.",
    UNOWNED: "Put a name or email in the owner column.",
    NO_NEXT_STEP: "Fill in next_step (and next_touch if it has a date).",
}


def _as_date(value) -> date | None:
    """A Central-time calendar date from a date, datetime or ISO string."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(CENTRAL).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return _as_date(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def business_days_between(start, end, blackout_dates: Collection = ()) -> int:
    """Business days after `start` up to and including `end`."""
    start_date, end_date = _as_date(start), _as_date(end)
    if not start_date or not end_date or end_date <= start_date:
        return 0
    blackouts = {_as_date(d) for d in blackout_dates}
    count = 0
    day = start_date
    while day < end_date:
        day += timedelta(days=1)
        if day.weekday() < 5 and day not in blackouts:
            count += 1
    return count


def reminder_reasons(
    lead: dict,
    today: date,
    blackout_dates: Collection = (),
    reply_owed_days: int = REPLY_OWED_DAYS,
) -> list[str]:
    """Every rule that applies to one Hot Leads row.

    `lead["waiting_since"]` is when the prospect's message became the latest in
    the thread (blank once anyone answered); see `replies.waiting_since`.
    """
    def days_since(value) -> int:
        return business_days_between(value, today, blackout_dates)

    reasons = []
    owner = str(lead.get("owner") or "").strip()
    next_step = str(lead.get("next_step") or "").strip()
    flagged_at = lead.get("flagged_at")

    waiting = lead.get("waiting_since")
    if waiting and days_since(waiting) > reply_owed_days:
        reasons.append(REPLY_OWED)
    next_touch = _as_date(lead.get("next_touch"))
    if next_touch and next_touch <= today:
        reasons.append(DUE)
    if not owner and days_since(flagged_at) > UNOWNED_DAYS:
        reasons.append(UNOWNED)
    if owner and not next_step and days_since(flagged_at) > NO_NEXT_STEP_DAYS:
        reasons.append(NO_NEXT_STEP)
    return reasons


@dataclass(frozen=True)
class Reminder:
    email: str
    reason: str
    name: str = ""
    company: str = ""
    owner: str = ""
    next_step: str = ""
    next_touch: str = ""
    thread_link: str = ""


def _key(value) -> str:
    return str(value or "").strip().lower()


def due_reminders(
    hot_leads: Iterable[dict],
    replies: Iterable[dict],
    history: Iterable[dict],
    today: date,
    blackout_dates: Collection = (),
    reply_owed_days: int = REPLY_OWED_DAYS,
) -> list[Reminder]:
    """Reminders to send today, from the Hot Leads, Replies and Reminders tabs."""
    waiting = {
        _key(r.get("lead_email")): r.get("waiting_since")
        for r in replies
        if str(r.get("waiting_since") or "").strip()
    }
    last_sent: dict[tuple[str, str], date] = {}
    for row in history:
        sent = _as_date(row.get("sent_at"))
        key = (_key(row.get("lead_email")), str(row.get("reminder_type") or "").strip())
        if sent and (key not in last_sent or sent > last_sent[key]):
            last_sent[key] = sent

    out = []
    seen: set[str] = set()
    for lead in hot_leads:
        email = _key(lead.get("email"))
        if not email or email in seen:
            continue
        seen.add(email)
        row = {**lead, "waiting_since": waiting.get(email)}
        for reason in reminder_reasons(row, today, blackout_dates, reply_owed_days):
            previous = last_sent.get((email, reason))
            if previous and business_days_between(previous, today, blackout_dates) < REPEAT_AFTER_DAYS:
                continue
            out.append(Reminder(
                email=email,
                reason=reason,
                name=str(lead.get("name") or "").strip(),
                company=str(lead.get("company") or "").strip(),
                owner=str(lead.get("owner") or "").strip(),
                next_step=str(lead.get("next_step") or "").strip(),
                next_touch=str(lead.get("next_touch") or "").strip(),
                thread_link=str(lead.get("thread_link") or "").strip(),
            ))
    return out


def owner_address(owner: str) -> str | None:
    """The owner's email if the `owner` cell holds one, else None."""
    owner = owner.strip().lower()
    local, _, domain = owner.partition("@")
    return owner if local and "." in domain and " " not in owner else None


def by_owner(reminders: Iterable[Reminder]) -> dict[str, list[Reminder]]:
    """Reminders grouped by owner email, for owners who gave one."""
    groups: dict[str, list[Reminder]] = {}
    for r in reminders:
        address = owner_address(r.owner)
        if address:
            groups.setdefault(address, []).append(r)
    return groups


def digest_subject(reminders: list[Reminder]) -> str:
    leads = {r.email for r in reminders}
    return (f"Follow-through: {len(leads)} hot lead{'s' if len(leads) != 1 else ''} "
            "need attention")


def format_digest(reminders: list[Reminder], sheet_url: str) -> str:
    """One plain-text email body, grouped by reason."""
    sections = []
    for reason in REASONS:
        items = [r for r in reminders if r.reason == reason]
        if not items:
            continue
        lines = [f"{reason} ({len(items)})", f"  {_HINTS[reason]}"]
        for r in items:
            who = " — ".join(filter(None, [r.name or r.email, r.company]))
            lines.append(f"  • {who} <{r.email}>")
            details = [f"owner: {r.owner or 'none'}"]
            if r.next_step:
                details.append(f"next step: {r.next_step}")
            if r.next_touch:
                details.append(f"next touch: {r.next_touch}")
            lines.append("      " + "; ".join(details))
            if r.thread_link and reason == REPLY_OWED:
                lines.append(f"      Open in Gmail: {r.thread_link}")
        sections.append("\n".join(lines))
    return "\n\n".join([
        "Hot leads that need a nudge from us. Nothing here was sent to the prospects.",
        *sections,
        "Update owner / next_step / next_touch on the Hot Leads tab:\n"
        f"  {sheet_url}",
    ])

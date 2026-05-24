"""Daily summary email.

After `send` finishes, build a digest of yesterday's activity and email
it to DIGEST_RECIPIENT. The point is for the director to open ONE email
and see whether they need to act on anything.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from .gmail_send import GmailSender
from .models import LeadStatus
from .sheets import SheetClient

log = logging.getLogger(__name__)


def send_daily_summary(sent_count: int, replies: list, follow_ups: int, drafts_pending: int) -> None:
    sheets = SheetClient()
    leads_ws = sheets.book.worksheet("Leads")
    rows = leads_ws.get_all_records()

    hot = [r for r in rows if r.get("status") == LeadStatus.HOT.value]
    suppressed_today = [r for r in rows if r.get("status") == LeadStatus.SUPPRESSED.value]

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheets.sheet_id}/edit"
    lines = [
        f"CUBE Outreach — Daily Summary ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})",
        "",
        f"  Sent today:               {sent_count}",
        f"  New replies:              {len(replies)}",
        f"  Follow-ups drafted:       {follow_ups}",
        f"  Drafts awaiting approval: {drafts_pending}",
        "",
    ]

    positive_replies = [r for r in replies if r.classification.value == "positive"]
    if positive_replies:
        lines.append(f"HOT LEADS ({len(positive_replies)}) — they want to talk:")
        for r in positive_replies:
            lines.append(f"  • {r.from_email}")
            lines.append(f"    {r.snippet[:200]}")
        lines.append("")

    if hot:
        lines.append(f"All open hot leads ({len(hot)}):")
        for r in hot:
            lines.append(f"  • {r['name']} — {r['company']} <{r['email']}>")
        lines.append("")

    lines.append(f"Drafts tab (approve drafts before 10am CT to include in next send):")
    lines.append(f"  {sheet_url}")

    body = "\n".join(lines)

    sender = GmailSender(send_interval_seconds=0)
    recipient = os.environ.get("DIGEST_RECIPIENT", os.environ["IMPERSONATE_EMAIL"])
    sender.send(
        to=recipient,
        subject=f"CUBE Outreach Summary — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        body=body,
        dry_run=bool(int(os.environ.get("DRY_RUN", "0"))),
    )


def send_prepare_digest(drafts_count: int) -> None:
    """Emailed at the end of `prepare` so director knows new drafts are ready."""
    sheets = SheetClient()
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheets.sheet_id}/edit"
    body = (
        f"Good morning — {drafts_count} new outreach drafts are ready for review.\n\n"
        f"Open the Drafts tab, edit anything you want changed, then tick the "
        f"`approved` checkbox on each row you want sent. The send job runs at "
        f"10am CT and will send everything marked approved.\n\n"
        f"Sheet: {sheet_url}\n"
    )
    sender = GmailSender(send_interval_seconds=0)
    recipient = os.environ.get("DIGEST_RECIPIENT", os.environ["IMPERSONATE_EMAIL"])
    sender.send(
        to=recipient,
        subject=f"CUBE Outreach — {drafts_count} drafts ready for approval",
        body=body,
        dry_run=bool(int(os.environ.get("DRY_RUN", "0"))),
    )

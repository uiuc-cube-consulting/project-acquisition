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
    recipient = _approver_recipient()
    sender.send(
        to=recipient,
        subject=f"CUBE Outreach Summary — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        body=body,
        dry_run=bool(int(os.environ.get("DRY_RUN", "0"))),
    )


def _approver_recipient() -> str:
    return (
        os.environ.get("APPROVER_EMAIL")
        or os.environ.get("DIGEST_RECIPIENT")
        or os.environ["IMPERSONATE_EMAIL"]
    )


def send_prepare_digest(items: list[dict]) -> None:
    """Email the approver every draft inline so they can approve by replying.

    `items` is a list of dicts with keys: n, drafts_row, lead_email, subject,
    body, is_follow_up. The reply gate (see src/approvals.py) reads the reply to
    this email and flips the matching Drafts rows to approved.
    """
    sheets = SheetClient()
    recipient = _approver_recipient()
    n = len(items)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not items:
        body = (
            "Good morning — no new outreach drafts were generated today, so "
            "there's nothing to approve.\n"
        )
        GmailSender(send_interval_seconds=0).send(
            to=recipient,
            subject=f"CUBE Outreach — nothing to approve ({today})",
            body=body,
            dry_run=bool(int(os.environ.get("DRY_RUN", "0"))),
        )
        return

    lines = [
        f"Good morning — {n} outreach draft{'s' if n != 1 else ''} ready for your review.",
        "",
        "Reply to THIS email to approve. Examples:",
        "  • approve all",
        "  • approve 1, 3, 5",
        "  • skip 2          (sends everything except 2)",
        "  • none            (sends nothing today)",
        "",
        "Whatever you approve goes out automatically at 10am CT. No spreadsheet needed.",
        "=" * 64,
        "",
    ]
    for it in items:
        tag = "  [FOLLOW-UP]" if it.get("is_follow_up") else ""
        lines.append(f"{it['n']}) {it['lead_email']}{tag}")
        lines.append(f"   Subject: {it['subject']}")
        lines.append("")
        lines.extend(f"   {bl}" for bl in it["body"].splitlines())
        lines.append("")
        lines.append("-" * 64)
        lines.append("")
    body = "\n".join(lines)

    sender = GmailSender(send_interval_seconds=0)
    dry = bool(int(os.environ.get("DRY_RUN", "0")))
    msg_id, thread_id = sender.send(
        to=recipient,
        subject=f"CUBE Outreach — {n} draft{'s' if n != 1 else ''} ready, reply to approve ({today})",
        body=body,
        dry_run=dry,
    )
    if not dry:
        sheets.record_digest(
            thread_id=thread_id,
            message_id=msg_id,
            items=[
                {
                    "n": it["n"],
                    "drafts_row": it["drafts_row"],
                    "lead_email": it["lead_email"],
                    "subject": it["subject"],
                }
                for it in items
            ],
        )

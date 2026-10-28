"""Daily summary email (send-only).

After `send` finishes, email a short recap of what went out to APPROVER_EMAIL
(falls back to the sending account). Approval itself happens in the Sheet.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from .env import env_flag
from .gmail_send import GmailSender
from .sheets import SheetClient

log = logging.getLogger(__name__)


def _recipient() -> str:
    return (
        os.environ.get("APPROVER_EMAIL")
        or os.environ.get("DIGEST_RECIPIENT")
        or os.environ["GMAIL_ADDRESS"]
    )


def send_daily_summary(sent_count: int, follow_ups: int, drafts_pending: int) -> None:
    sheets = SheetClient()
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheets.sheet_id}/edit"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    body = "\n".join([
        f"CUBE Outreach — Daily Summary ({today})",
        "",
        f"  Sent today:                    {sent_count}",
        f"  Follow-ups sent:               {follow_ups}",
        f"  Drafts approved + still pending: {drafts_pending}",
        "",
        "Review drafts and set 'approved' to yes on the ones to send next:",
        f"  {sheet_url}",
    ])
    GmailSender(send_interval_seconds=0).send(
        to=_recipient(),
        subject=f"CUBE Outreach Summary — {today}",
        body=body,
        dry_run=env_flag("DRY_RUN"),
    )


def send_hot_lead_alert(leads: list[dict], sheet_id: str, sender=None) -> None:
    """One email to DIGEST_RECIPIENT (falls back to the sending account) listing
    every newly flagged hot lead."""
    first = leads[0]
    subject = (
        f"Hot lead: {first['name'] or first['email']} wants to talk"
        if len(leads) == 1 else f"{len(leads)} hot leads want to talk"
    )
    blocks = []
    for l in leads:
        lines = [" — ".join(filter(None, [l["name"] or l["email"], l["company"]])), f"  {l['email']}"]
        if l["reason"]:
            lines.append(f"  Why: {l['reason']}")
        lines.append(f'  "{l["reply_excerpt"]}"')
        if l["thread_link"]:
            lines.append(f"  Open in Gmail: {l['thread_link']}")
        blocks.append("\n".join(lines))
    body = "\n\n".join([
        "New positive replies. Someone should answer these today.",
        *blocks,
        "Put an owner and next step on the Hot Leads tab:\n"
        f"  https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
    ])
    (sender or GmailSender(send_interval_seconds=0)).send(
        to=os.environ.get("DIGEST_RECIPIENT") or os.environ["GMAIL_ADDRESS"],
        subject=subject,
        body=body,
    )

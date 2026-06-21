"""Daily summary email (send-only).

After `send` finishes, email a short recap of what went out to APPROVER_EMAIL
(falls back to the sending account). Approval itself happens in the Sheet.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

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
        dry_run=bool(int(os.environ.get("DRY_RUN", "0"))),
    )

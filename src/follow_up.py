"""Follow-up scheduler.

For every lead sent >= 3 business days ago with no reply yet (and no prior
follow-up), draft a short follow-up email that threads onto the original.
Director still has to approve the follow-up row in the Drafts tab before
it sends — same gate as initial outreach.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from .draft import Drafter, make_footer
from .models import Draft, Lead, LeadStatus
from .sheets import SheetClient

log = logging.getLogger(__name__)


def prepare_follow_ups(sender_name: str, business_days: int = 3) -> list[tuple[int, Draft]]:
    """Draft follow-ups due today. Returns (drafts_row, Draft) for each new row."""
    sheets = SheetClient()
    pending = sheets.list_awaiting_follow_up(business_days=business_days)
    if not pending:
        log.info("No follow-ups due today")
        return []

    drafter = Drafter()
    footer = make_footer()
    drafts_to_append = []
    for row in pending:
        lead = Lead(
            name=row["name"],
            email=row["email"],
            company=row["company"],
            title=row.get("title") or None,
            industry=row.get("industry") or None,
        )
        try:
            d = drafter.draft_follow_up(
                lead=lead,
                original_message_id=row.get("message_id") or "",
                sender_name=sender_name,
                footer=footer,
            )
        except Exception as exc:
            log.exception("Follow-up draft failed for %s: %s", row["email"], exc)
            continue
        drafts_to_append.append(d)

    rows: list[int] = []
    if drafts_to_append:
        rows = sheets.append_drafts(drafts_to_append)
        # Mark lead.last_follow_up_at = now so we don't redraft tomorrow even
        # if the approver hasn't approved the row yet.
        now = datetime.now(timezone.utc)
        for d in drafts_to_append:
            sheets.update_lead_status(
                d.lead_email,
                LeadStatus.FOLLOWED_UP,
                last_follow_up_at=now,
            )

    log.info("Prepared %d follow-up drafts", len(drafts_to_append))
    return list(zip(rows, drafts_to_append))

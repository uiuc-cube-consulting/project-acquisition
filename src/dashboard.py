"""Stats dashboard.

Computes pipeline metrics from the Leads + Drafts tabs and writes a readable
`Dashboard` tab in the same Sheet. Run via `python -m src.main stats`; also
refreshed automatically at the end of `prepare` and `send`.

Response metrics come from the Leads `status` column (replied / hot / closed).
Since the pipeline is send-only, update a lead's status when someone replies
(or run the optional inbox checker) for those numbers to populate.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

import gspread

from .models import LeadStatus
from .sheets import SheetClient, _truthy

log = logging.getLogger(__name__)

# Statuses that indicate the recipient responded in some way.
RESPONSE_STATUSES = {LeadStatus.REPLIED.value, LeadStatus.HOT.value, LeadStatus.CLOSED.value}


def _week(iso_ts: str) -> str:
    """ISO date string -> 'YYYY-Www' (the Monday-based ISO week)."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def compute_stats(sheets: SheetClient) -> dict:
    leads = sheets.book.worksheet("Leads").get_all_records()
    drafts = sheets.book.worksheet("Drafts").get_all_records()
    try:
        suppression = len(sheets.book.worksheet("Suppression").get_all_records())
    except gspread.WorksheetNotFound:
        suppression = 0

    email_alum = {
        str(l.get("email", "")).strip().lower(): _truthy(l.get("is_uiuc_alum"))
        for l in leads if l.get("email")
    }

    sent_drafts = [d for d in drafts if str(d.get("sent_at", "")).strip()]
    approved = [d for d in drafts if _truthy(d.get("approved"))]
    sent_alumni = sum(1 for d in sent_drafts if email_alum.get(str(d.get("lead_email", "")).strip().lower()))

    by_status = Counter(str(l.get("status", "")).strip() or "(blank)" for l in leads)
    by_source = Counter(str(l.get("source", "")).strip() or "(blank)" for l in leads)
    sent_by_week = Counter(w for d in sent_drafts if (w := _week(str(d.get("sent_at", "")))))

    alumni = sum(1 for l in leads if _truthy(l.get("is_uiuc_alum")))
    responses = sum(1 for l in leads if str(l.get("status", "")).strip() in RESPONSE_STATUSES)
    hot = by_status.get(LeadStatus.HOT.value, 0)
    sent_count = len(sent_drafts)

    return {
        "leads_total": len(leads),
        "drafts_total": len(drafts),
        "approved": len(approved),
        "sent": sent_count,
        "pending_approval": sum(1 for d in approved if not str(d.get("sent_at", "")).strip()),
        "send_errors": sum(1 for d in drafts if str(d.get("send_error", "")).strip()),
        "alumni": alumni,
        "non_alumni": len(leads) - alumni,
        "sent_alumni": sent_alumni,
        "sent_non_alumni": sent_count - sent_alumni,
        "responses": responses,
        "hot": hot,
        "response_rate": (responses / sent_count) if sent_count else 0.0,
        "alumni_response_rate": None,  # filled below if we can compute it
        "suppression": suppression,
        "by_status": by_status.most_common(),
        "by_source": by_source.most_common(),
        "sent_by_week": sorted(sent_by_week.items()),
    }


def write_dashboard(sheets: SheetClient) -> None:
    s = compute_stats(sheets)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pct = f"{s['response_rate'] * 100:.0f}%"

    rows: list[list] = [
        ["CUBE Outreach — Dashboard", f"updated {now}"],
        [],
        ["FUNNEL", ""],
        ["Leads sourced", s["leads_total"]],
        ["Drafts created", s["drafts_total"]],
        ["Approved", s["approved"]],
        ["Sent", s["sent"]],
        ["Pending approval (approved, unsent)", s["pending_approval"]],
        ["Send errors", s["send_errors"]],
        ["Suppressed (do-not-contact)", s["suppression"]],
        [],
        ["ALUMNI", ""],
        ["UIUC alumni (leads)", s["alumni"]],
        ["Non-alumni (leads)", s["non_alumni"]],
        ["Sent to alumni", s["sent_alumni"]],
        ["Sent to non-alumni", s["sent_non_alumni"]],
        [],
        ["RESPONSES  (from Leads status — set status=replied/hot when they reply)", ""],
        ["Responses (replied + hot + closed)", s["responses"]],
        ["Hot leads", s["hot"]],
        ["Response rate (of sent)", pct],
        [],
        ["BY SOURCE", "leads"],
        *[[src, n] for src, n in s["by_source"]],
        [],
        ["BY STATUS", "leads"],
        *[[st, n] for st, n in s["by_status"]],
        [],
        ["SENT BY WEEK", "count"],
        *([[wk, n] for wk, n in s["sent_by_week"]] or [["(none yet)", 0]]),
    ]

    try:
        ws = sheets.book.worksheet("Dashboard")
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sheets.book.add_worksheet(title="Dashboard", rows=max(60, len(rows) + 10), cols=4)

    ws.update(range_name="A1", values=rows)
    log.info("Dashboard refreshed: %d sourced, %d sent, %d alumni, %s response rate",
             s["leads_total"], s["sent"], s["alumni"], pct)

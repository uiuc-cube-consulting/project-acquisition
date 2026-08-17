"""Stats dashboard — the `Dashboard` tab inside the Sheet.

A plain-text mirror of the numbers in `metrics.py`, written into the workbook so
anyone with the Sheet open can see how outreach is doing without opening
anything else. Refreshed automatically at the end of `prepare`, `send` and
`replies`, or on demand with `python -m src.main stats`.

For the shareable version — charts, trends, the whole story in one file you can
email — build the standalone page instead: `python -m src.main report`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import gspread

from .metrics import collect

log = logging.getLogger(__name__)


def _pct(value: float | None) -> str:
    """Percentages that aren't measurable read as '—', never as 0%."""
    return "—" if value is None else f"{value * 100:.1f}%"


def write_dashboard(sheets) -> None:
    m = collect(sheets)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    h, r, d, a, q, rw = (
        m["headline"], m["reply"], m["deliverability"],
        m["apollo"], m["quality"], m["runway"],
    )

    rows: list[list] = [
        ["CUBE Outreach — Dashboard", f"updated {now}"],
        ["", f"{m['window']['first_send'] or '—'} to {m['window']['last_send'] or '—'}"],
        [],
        ["HEADLINE", ""],
        ["Emails sent", h["emails_sent"]],
        ["People reached", h["people_reached"]],
        ["Companies reached", h["companies_reached"]],
        ["Sent in the last 7 days", h["last_7"]],
        ["Avg per sending day", h["avg_per_active_day"]],
        [],
        ["RESPONSES  (from the inbox scan — run `replies` to refresh)", ""],
        ["Replies from a human", r["responses"]],
        ["Reply rate (of delivered)", _pct(r["rate"])],
        ["Interested replies", r["interested"]],
        ["Interested rate (of delivered)", _pct(r["interested_rate"])],
        ["Auto-replies / out-of-office", r["auto_replies"]],
        ["Engagement rate (replied or auto-replied)", _pct(r["engagement_rate"])],
        *[[f"  reply sentiment — {s['label']}", s["count"]] for s in r["sentiment"]],
        [],
        ["DELIVERABILITY", ""],
        ["Delivered", d["delivered"]],
        ["Bounced (bad address)", d["bounced"]],
        ["Bounce rate", _pct(d["bounce_rate"])],
        [],
        ["SOURCING (Apollo)", ""],
        ["Email lookups attempted", a["attempted"]],
        ["Emails found", a["found"]],
        ["Find rate", _pct(a["find_rate"])],
        ["Usable rate (found AND deliverable)", _pct(a["usable_rate"])],
        [],
        ["FUNNEL", "count"],
        *[[f["stage"], f["count"]] for f in m["funnel"]],
        [],
        ["PIPELINE HEALTH", ""],
        ["Alumni bench left (contactable)", rw["projected_contacts"]],
        ["  with an email ready", rw["bench_ready"]],
        ["  still to look up", rw["bench_unlooked"]],
        ["Business days of runway", rw["business_days_left"] if rw["business_days_left"] is not None else "—"],
        ["Approved, not yet sent", rw["approved_unsent"]],
        ["Awaiting approval", rw["awaiting_approval"]],
        ["Send success rate", _pct(q["send_success_rate"])],
        ["Failed sends", q["failed_sends"]],
        ["Suppressed (do-not-contact)", q["suppressed"]],
        [],
        ["BY SOURCE", "sent", "delivered", "bounced", "replies", "reply rate"],
        *[
            [s["label"], s["sent"], s["delivered"], s["bounced"], s["replies"], _pct(s["reply_rate"])]
            for s in r["by_source"]
        ],
        [],
        ["BY AUDIENCE", "sent", "delivered", "bounced", "replies", "reply rate"],
        *[
            [s["label"], s["sent"], s["delivered"], s["bounced"], s["replies"], _pct(s["reply_rate"])]
            for s in r["by_audience"]
        ],
        [],
        ["SENT BY WEEK", "sent", "drafted"],
        *([[w["week_start"], w["sent"], w["drafted"]] for w in m["timeline"]["weekly"]]
          or [["(none yet)", 0, 0]]),
    ]

    try:
        ws = sheets.book.worksheet("Dashboard")
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sheets.book.add_worksheet(title="Dashboard", rows=max(80, len(rows) + 10), cols=6)

    ws.update(range_name="A1", values=rows)
    log.info(
        "Dashboard refreshed: %d sent, %s reply rate, %s delivered, %s Apollo find rate",
        h["emails_sent"], _pct(r["rate"]), _pct(d["delivered_rate"]), _pct(a["find_rate"]),
    )

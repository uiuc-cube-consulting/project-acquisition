"""Google Sheets data layer.

One workbook (env: SHEET_ID) with four tabs:

  Leads        — every contact we've ever pulled in, with status
  Drafts       — pending + sent drafts; the `approved` checkbox is the gate
  Hot Leads    — auto-populated when a reply is classified positive
  Suppression  — emails we will never contact (unsubscribes, bounces, do-not-contact)

We keep one row per lead in `Leads` (deduped by lowercased email). Drafts
can have multiple rows per lead over time (initial + follow-up). Status
transitions live here, not in the source code, so the director can also
edit them by hand.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Iterable

import gspread
from google.oauth2.service_account import Credentials

from .models import Draft, Lead, LeadStatus

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

LEADS_HEADERS = [
    "date_added", "name", "title", "company", "email", "linkedin",
    "industry", "location", "company_stage", "is_uiuc_alum", "schools",
    "source", "score", "status", "sent_at", "replied_at",
    "last_follow_up_at", "thread_id", "message_id",
]

DRAFTS_HEADERS = [
    "prepared_at", "lead_email", "template_used", "subject", "body",
    "approved", "sent_at", "send_error", "message_id",
    "is_follow_up", "in_reply_to",
]

HOT_LEADS_HEADERS = [
    "flagged_at", "name", "company", "email", "linkedin",
    "reply_excerpt", "thread_link",
]

SUPPRESSION_HEADERS = ["email", "added_at", "reason"]

# Free, manually-curated lead source. The team pastes prospective-client rows
# here and `prepare` reads them — the no-cost stand-in for Apollo discovery.
# Only name + email are required; the rest improves personalization.
PROSPECTS_HEADERS = [
    "name", "title", "company", "email", "linkedin",
    "industry", "location", "is_uiuc_alum",
]

# UIUC-alumni input tab. Paste alumni from LinkedIn's Alumni tool — name +
# company is enough (email is looked up via Apollo if blank). Every row here is
# treated as a UIUC alum and ranked first.
ALUMNI_HEADERS = [
    "name", "company", "linkedin", "title", "industry", "location", "email", "cube_member",
]

# One row per `prepare` run. Records the Gmail thread the approval digest was
# sent on plus a JSON map of digest-number -> Drafts row, so the `send` job can
# resolve "approve 1,3" from the approver's reply back to the right rows.
APPROVALS_HEADERS = ["digest_at", "thread_id", "message_id", "items_json", "processed_at"]

TAB_HEADERS = {
    "Leads": LEADS_HEADERS,
    "Drafts": DRAFTS_HEADERS,
    "Hot Leads": HOT_LEADS_HEADERS,
    "Suppression": SUPPRESSION_HEADERS,
    "Prospects": PROSPECTS_HEADERS,
    "Alumni": ALUMNI_HEADERS,
    "Approvals": APPROVALS_HEADERS,
}


def load_service_account_info() -> dict:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        return json.loads(raw)
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    with open(path) as f:
        return json.load(f)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SheetClient:
    def __init__(
        self,
        service_account_info: dict | None = None,
        sheet_id: str | None = None,
    ) -> None:
        info = service_account_info or load_service_account_info()
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.sheet_id = sheet_id or os.environ["SHEET_ID"]
        self.book = self.gc.open_by_key(self.sheet_id)

    def bootstrap(self) -> None:
        """Create any missing tabs + set the header row."""
        existing = {ws.title for ws in self.book.worksheets()}
        for tab, headers in TAB_HEADERS.items():
            if tab not in existing:
                ws = self.book.add_worksheet(title=tab, rows=1000, cols=len(headers))
            else:
                ws = self.book.worksheet(tab)
            current_headers = ws.row_values(1)
            if current_headers != headers:
                ws.update("A1", [headers])
        # Drop "Sheet1" default if present
        if "Sheet1" in existing and len(existing) > 1:
            try:
                self.book.del_worksheet(self.book.worksheet("Sheet1"))
            except Exception:
                pass

    # ---------------- Prospects (free lead source) ----------------

    def fetch_prospect_leads(self) -> list[Lead]:
        """Read the 'Prospects' input tab into Lead records.

        A manually compiled list of prospective clients — the free stand-in for
        Apollo. Rows missing a name or a valid email are skipped. `is_uiuc_alum`
        accepts true/yes/1 (default false), so the drafter only adds the "fellow
        Illini" line when the row actually marks an alumnus.
        """
        try:
            ws = self.book.worksheet("Prospects")
        except gspread.WorksheetNotFound:
            log.info("No 'Prospects' tab found; skipping the manual lead source")
            return []

        now = datetime.now(timezone.utc)
        out: list[Lead] = []
        for row in ws.get_all_records():
            keyed = {str(k).lower().strip(): (v if v is not None else "") for k, v in row.items()}
            email = str(keyed.get("email", "")).strip()
            name = str(keyed.get("name", "")).strip()
            if not email or not name or "@" not in email:
                continue
            is_alum = str(keyed.get("is_uiuc_alum", "")).strip().lower() in ("true", "yes", "1", "y")
            out.append(Lead(
                name=name,
                title=str(keyed.get("title", "")).strip() or None,
                company=str(keyed.get("company", "")).strip() or "",
                email=email,
                linkedin=str(keyed.get("linkedin", "")).strip() or None,
                industry=str(keyed.get("industry", "")).strip() or None,
                location=str(keyed.get("location", "")).strip() or None,
                is_uiuc_alum=is_alum,
                schools=["University of Illinois Urbana-Champaign"] if is_alum else [],
                source="prospects_sheet",
                date_added=now,
            ))
        log.info("Loaded %d leads from the Prospects tab", len(out))
        return out

    def fetch_alumni_targets(self) -> tuple[list[Lead], list[dict]]:
        """Read the 'Alumni' input tab. Every row is treated as a UIUC alum.

        Returns (ready_leads, contacts_to_enrich): rows that already have an
        email become Leads immediately; rows with just name + company are
        returned as dicts for Apollo to look up the email.
        """
        try:
            ws = self.book.worksheet("Alumni")
        except gspread.WorksheetNotFound:
            log.info("No 'Alumni' tab found; skipping the alumni source")
            return [], []

        now = datetime.now(timezone.utc)
        leads: list[Lead] = []
        contacts: list[dict] = []
        skipped = 0
        # start=2: row 1 is the header, so records line up with sheet row numbers.
        for sheet_row, row in enumerate(ws.get_all_records(), start=2):
            keyed = {str(k).lower().strip(): (v if v is not None else "") for k, v in row.items()}
            name = str(keyed.get("name", "")).strip()
            if not name:
                continue
            company = str(keyed.get("company", "")).strip()
            common = dict(
                name=name,
                company=company,
                linkedin=str(keyed.get("linkedin", "")).strip() or None,
                title=str(keyed.get("title", "")).strip() or None,
                industry=str(keyed.get("industry", "")).strip() or None,
                location=str(keyed.get("location", "")).strip() or None,
                is_cube_member=_truthy(keyed.get("cube_member")),
            )
            email = str(keyed.get("email", "")).strip()
            if email and "@" in email:
                leads.append(Lead(
                    **common,
                    email=email,
                    is_uiuc_alum=True,
                    schools=["University of Illinois Urbana-Champaign"],
                    source="alumni_input",
                    date_added=now,
                ))
            elif email:
                # Non-email marker (e.g. NOT_FOUND) written back on a prior run —
                # already looked up and unresolvable, so don't spend a credit again.
                skipped += 1
            elif company:  # name + company: look up the email (record the row to write back)
                contacts.append({**common, "_row": sheet_row})
        log.info("Alumni tab: %d with email, %d to look up, %d already-processed (skipped)",
                 len(leads), len(contacts), skipped)
        return leads, contacts

    NOT_FOUND_MARKER = "NOT_FOUND"

    def set_alumni_email(self, row_index: int, value: str) -> None:
        """Write the resolved email (or NOT_FOUND) back to an Alumni row so it's
        not looked up again on the next run."""
        ws = self.book.worksheet("Alumni")
        ws.update_cell(row_index, ALUMNI_HEADERS.index("email") + 1, value)

    # ---------------- Leads ----------------

    def get_known_emails(self) -> set[str]:
        ws = self.book.worksheet("Leads")
        col = ws.col_values(LEADS_HEADERS.index("email") + 1)[1:]
        return {e.strip().lower() for e in col if e.strip()}

    def get_known_linkedins(self) -> set[str]:
        """LinkedIn URLs already in the pipeline — used to dedup Apollo
        candidates *before* spending a credit to reveal their email."""
        ws = self.book.worksheet("Leads")
        col = ws.col_values(LEADS_HEADERS.index("linkedin") + 1)[1:]
        return {u.strip().lower() for u in col if u.strip()}

    def get_contacted_dates(self) -> dict[str, datetime]:
        ws = self.book.worksheet("Leads")
        rows = ws.get_all_records()
        out: dict[str, datetime] = {}
        for r in rows:
            sent = r.get("sent_at")
            email = (r.get("email") or "").strip().lower()
            if sent and email:
                try:
                    out[email] = datetime.fromisoformat(sent.replace("Z", "+00:00"))
                except ValueError:
                    pass
        return out

    def get_suppression_emails(self) -> set[str]:
        ws = self.book.worksheet("Suppression")
        col = ws.col_values(1)[1:]
        return {e.strip().lower() for e in col if e.strip()}

    def append_leads(self, leads: Iterable[Lead]) -> int:
        ws = self.book.worksheet("Leads")
        rows = [_lead_to_row(l) for l in leads]
        if not rows:
            return 0
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        return len(rows)

    def update_lead_status(
        self,
        email: str,
        status: LeadStatus,
        **fields,
    ) -> None:
        ws = self.book.worksheet("Leads")
        records = ws.get_all_records()
        for i, row in enumerate(records, start=2):  # row 1 is header
            if (row.get("email") or "").strip().lower() == email.lower():
                updates = {"status": status.value, **fields}
                for k, v in updates.items():
                    if k not in LEADS_HEADERS:
                        continue
                    col_idx = LEADS_HEADERS.index(k) + 1
                    if isinstance(v, datetime):
                        v = v.isoformat(timespec="seconds")
                    ws.update_cell(i, col_idx, v if v is not None else "")
                return

    # ---------------- Drafts ----------------

    def append_drafts(self, drafts: Iterable[Draft]) -> list[int]:
        """Append drafts and return the Sheet row index of each new row."""
        ws = self.book.worksheet("Drafts")
        rows = [_draft_to_row(d) for d in drafts]
        if not rows:
            return []
        resp = ws.append_rows(rows, value_input_option="USER_ENTERED")
        updated_range = (resp or {}).get("updates", {}).get("updatedRange", "")
        start = _parse_start_row(updated_range)
        if start is None:
            # Fallback: assume they landed at the current tail of the sheet.
            start = len(ws.col_values(1)) - len(rows) + 1
        return list(range(start, start + len(rows)))

    def get_sent_emails(self) -> set[str]:
        """Emails already contacted (a send recorded in Drafts or Leads) — used to
        guard against re-emailing someone via a duplicate approved draft."""
        out: set[str] = set()
        for d in self.book.worksheet("Drafts").get_all_records():
            if str(d.get("sent_at", "")).strip() and d.get("lead_email"):
                out.add(str(d["lead_email"]).strip().lower())
        for lead in self.book.worksheet("Leads").get_all_records():
            if str(lead.get("sent_at", "")).strip() and lead.get("email"):
                out.add(str(lead["email"]).strip().lower())
        return out

    def list_approved_pending(self) -> list[tuple[int, Draft]]:
        """Returns (sheet_row_index, draft) for unsent, approved rows."""
        ws = self.book.worksheet("Drafts")
        records = ws.get_all_records()
        out: list[tuple[int, Draft]] = []
        for i, row in enumerate(records, start=2):
            if _truthy(row.get("approved")) and not row.get("sent_at"):
                out.append((i, _row_to_draft(row)))
        return out

    def mark_draft_sent(self, row_index: int, message_id: str) -> None:
        ws = self.book.worksheet("Drafts")
        ws.update_cell(row_index, DRAFTS_HEADERS.index("sent_at") + 1, _now_iso())
        ws.update_cell(row_index, DRAFTS_HEADERS.index("message_id") + 1, message_id)

    def mark_draft_error(self, row_index: int, error: str) -> None:
        ws = self.book.worksheet("Drafts")
        ws.update_cell(row_index, DRAFTS_HEADERS.index("send_error") + 1, error[:500])

    def approve_draft_rows(self, row_indices: Iterable[int]) -> int:
        """Flip the `approved` cell to TRUE for the given Drafts rows."""
        ws = self.book.worksheet("Drafts")
        col = DRAFTS_HEADERS.index("approved") + 1
        count = 0
        for ri in row_indices:
            ws.update_cell(ri, col, "TRUE")
            count += 1
        return count

    # ---------------- Approvals (email reply gate) ----------------

    def record_digest(self, thread_id: str, message_id: str, items: list[dict]) -> None:
        """Persist the digest thread + number->row map for the send job to read."""
        ws = self.book.worksheet("Approvals")
        ws.append_row(
            [_now_iso(), thread_id or "", message_id or "", json.dumps(items), ""],
            value_input_option="RAW",
        )

    def latest_unprocessed_digest(self) -> tuple[int, dict] | None:
        """Most recent Approvals row that hasn't been processed yet, with its row index."""
        ws = self.book.worksheet("Approvals")
        records = ws.get_all_records()
        for i in range(len(records) - 1, -1, -1):
            if not str(records[i].get("processed_at") or "").strip():
                return i + 2, records[i]  # +2: header row + 1-based
        return None

    def mark_digest_processed(self, row_index: int) -> None:
        ws = self.book.worksheet("Approvals")
        ws.update_cell(row_index, APPROVALS_HEADERS.index("processed_at") + 1, _now_iso())

    def list_awaiting_follow_up(self, business_days: int = 3) -> list[dict]:
        """Leads sent >= N business days ago, not yet replied, not yet followed up."""
        ws = self.book.worksheet("Leads")
        rows = ws.get_all_records()
        out = []
        now = datetime.now(timezone.utc)
        for r in rows:
            if r.get("status") != LeadStatus.SENT.value:
                continue
            if r.get("last_follow_up_at"):
                continue
            sent = r.get("sent_at")
            if not sent:
                continue
            try:
                sent_dt = datetime.fromisoformat(sent.replace("Z", "+00:00"))
            except ValueError:
                continue
            if _business_days_between(sent_dt, now) >= business_days:
                out.append(r)
        return out

    # ---------------- Hot leads / Suppression ----------------

    def append_hot_lead(
        self,
        name: str,
        company: str,
        email: str,
        linkedin: str | None,
        reply_excerpt: str,
        thread_link: str,
    ) -> None:
        ws = self.book.worksheet("Hot Leads")
        ws.append_row(
            [_now_iso(), name, company, email, linkedin or "", reply_excerpt[:500], thread_link],
            value_input_option="USER_ENTERED",
        )

    def add_suppression(self, email: str, reason: str) -> None:
        ws = self.book.worksheet("Suppression")
        existing = {e.strip().lower() for e in ws.col_values(1)[1:]}
        if email.lower() in existing:
            return
        ws.append_row([email.lower(), _now_iso(), reason], value_input_option="USER_ENTERED")


# --------- helpers ---------

def _lead_to_row(l: Lead) -> list:
    return [
        (l.date_added or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        l.name, l.title or "", l.company, l.email, l.linkedin or "",
        l.industry or "", l.location or "", l.company_stage or "",
        "TRUE" if l.is_uiuc_alum else "FALSE",
        "; ".join(l.schools), l.source, round(l.score, 2),
        l.status.value,
        l.sent_at.isoformat(timespec="seconds") if l.sent_at else "",
        l.replied_at.isoformat(timespec="seconds") if l.replied_at else "",
        l.last_follow_up_at.isoformat(timespec="seconds") if l.last_follow_up_at else "",
        l.thread_id or "", l.message_id or "",
    ]


def _draft_to_row(d: Draft) -> list:
    return [
        d.prepared_at.isoformat(timespec="seconds"),
        d.lead_email, d.template_used.value, d.subject, d.body,
        "FALSE",  # approved: starts unchecked
        d.sent_at.isoformat(timespec="seconds") if d.sent_at else "",
        d.send_error or "", d.message_id or "",
        "TRUE" if d.is_follow_up else "FALSE",
        d.in_reply_to or "",
    ]


def _row_to_draft(row: dict) -> Draft:
    from .models import TemplateType
    return Draft(
        lead_email=row["lead_email"],
        prepared_at=datetime.fromisoformat(str(row["prepared_at"]).replace("Z", "+00:00")),
        template_used=TemplateType(row["template_used"]),
        subject=row["subject"],
        body=row["body"],
        approved=_truthy(row.get("approved")),
        is_follow_up=_truthy(row.get("is_follow_up")),
        in_reply_to=row.get("in_reply_to") or None,
        message_id=row.get("message_id") or None,
    )


def _parse_start_row(updated_range: str) -> int | None:
    """Pull the first row number out of an A1 range like 'Drafts!A10:K12'."""
    import re

    m = re.search(r"![A-Z]+(\d+)", updated_range or "")
    return int(m.group(1)) if m else None


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("true", "yes", "y", "1", "✓", "checked")


def _business_days_between(a: datetime, b: datetime) -> int:
    days = 0
    cur = a.date()
    end = b.date()
    while cur < end:
        cur = cur.fromordinal(cur.toordinal() + 1)
        if cur.weekday() < 5:
            days += 1
    return days

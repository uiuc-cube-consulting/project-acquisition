"""Orchestrator CLI.

Two commands wired into separate GitHub Actions workflows:

  prepare  — 06:00 CT M-F
      1. Source new leads (free Prospects + CUBE alumni Sheets; Apollo optional)
      2. Dedup against existing Leads + suppression list
      3. Score and keep the top DAILY_PREPARE_TARGET (default 15)
      4. Draft personalized emails via Gemini
      5. Write Leads + Drafts to the Sheet
      6. Prepare follow-up drafts for leads sent 3 business days ago
      7. Email the approver (APPROVER_EMAIL) a numbered list of every draft,
         inline. They approve by replying ("approve all", "approve 1,3", ...).

  send     — 10:00 CT M-F
      1. Read the approver's reply to the digest; flip approved Drafts rows
      2. Read approved-but-unsent rows from Drafts
      3. Send each via Gmail, throttled (max DAILY_SEND_CAP)
      4. Update Leads.status -> sent, write thread/message IDs
      5. Poll Gmail for replies on prior threads; classify; route to
         Hot Leads / Suppression / closed as appropriate
      6. Email the approver a daily summary

Both commands accept --dry-run for safe local smoke testing.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from .draft import draft_for_leads
from .follow_up import prepare_follow_ups
from .models import Lead, LeadStatus
from .past_projects import PastProjectIndex
from .reply_check import check_replies
from .scoring import Scorer
from .sheets import SheetClient
from .sourcing.pdl import (
    PDLClient, get_uiuc_profile, load_profiles, pick_profile_for_today, search_leads,
)
from .sourcing.cube_alumni import fetch_alumni_leads
from .summary import send_daily_summary, send_prepare_digest
from .template import TemplateRouter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("cube.main")


def _sender_identity() -> tuple[str, str]:
    return (
        os.environ.get("SENDER_NAME", "Raghav Taneja"),
        os.environ.get("SENDER_PHONE", "—"),
    )


# ---------------- prepare ----------------

def _filter_fresh(leads, known, known_li, suppression, contacted, scorer) -> list[Lead]:
    """Drop leads already in the pipeline (by email or LinkedIn), duplicates
    within this batch, suppressed addresses, and recently-contacted leads."""
    out: list[Lead] = []
    seen: set[str] = set()
    for lead in leads:
        email_lc = lead.email.lower()
        if email_lc in seen:
            continue
        li = (lead.linkedin or "").strip().lower()
        if email_lc in known or (li and li in known_li):
            continue
        excluded, reason = scorer.is_excluded(lead, suppression, contacted)
        if excluded:
            log.info("Skipping %s: %s", email_lc, reason)
            continue
        seen.add(email_lc)
        out.append(lead)
    return out


def cmd_prepare(dry_run: bool) -> int:
    target = int(os.environ.get("DAILY_PREPARE_TARGET", "15"))
    sheets = SheetClient()
    sheets.bootstrap()

    suppression = sheets.get_suppression_emails()
    known = sheets.get_known_emails()
    known_li = sheets.get_known_linkedins()
    contacted = sheets.get_contacted_dates()
    scorer = Scorer()
    past_index = PastProjectIndex.load()
    past_kw = {kw.lower() for p in past_index.projects for kw in p.keywords}

    # 1) Source. Every source returns ready-to-use Leads (emails included). PDL
    #    bills per record returned, so we fetch UIUC alumni first and only run the
    #    breadth search if alumni + Sheet sources don't already fill the target.
    sheet_leads: list[Lead] = []
    pdl_leads: list[Lead] = []
    if dry_run:
        log.info("[DRY RUN] skipping live sourcing — using fixtures")
        sheet_leads = _dry_run_fixture_leads()
    else:
        # Free sources: the manually curated Prospects tab plus the CUBE alumni
        # Sheet when ALUMNI_SHEET_ID is set.
        sheet_leads.extend(sheets.fetch_prospect_leads())
        from .sheets import load_service_account_info
        sheet_leads.extend(fetch_alumni_leads(load_service_account_info()))
        # PDL is optional — enabled only when PDL_API_KEY is set.
        if os.environ.get("PDL_API_KEY"):
            client = PDLClient()
            profiles = load_profiles()
            day_index = datetime.now(timezone.utc).timetuple().tm_yday
            # Primary: UIUC alumni, our highest-converting segment, every day.
            uiuc_profile = get_uiuc_profile(profiles)
            if uiuc_profile:
                log.info("PDL UIUC alumni search: %s", uiuc_profile["name"])
                pdl_leads.extend(search_leads(client, uiuc_profile, size=target + 5))
            # Breadth search runs ONLY if alumni + Sheet leads don't fill target,
            # and only fetches the gap — keeps PDL credit spend close to `target`.
            have = len(_filter_fresh(sheet_leads + pdl_leads, known, known_li, suppression, contacted, scorer))
            if have < target:
                secondary = pick_profile_for_today(profiles, day_index)
                log.info("PDL breadth search: %s (need %d more)", secondary["name"], target - have)
                pdl_leads.extend(search_leads(client, secondary, size=(target - have) + 5))
        else:
            log.info("PDL_API_KEY not set — sourcing from the free Sheet sources only")

    # 2) Dedup + filter (LinkedIn + email + suppression + recency).
    fresh = _filter_fresh(sheet_leads + pdl_leads, known, known_li, suppression, contacted, scorer)
    log.info("After dedup/filter: %d fresh leads", len(fresh))

    # 3) Score + rank alumni-first, then take the top `target`.
    for lead in fresh:
        lead.score = scorer.score(lead, past_kw)
    fresh.sort(key=lambda l: (l.is_uiuc_alum, l.score), reverse=True)
    top: list[Lead] = fresh[:target]
    log.info("Selected %d leads for drafting (alumni-first)", len(top))

    sender_name, sender_phone = _sender_identity()

    # 4) Draft
    pairs = []
    if top:
        router = TemplateRouter()
        pairs = draft_for_leads(top, router, past_index, sender_name, sender_phone)
        log.info("Generated %d drafts", len(pairs))
    else:
        log.info("No new leads to draft today (follow-ups may still be due)")

    if dry_run:
        for lead, draft in pairs[:3]:
            print(f"\n---\nTO: {lead.email}\nSUBJECT: {draft.subject}\n\n{draft.body}\n")
        return 0

    # 5) Write to Sheet
    leads_to_write = []
    drafts_to_write = []
    for lead, draft in pairs:
        lead.status = LeadStatus.DRAFTED
        leads_to_write.append(lead)
        drafts_to_write.append(draft)
    sheets.append_leads(leads_to_write)
    draft_rows = sheets.append_drafts(drafts_to_write)

    # 6) Follow-ups for older sends
    follow_ups = prepare_follow_ups(sender_name=sender_name)  # list[(row, Draft)]

    # 7) Build the numbered approval digest and email the approver. They reply to
    #    approve; the send job reads that reply and flips the matching rows.
    items: list[dict] = []
    for row_idx, draft in list(zip(draft_rows, drafts_to_write)) + follow_ups:
        items.append({
            "n": len(items) + 1,
            "drafts_row": row_idx,
            "lead_email": draft.lead_email,
            "subject": draft.subject,
            "body": draft.body,
            "is_follow_up": draft.is_follow_up,
        })
    send_prepare_digest(items)
    return 0


# ---------------- send ----------------

def cmd_send(dry_run: bool) -> int:
    cap = int(os.environ.get("DAILY_SEND_CAP", "10"))
    sheets = SheetClient()

    # Read the approver's emailed reply to this morning's digest and flip the
    # approved drafts before we look at what's approved + pending.
    from .approvals import apply_email_approvals
    approved_via_email = apply_email_approvals(dry_run=dry_run)
    log.info("Approved %d drafts from the emailed reply", approved_via_email)

    approved = sheets.list_approved_pending()
    log.info("%d drafts approved + pending", len(approved))

    from .gmail_send import GmailSender
    sender = GmailSender()

    sent_count = 0
    for row_idx, draft in approved[:cap]:
        # Look up the lead row so we can update status + record thread id
        try:
            msg_id, thread_id = sender.send(
                to=draft.lead_email,
                subject=draft.subject,
                body=draft.body,
                in_reply_to=draft.in_reply_to,
                dry_run=dry_run,
            )
        except Exception as exc:
            log.exception("Send failed for %s: %s", draft.lead_email, exc)
            sheets.mark_draft_error(row_idx, str(exc))
            continue

        if dry_run:
            log.info("[DRY RUN] would mark sent: %s", draft.lead_email)
        else:
            sheets.mark_draft_sent(row_idx, msg_id)
            now = datetime.now(timezone.utc)
            new_status = LeadStatus.FOLLOWED_UP if draft.is_follow_up else LeadStatus.SENT
            sheets.update_lead_status(
                draft.lead_email,
                new_status,
                sent_at=now if not draft.is_follow_up else None,
                last_follow_up_at=now if draft.is_follow_up else None,
                thread_id=thread_id,
                message_id=msg_id,
            )
        sent_count += 1

    # Check replies
    replies = check_replies(dry_run=dry_run)

    # Daily summary
    drafts_pending = len(sheets.list_approved_pending())  # remaining after this run
    send_daily_summary(
        sent_count=sent_count,
        replies=replies,
        follow_ups=sum(1 for _, d in approved[:cap] if d.is_follow_up),
        drafts_pending=drafts_pending,
    )
    return 0


# ---------------- dry-run fixture ----------------

def _dry_run_fixture_leads():
    """A handful of fake leads so `prepare --dry-run` works without Apollo."""
    from .models import Lead
    return [
        Lead(
            name="Sunny Shajan",
            title="Managing Director",
            company="McKesson",
            email="sunny.test@example.com",
            linkedin="https://www.linkedin.com/in/sunny-shajan/",
            industry="Healthcare",
            location="Chicago, Illinois",
            is_uiuc_alum=True,
            schools=["University of Illinois Urbana-Champaign"],
            source="fixture",
        ),
        Lead(
            name="Alex Meyer",
            title="Managing Partner",
            company="Origin Ventures",
            email="alex.test@example.com",
            linkedin="https://www.linkedin.com/in/meyerchicago/",
            industry="Venture Capital",
            location="Chicago, Illinois",
            is_uiuc_alum=True,
            source="fixture",
        ),
        Lead(
            name="Gautam Ajjarapu",
            title="CEO & Founder",
            company="Glide",
            email="gautam.test@example.com",
            industry="Computer Software",
            location="San Francisco, CA",
            is_uiuc_alum=True,
            source="fixture",
        ),
    ]


# ---------------- entrypoint ----------------

def main() -> int:
    # Load a local .env for dev runs. No-op in GitHub Actions (no .env file there);
    # load_dotenv never overrides env vars already set, so CI secrets win.
    load_dotenv()
    parser = argparse.ArgumentParser(prog="cube-outreach")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="Source + draft today's outreach batch")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("send", help="Send approved drafts + check replies + digest")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("bootstrap", help="Create Sheet tabs + headers")
    args = parser.parse_args()

    if args.cmd == "prepare":
        return cmd_prepare(dry_run=args.dry_run)
    if args.cmd == "send":
        return cmd_send(dry_run=args.dry_run)
    if args.cmd == "bootstrap":
        sheets = SheetClient()
        sheets.bootstrap()
        log.info("Sheet bootstrapped")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

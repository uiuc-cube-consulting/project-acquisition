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

from .dashboard import write_dashboard
from .draft import draft_for_leads
from .follow_up import prepare_follow_ups
from .models import Lead, LeadStatus
from .past_projects import PastProjectIndex
from .scoring import Scorer
from .sheets import SheetClient
from .sourcing.apollo import (
    ApolloClient, Candidate, bulk_reveal, candidate_from_contact, load_profiles,
    pick_profile_for_today, search_candidates,
)
from .sourcing.cube_alumni import fetch_alumni_leads
from .summary import send_daily_summary
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

    # 1) Source. `sheet_leads` already have emails (no cost). `candidates` need an
    #    Apollo enrichment to reveal the email — these come from the pasted Alumni
    #    tab (looked up by name+company / LinkedIn) and from Apollo discovery. We
    #    only spend a credit on the ones we actually select (step 3).
    apollo: ApolloClient | None = ApolloClient() if (not dry_run and os.environ.get("APOLLO_API_KEY")) else None
    sheet_leads: list[Lead] = []
    candidates: list[Candidate] = []
    if dry_run:
        log.info("[DRY RUN] skipping live sourcing — using fixtures")
        sheet_leads = _dry_run_fixture_leads()
    else:
        # Free sheet leads that already include an email.
        sheet_leads.extend(sheets.fetch_prospect_leads())
        from .sheets import load_service_account_info
        sheet_leads.extend(fetch_alumni_leads(load_service_account_info()))

        # UIUC alumni you paste into the Alumni tab (from LinkedIn's Alumni tool).
        # Rows with an email are ready; rows with just name+company get their email
        # looked up via Apollo. All are flagged alumni and rank first.
        alum_leads, alum_contacts = sheets.fetch_alumni_targets()
        sheet_leads.extend(alum_leads)
        if alum_contacts and apollo:
            candidates.extend(
                candidate_from_contact(**c, is_uiuc_alum=True, source="alumni_input")
                for c in alum_contacts
            )
        elif alum_contacts:
            log.warning("%d alumni rows need an email lookup, but APOLLO_API_KEY is unset", len(alum_contacts))

        # Apollo discovery for breadth/volume (ranked below confirmed alumni).
        if apollo:
            profiles = load_profiles()
            day_index = datetime.now(timezone.utc).timetuple().tm_yday
            secondary = pick_profile_for_today(profiles, day_index)
            log.info("Apollo discovery profile: %s", secondary["name"])
            candidates.extend(search_candidates(apollo, secondary, max_results=50))
        else:
            log.info("APOLLO_API_KEY not set — sourcing from the sheet sources only")

    # 2) Pre-reveal filtering (no Apollo credits spent). Drop anyone already in
    #    the pipeline by LinkedIn; Sheet leads (email already known) also get the
    #    full email-based exclusions now.
    pool: list = list(sheet_leads) + list(candidates)
    filtered: list = []
    for item in pool:
        li = (getattr(item, "linkedin", None) or "").strip().lower()
        if li and li in known_li:
            continue
        if isinstance(item, Lead):
            if item.email.lower() in known:
                continue
            excluded, reason = scorer.is_excluded(item, suppression, contacted)
            if excluded:
                log.info("Skipping %s: %s", item.email.lower(), reason)
                continue
        filtered.append(item)

    # 3) Score, rank alumni-first, then select the top `target`. Apollo emails are
    #    revealed here and ONLY here — one credit per selected lead, capped at
    #    `target * 2` reveals so a single run can't burn through credits.
    for item in filtered:
        item.score = scorer.score(item, past_kw)
    filtered.sort(key=lambda x: (x.is_uiuc_alum, x.score), reverse=True)

    # Walk in alumni-first order, revealing Apollo candidates' emails in BULK
    # (10 per call) and only for leads we actually take. Cap total reveals at
    # target*2 to protect credits.
    top: list[Lead] = []
    revealed: dict[int, Lead | None] = {}
    reveals = 0
    reveal_budget = target * 2
    idx = 0
    while len(top) < target and idx < len(filtered) and reveals < reveal_budget:
        # Reveal in bulk, but size each batch to what we still need (+2 buffer for
        # ones that get filtered out), capped at 10/call and the overall budget.
        batch_size = max(1, min(10, (target - len(top)) + 2, reveal_budget - reveals))
        window = filtered[idx:idx + batch_size]
        idx += batch_size
        to_reveal = [it for it in window if isinstance(it, Candidate) and id(it) not in revealed]
        if to_reveal:
            for cand, lead in zip(to_reveal, bulk_reveal(apollo, to_reveal)):
                revealed[id(cand)] = lead
            reveals += len(to_reveal)
        for it in window:
            if len(top) >= target:
                break
            lead = it if isinstance(it, Lead) else revealed.get(id(it))
            if lead is None:
                continue  # no email revealed
            email_lc = lead.email.lower()
            if email_lc in known or email_lc in suppression:
                continue
            excluded, reason = scorer.is_excluded(lead, suppression, contacted)
            if excluded:
                log.info("Skipping %s: %s", email_lc, reason)
                continue
            if isinstance(it, Candidate):
                lead.score = it.score
            top.append(lead)

    log.info("Selected %d leads for drafting (alumni-first); %d Apollo reveals used", len(top), reveals)

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
    sheets.append_drafts(drafts_to_write)

    # 6) Follow-ups for older sends (also written to Drafts for approval)
    follow_ups = prepare_follow_ups(sender_name=sender_name)  # list[(row, Draft)]

    # 7) Approval happens in the Sheet: review the Drafts tab and set the
    #    `approved` column to yes/TRUE on the rows to send. The send job mails
    #    exactly those. No approval email is sent.
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheets.sheet_id}/edit"
    log.info(
        "Wrote %d new drafts (+%d follow-ups) to the Drafts tab. Set 'approved'=yes "
        "on the rows to send, then `send` mails them: %s",
        len(drafts_to_write), len(follow_ups), sheet_url,
    )
    _refresh_dashboard(sheets)
    return 0


def _refresh_dashboard(sheets: SheetClient) -> None:
    """Refresh the Dashboard tab; never let a stats error fail the main job."""
    try:
        write_dashboard(sheets)
    except Exception as exc:
        log.warning("Dashboard refresh failed (non-fatal): %s", exc)


# ---------------- send ----------------

def cmd_send(dry_run: bool) -> int:
    cap = int(os.environ.get("DAILY_SEND_CAP", "10"))
    sheets = SheetClient()

    # Approval is the `approved` column in the Drafts tab (set it to yes/TRUE).
    approved = sheets.list_approved_pending()
    log.info("%d drafts approved + pending", len(approved))

    from .gmail_send import GmailSender
    sender = GmailSender()

    sent_count = 0
    follow_up_count = 0
    for row_idx, draft in approved[:cap]:
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
        if draft.is_follow_up:
            follow_up_count += 1

    log.info("Sent %d emails (%d follow-ups)", sent_count, follow_up_count)
    if not dry_run:
        drafts_pending = len(sheets.list_approved_pending())  # remaining after this run
        send_daily_summary(
            sent_count=sent_count,
            follow_ups=follow_up_count,
            drafts_pending=drafts_pending,
        )
        _refresh_dashboard(sheets)
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
    p = sub.add_parser("stats", help="Refresh the Dashboard tab with current metrics")
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
    if args.cmd == "stats":
        write_dashboard(SheetClient())
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

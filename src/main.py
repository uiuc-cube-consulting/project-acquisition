"""Orchestrator CLI.

Commands, and the GitHub Actions workflows that run them:

  prepare  — 06:00 CT M-F (prepare.yml)
      1. Source leads: the Alumni/Prospects Sheet tabs plus Apollo discovery
         (DISCOVERY_PROFILES_PER_RUN breadth profiles, rotated daily)
      2. Dedupe against existing Leads, LinkedIn URLs and the suppression list
      3. Score, then fill TWO quotas — ALUMNI_TARGET_SHARE of the batch to UIUC
         alumni, the rest to non-alumni discovery. Apollo emails are revealed
         only for the leads actually selected. Either quota backfills the other.
      4. Draft the batch via Gemini (batched, see draft.py)
      5. Write Leads + Drafts, pre-approved when AUTO_APPROVE is set
      6. Log the run's sourcing stats to the `Runs` tab
      7. Refresh the Sheet's Dashboard tab

  send     — 10:00 CT M-F (send.yml)
      1. Read approved-but-unsent rows from Drafts
      2. Skip anyone already contacted (dedupe guard) and anyone suppressed
      3. Send each via Gmail SMTP, throttled, up to DAILY_SEND_CAP
      4. Update Leads.status -> sent, write message IDs
      5. Email a daily summary, then scan the inbox (`replies`)

  replies  — read-only IMAP scan: records replies, auto-replies and bounces
  report   — build the standalone HTML dashboard in dashboard/
  stats    — refresh the Sheet's Dashboard tab
  bootstrap— create the Sheet tabs + headers

`prepare` and `send` accept --dry-run for safe local smoke testing.
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
    pick_profiles_for_today, search_candidates,
)
from .sourcing.cube_alumni import fetch_alumni_leads
from .summary import send_daily_summary
from .template import TemplateRouter
from .companies import CompanyRegistry, email_domain, normalize_company
from .env import env_flag, env_float, env_int, env_str

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("cube.main")


def _sender_identity() -> tuple[str, str]:
    return (
        env_str("SENDER_NAME", "Sujan Sriram"),
        env_str("SENDER_PHONE", "—"),
    )


def _alumni_share() -> float:
    """Fraction of each day's batch reserved for UIUC alumni (rest is discovery).

    Default 0.35: alumni still convert best, but the whole point of the Spring
    2027 push is breadth — founders, Chicago businesses, big tech and big
    consulting. Set ALUMNI_TARGET_SHARE to retune without a code change.
    """
    return min(1.0, max(0.0, env_float("ALUMNI_TARGET_SHARE", 0.35)))


def _auto_approve() -> bool:
    """Whether `prepare` should mark its drafts approved without a human.

    Off by default — the Sheet's `approved` column stays the gate. Turned on for
    the Spring 2027 campaign (AUTO_APPROVE=1 in the prepare workflow) so the
    daily batch goes out unattended. The send job's own guards still apply:
    the suppression list, the already-contacted dedupe, and DAILY_SEND_CAP.
    """
    return env_flag("AUTO_APPROVE")


def _company_dedupe() -> bool:
    """Whether to enforce one-company-one-conversation (COMPANY_DEDUPE, default on)."""
    return env_flag("COMPANY_DEDUPE", default=True)


def _discovery_profile_count() -> int:
    """How many Apollo breadth profiles to search per run."""
    return max(1, env_int("DISCOVERY_PROFILES_PER_RUN", 3))


class _Selector:
    """Walks a scored queue and takes the first N contactable leads.

    Apollo emails are revealed here and ONLY here — one credit per candidate —
    in bulk (10 per call) and only for people we are actually about to email.
    One instance is shared across the alumni and discovery queues so the reveal
    budget is enforced across the whole run, not per queue.
    """

    def __init__(self, *, apollo, sheets, scorer, known, suppression, contacted, budget,
                 companies=None):
        self.apollo = apollo
        self.sheets = sheets
        self.scorer = scorer
        self.known = known
        self.suppression = suppression
        self.contacted = contacted
        self.budget = budget
        # Companies already contacted (plus any claimed earlier in this run).
        self.companies = companies
        self.revealed: dict[int, Lead | None] = {}
        self.reveals = 0
        self.reveals_found = 0
        self.alumni_attempted = 0
        self.alumni_found = 0
        self._cursors: dict[int, int] = {}

    def take(self, queue: list, slots: int) -> list[Lead]:
        """Pull up to `slots` usable leads off `queue`, resuming where the last
        call on that queue stopped (so backfill never re-walks the same people)."""
        picked: list[Lead] = []
        if slots <= 0:
            return picked
        idx = self._cursors.get(id(queue), 0)
        while len(picked) < slots and idx < len(queue) and self.reveals < self.budget:
            # Size each batch to what we still need (+2 for ones that get
            # filtered out), capped at 10 per call and by the remaining budget.
            batch = max(1, min(10, (slots - len(picked)) + 2, self.budget - self.reveals))
            window = queue[idx:idx + batch]
            idx += batch
            self._reveal(window)
            for it in window:
                if len(picked) >= slots:
                    break
                lead = it if isinstance(it, Lead) else self.revealed.get(id(it))
                if lead is None:
                    continue  # no email revealed
                email_lc = lead.email.lower()
                if email_lc in self.known or email_lc in self.suppression:
                    continue
                # Second company check, now that the email (and so the domain) is
                # known — this catches what the name alone can't, e.g. "PwC" vs
                # "PricewaterhouseCoopers" both resolving to pwc.com.
                if self.companies is not None and self.companies.seen(lead.company, lead.email):
                    log.info("Skipping %s: %s already contacted", email_lc, lead.company)
                    continue
                excluded, reason = self.scorer.is_excluded(lead, self.suppression, self.contacted)
                if excluded:
                    log.info("Skipping %s: %s", email_lc, reason)
                    continue
                if isinstance(it, Candidate):
                    lead.score = it.score
                # Claim the address and the company so nothing later in this run
                # — including the other quota — takes either again.
                self.known.add(email_lc)
                if self.companies is not None:
                    self.companies.claim(lead.company, lead.email)
                picked.append(lead)
        self._cursors[id(queue)] = idx
        return picked

    def _reveal(self, window: list) -> None:
        # Filtering on the company name BEFORE the reveal is the whole point: an
        # already-contacted company costs us nothing instead of a credit. The
        # `batch_seen` set extends that within the window itself — two founders
        # at the same new company would otherwise both be revealed, and the
        # second rejected immediately afterwards.
        to_reveal: list[Candidate] = []
        batch_seen: set[str] = set()
        for it in window:
            if not isinstance(it, Candidate) or id(it) in self.revealed:
                continue
            if self.companies is not None and self.companies.seen(it.company):
                continue
            key = normalize_company(it.company)
            if key and key in batch_seen:
                continue
            if key:
                batch_seen.add(key)
            to_reveal.append(it)
        if not to_reveal or self.apollo is None:
            return
        for cand, lead in zip(to_reveal, bulk_reveal(self.apollo, to_reveal)):
            self.revealed[id(cand)] = lead
            self.reveals_found += 1 if lead else 0
            # Cache the lookup back to the Alumni tab so we never re-spend a
            # credit on this person: their email (or NOT_FOUND if unresolved).
            if cand.ref is not None:
                self.alumni_attempted += 1
                self.alumni_found += 1 if lead else 0
                try:
                    self.sheets.set_alumni_email(
                        cand.ref, lead.email if lead else self.sheets.NOT_FOUND_MARKER
                    )
                except Exception as exc:
                    log.warning("Alumni write-back failed for row %s: %s", cand.ref, exc)
        self.reveals += len(to_reveal)


# ---------------- prepare ----------------

def cmd_prepare(dry_run: bool) -> int:
    target = env_int("DAILY_PREPARE_TARGET", 15)
    sheets = SheetClient()
    sheets.bootstrap()

    suppression = sheets.get_suppression_emails()
    known = sheets.get_known_emails()
    known_li = sheets.get_known_linkedins()
    contacted = sheets.get_contacted_dates()
    # One company, one conversation: every company already in Leads (or listed
    # by hand on the Companies tab) is off the table. See companies.py.
    companies = CompanyRegistry.from_rows(
        sheets.book.worksheet("Leads").get_all_records(),
        sheets.fetch_company_rows(),
    ) if _company_dedupe() else None
    if companies is not None:
        log.info("Company dedupe on: %d companies already contacted", len(companies))
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
    profiles_used: list[str] = []  # today's Apollo discovery profiles, for the Runs log
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
            for c in alum_contacts:
                ref = c.pop("_row", None)
                candidates.append(
                    candidate_from_contact(**c, is_uiuc_alum=True, source="alumni_input", ref=ref)
                )
        elif alum_contacts:
            log.warning("%d alumni rows need an email lookup, but APOLLO_API_KEY is unset", len(alum_contacts))

        # Apollo discovery — the non-alumni half of the batch. We search SEVERAL
        # profiles per run (rotating which ones lead) rather than one: a single
        # profile's top hits are mostly people we already emailed, so one profile
        # cannot reliably fill the discovery quota once the pipeline has run for
        # a while. Searching costs no credits — only the reveals do.
        if apollo:
            profiles = load_profiles()
            day_index = datetime.now(timezone.utc).timetuple().tm_yday
            for profile in pick_profiles_for_today(profiles, day_index, count=_discovery_profile_count()):
                log.info("Apollo discovery profile: %s", profile["name"])
                profiles_used.append(profile["name"])
                try:
                    candidates.extend(search_candidates(apollo, profile, max_results=50))
                except Exception as exc:
                    # One bad profile (bad filter, transient 5xx) must not cost us
                    # the whole day's discovery pool.
                    log.warning("Apollo search failed for %s: %s", profile["name"], exc)
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
        if companies is not None and companies.seen(
            getattr(item, "company", None), getattr(item, "email", None)
        ):
            continue  # already pitched this company
        if isinstance(item, Lead):
            if item.email.lower() in known:
                continue
            excluded, reason = scorer.is_excluded(item, suppression, contacted)
            if excluded:
                log.info("Skipping %s: %s", item.email.lower(), reason)
                continue
        filtered.append(item)

    # 3) Score, then fill two SEPARATE quotas — alumni and everyone else.
    #
    #    This used to be one alumni-first sort over a single queue, which starved
    #    non-alumni completely: any day the Alumni tab held >= `target` people,
    #    every slot went to alumni and Apollo discovery contributed zero. Quotas
    #    guarantee the outreach reaches founders, Chicago businesses and big
    #    tech/consulting every day, not just on days the alumni bench runs dry.
    for item in filtered:
        item.score = scorer.score(item, past_kw)

    alumni_queue = sorted(
        (x for x in filtered if x.is_uiuc_alum), key=lambda x: x.score, reverse=True
    )
    discovery_queue = sorted(
        (x for x in filtered if not x.is_uiuc_alum), key=lambda x: x.score, reverse=True
    )
    alumni_slots = round(target * _alumni_share())
    discovery_slots = target - alumni_slots

    selector = _Selector(
        apollo=apollo, sheets=sheets, scorer=scorer, known=known,
        suppression=suppression, contacted=contacted, budget=target * 2,
        companies=companies,
    )
    # Whichever pool runs short hands its unused slots to the other, so a thin
    # alumni bench never costs us total volume.
    alumni_picked = selector.take(alumni_queue, alumni_slots)
    discovery_picked = selector.take(discovery_queue, target - len(alumni_picked))
    if len(alumni_picked) + len(discovery_picked) < target:
        alumni_picked += selector.take(
            alumni_queue, target - len(alumni_picked) - len(discovery_picked)
        )
    top: list[Lead] = alumni_picked + discovery_picked
    reveals, reveals_found = selector.reveals, selector.reveals_found
    alumni_attempted, alumni_found = selector.alumni_attempted, selector.alumni_found

    log.info(
        "Selected %d leads: %d alumni (%d slots) + %d discovery (%d slots); %d Apollo reveals used",
        len(top), len(alumni_picked), alumni_slots,
        len(discovery_picked), discovery_slots, reveals,
    )

    sender_name, sender_phone = _sender_identity()

    # 4) Draft
    pairs = []
    if top:
        router = TemplateRouter()
        pairs = draft_for_leads(top, router, past_index, sender_name, sender_phone)
        log.info("Generated %d drafts", len(pairs))
        # Every lead here already cost an Apollo credit to reveal, so a drafting
        # shortfall is wasted spend, not just a smaller batch. Say so loudly.
        if len(pairs) < len(top):
            log.error(
                "DRAFTING SHORTFALL: %d of %d selected leads produced no draft "
                "(their Apollo credits are spent). Usually Gemini rate limits — "
                "raise GEMINI_MIN_INTERVAL_SECONDS or lower DAILY_PREPARE_TARGET.",
                len(top) - len(pairs), len(top),
            )
    else:
        log.info("No new leads to draft today (follow-ups may still be due)")

    if dry_run:
        for lead, draft in pairs[:3]:
            print(f"\n---\nTO: {lead.email}\nSUBJECT: {draft.subject}\n\n{draft.body}\n")
        return 0

    # 5) Write to Sheet
    auto_approve = _auto_approve()
    leads_to_write = []
    drafts_to_write = []
    for lead, draft in pairs:
        draft.approved = auto_approve
        lead.status = LeadStatus.DRAFTED
        leads_to_write.append(lead)
        drafts_to_write.append(draft)
    sheets.append_leads(leads_to_write)
    sheets.append_drafts(drafts_to_write)

    # Extend the running company list so tomorrow's run skips these outright.
    if _company_dedupe() and leads_to_write:
        added = sheets.record_companies(
            {
                "company": lead.company,
                "normalized": normalize_company(lead.company),
                "domain": email_domain(lead.email),
                "source": lead.source,
            }
            for lead in leads_to_write if normalize_company(lead.company)
        )
        log.info("Company list: %d new companies recorded", added)

    # Sourcing telemetry for the dashboards — how many credits we spent and how
    # many of them actually produced an email.
    sheets.log_run(
        profile=", ".join(profiles_used),
        candidates_seen=len(candidates),
        reveals_attempted=reveals,
        emails_found=reveals_found,
        alumni_attempted=alumni_attempted,
        alumni_found=alumni_found,
        leads_selected=len(top),
        drafts_created=len(drafts_to_write),
        drafts_failed=len(top) - len(pairs),
    )

    # 6) Follow-ups are OFF by default — every slot goes to reaching NEW people;
    #    re-emailing is handled manually. Set ENABLE_FOLLOW_UPS=1 to re-enable.
    follow_ups: list = []
    if env_flag("ENABLE_FOLLOW_UPS"):
        follow_ups = prepare_follow_ups(sender_name=sender_name)  # list[(row, Draft)]

    # 7) Approval. With AUTO_APPROVE the rows are already ticked and the next
    #    send run mails them; otherwise a human sets `approved`=yes in the Sheet.
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheets.sheet_id}/edit"
    if auto_approve:
        log.info(
            "Wrote %d new drafts (+%d follow-ups), AUTO-APPROVED — the next send run "
            "mails them with no review step: %s",
            len(drafts_to_write), len(follow_ups), sheet_url,
        )
    else:
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
    cap = env_int("DAILY_SEND_CAP", 10)
    sheets = SheetClient()

    # Approval is the `approved` column in the Drafts tab (set it to yes/TRUE).
    approved = sheets.list_approved_pending()
    # Guard: never email anyone already contacted (skip duplicate approved drafts),
    # and dedupe within this batch — protects against double-sends.
    already = sheets.get_sent_emails()
    queue: list = []
    seen: set[str] = set()
    for row_idx, draft in approved:
        el = draft.lead_email.lower()
        # Follow-ups are intentionally to already-contacted leads — let them through.
        if not draft.is_follow_up and (el in already or el in seen):
            log.info("Skipping %s: already contacted (duplicate draft)", draft.lead_email)
            sheets.mark_draft_error(row_idx, "skipped: lead already contacted")
            continue
        seen.add(el)
        queue.append((row_idx, draft))
    log.info("%d approved; %d to send after dedupe (cap %d)", len(approved), len(queue), cap)

    from .gmail_send import GmailSender
    sender = GmailSender()
    # The info packet now travels as a link in the email body (see draft.py),
    # so outreach no longer carries a PDF attachment.

    sent_count = 0
    follow_up_count = 0
    for row_idx, draft in queue[:cap]:
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


# ---------------- replies ----------------

def cmd_replies(dry_run: bool, since: str | None, classify: bool) -> int:
    """Read the sending mailbox and record what came back.

    Read-only on the mailbox; writes the `Replies` tab and the matching
    `status` / `replied_at` cells on Leads.
    """
    from datetime import date

    from .replies import sync_replies

    since_date = date.fromisoformat(since) if since else None
    sheets = SheetClient()
    summary = sync_replies(sheets, since=since_date, classify=classify, dry_run=dry_run)
    log.info(
        "Inbox scan: %d matched — %d replies, %d auto-replies, %d bounced (%d scanned)",
        summary["matched"], summary["human"], summary["auto_reply"],
        summary["bounce"], summary["scanned"],
    )
    if summary["by_classification"]:
        log.info("Reply sentiment: %s", summary["by_classification"])
    if not dry_run:
        _refresh_dashboard(sheets)
    return 0


# ---------------- report ----------------

def cmd_report(out_dir: str, open_browser: bool) -> int:
    from .report import build_report

    path = build_report(SheetClient(), out_dir=out_dir)
    log.info("Dashboard written to %s", path)
    if open_browser:
        import webbrowser

        webbrowser.open(path.resolve().as_uri())
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
    p = sub.add_parser("replies", help="Scan the mailbox for replies/bounces (read-only)")
    p.add_argument("--dry-run", action="store_true", help="Print what was found; write nothing")
    p.add_argument("--since", help="Only scan mail on/after this date (YYYY-MM-DD)")
    p.add_argument("--no-classify", action="store_true", help="Skip Gemini sentiment labelling")
    p = sub.add_parser("report", help="Build the standalone HTML metrics dashboard")
    p.add_argument("--out", default="dashboard", help="Output directory (default: dashboard/)")
    p.add_argument("--open", action="store_true", help="Open the dashboard in a browser when done")
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
    if args.cmd == "replies":
        return cmd_replies(
            dry_run=args.dry_run, since=args.since, classify=not args.no_classify
        )
    if args.cmd == "report":
        return cmd_report(out_dir=args.out, open_browser=args.open)
    return 1


if __name__ == "__main__":
    sys.exit(main())

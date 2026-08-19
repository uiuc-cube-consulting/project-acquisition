"""Pipeline metrics.

One place that turns the raw Sheet tabs into every number the dashboards show.
Both consumers read from here so they can never disagree:

  - `dashboard.py`  writes the plain `Dashboard` tab inside the Sheet
  - `report.py`     renders the standalone shareable HTML dashboard

`compute()` is pure — it takes lists of row dicts, so it can be exercised
without touching Google. `collect()` is the thin wrapper that fetches the tabs.

A note on the two rates people ask about most:

  Reply rate       counts people who typed a real answer back, over the emails
                   that were actually *delivered* — bounced addresses never
                   reached a human, so leaving them in the denominator would
                   understate the rate. The data comes from the `Replies` tab
                   (written by `replies.py` scanning the mailbox); the Leads
                   `status` / `replied_at` columns are honored too, for replies
                   logged by hand.
  Apollo find rate is how often an email lookup actually resolves. The precise
                   per-run version comes from the `Runs` tab (written by
                   `prepare`); before that tab has history we fall back to the
                   Alumni tab's resolved-vs-NOT_FOUND tally, which covers every
                   lookup ever done. Its companion is the bounce rate — a found
                   address that bounces was still the wrong address.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from .models import LeadStatus
from .sheets import _truthy

# Lead statuses that mean the recipient came back to us in some way.
RESPONSE_STATUSES = {
    LeadStatus.REPLIED.value,
    LeadStatus.HOT.value,
    LeadStatus.CLOSED.value,
}

# Lead `source` values that arrive with an email already attached (no Apollo
# credit spent). Anything else came out of Apollo discovery.
FREE_SOURCES = {"prospects_sheet", "cube_alumni_sheet", "fixture"}

NOT_FOUND = "NOT_FOUND"

# Send errors that aren't failures — the dedupe guard declining to re-email.
SKIP_PREFIX = "skipped:"


# ---------------- small helpers ----------------

def _s(row: dict, key: str) -> str:
    v = row.get(key)
    return "" if v is None else str(v).strip()


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _day(value: str) -> date | None:
    dt = _parse_dt(value)
    return dt.date() if dt else None


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _rate(num: int, den: int) -> float | None:
    """None (not 0.0) when there's no denominator — 'no data' and 'zero' differ."""
    return (num / den) if den else None


def _top(counter: Counter, n: int) -> list[dict]:
    return [{"label": k, "count": v} for k, v in counter.most_common(n)]


def _alumni_share_setting() -> float:
    """Mirrors main._alumni_share so the dashboard can show the mix we're
    aiming for next to the mix we're actually getting."""
    from .env import env_float

    return min(1.0, max(0.0, env_float("ALUMNI_TARGET_SHARE", 0.35)))


def campaign_window() -> tuple[str, str]:
    """(campaign name, ISO start date) for the term currently being sourced.

    Outreach runs a semester ahead, so the pipeline's history spans more than one
    campaign. Everything sent on or after CAMPAIGN_START belongs to the term named
    by TARGET_TERM; earlier sends were the previous cycle. This is what lets the
    dashboard show "how is Spring 2027 going" instead of an all-time average that
    the old cycle dominates.
    """
    from .env import env_str

    return (
        env_str("TARGET_TERM", "Spring 2027"),
        env_str("CAMPAIGN_START", "2026-08-18"),
    )


def _campaign_slice(
    contacted_dates: dict[str, date],
    start: date,
    alum_flag: dict[str, bool],
    bounced: set[str],
    auto_replied: set[str],
    human_replied: set[str],
    interested: set[str],
    source_of: dict[str, str],
) -> dict:
    """Recompute the headline funnel over only the people first emailed on/after
    `start`. Same definitions as the all-time block, narrower population."""
    cohort = {e for e, d in contacted_dates.items() if d >= start}
    delivered = cohort - bounced
    replied = cohort & human_replied
    alumni = {e for e in cohort if alum_flag.get(e)}
    non_alumni = cohort - alumni

    def seg(members: set[str], label: str) -> dict:
        deliv = members - bounced
        reps = members & human_replied
        return {
            "label": label,
            "sent": len(members),
            "delivered": len(deliv),
            "bounced": len(members & bounced),
            "replies": len(reps),
            "interested": len(members & interested),
            "reply_rate": _rate(len(reps), len(deliv)),
            "bounce_rate": _rate(len(members & bounced), len(members)),
        }

    by_source = Counter(source_of.get(e, "unknown") for e in cohort)
    return {
        "start": start.isoformat(),
        "sent": len(cohort),
        "delivered": len(delivered),
        "bounced": len(cohort & bounced),
        "replies": len(replied),
        "interested": len(cohort & interested),
        "auto_replies": len(cohort & auto_replied),
        "reply_rate": _rate(len(replied), len(delivered)),
        "interested_rate": _rate(len(cohort & interested), len(delivered)),
        "bounce_rate": _rate(len(cohort & bounced), len(cohort)),
        "alumni_sent": len(alumni),
        "non_alumni_sent": len(non_alumni),
        "non_alumni_share": _rate(len(non_alumni), len(cohort)),
        "by_audience": [seg(alumni, "UIUC alumni"), seg(non_alumni, "Non-alumni")],
        "by_source": [
            {"label": k, "count": v} for k, v in by_source.most_common()
        ],
        "daily": [
            {"date": d.isoformat(), "sent": n}
            for d, n in sorted(Counter(
                day for e, day in contacted_dates.items() if day >= start
            ).items())
        ],
    }


def _clean_error(text: str) -> str:
    """Make an SMTP error readable. They arrive as a stringified bytes tuple —
    `(535, b'5.7.8 Username and Password not accepted...\\n5.7.8 http...')` —
    whose literal escapes and repeated codes are noise on a dashboard."""
    out = re.sub(r"^\(\s*\d+\s*,\s*b?['\"]?", "", text.strip())
    out = out.replace("\\n", " ").replace("\\r", " ").rstrip("')\"")
    out = re.sub(r"\b\d\.\d\.\d\b", " ", out)          # drop repeated status codes
    out = re.sub(r"https?://\S+", "", out)             # and the help URLs
    # Cut the boilerplate tail. It carries a per-session id, so leaving it in
    # gives every occurrence of the same failure a different label and stops
    # them grouping in the error tally.
    out = re.split(r"\s*for more info(?:rmation)?\b", out, maxsplit=1, flags=re.I)[0]
    out = re.sub(r"\s+[a-z0-9-]{12,}\s*-\s*\w+\s*$", "", out, flags=re.I)
    return re.sub(r"\s+", " ", out).strip(" -.,") or text[:90]


# ---------------- the computation ----------------

def compute(
    leads: list[dict],
    drafts: list[dict],
    alumni: list[dict],
    suppression: list[dict] | None = None,
    hot_leads: list[dict] | None = None,
    runs: list[dict] | None = None,
    replies: list[dict] | None = None,
    daily_target: int = 15,
) -> dict:
    suppression = suppression or []
    hot_leads = hot_leads or []
    runs = runs or []
    replies = replies or []
    today = datetime.now(timezone.utc).date()

    # ---- leads ----
    lead_by_email: dict[str, dict] = {}
    for l in leads:
        email = _s(l, "email").lower()
        if email:
            lead_by_email[email] = l

    alum_flag = {e: _truthy(l.get("is_uiuc_alum")) for e, l in lead_by_email.items()}
    source_of = {e: (_s(l, "source") or "unknown") for e, l in lead_by_email.items()}

    def responded(l: dict) -> bool:
        return _s(l, "status") in RESPONSE_STATUSES or bool(_s(l, "replied_at"))

    responders = {e for e, l in lead_by_email.items() if responded(l)}

    # ---- what came back (Replies tab, written by the inbox scan) ----
    reply_by_email: dict[str, dict] = {}
    for r in replies:
        email = _s(r, "lead_email").lower()
        if email:
            reply_by_email[email] = r

    bounced = {e for e, r in reply_by_email.items() if _s(r, "category") == "bounce"}
    auto_replied = {e for e, r in reply_by_email.items() if _s(r, "category") == "auto_reply"}
    human_replied = {e for e, r in reply_by_email.items() if _s(r, "category") == "human"}
    # Replies logged by hand on the Leads tab count too.
    human_replied |= responders
    sentiment = Counter(
        _s(r, "classification")
        for e, r in reply_by_email.items()
        if _s(r, "category") == "human" and _s(r, "classification")
    )
    interested = {
        e for e, r in reply_by_email.items()
        if _s(r, "classification") == "positive"
    } | {e for e, l in lead_by_email.items() if _s(l, "status") == LeadStatus.HOT.value}

    # ---- drafts: split initial outreach from follow-ups ----
    initial = [d for d in drafts if not _truthy(d.get("is_follow_up"))]
    follow_ups = [d for d in drafts if _truthy(d.get("is_follow_up"))]
    sent_all = [d for d in drafts if _s(d, "sent_at")]
    sent_initial = [d for d in initial if _s(d, "sent_at")]
    sent_follow_ups = [d for d in follow_ups if _s(d, "sent_at")]
    approved_initial = [d for d in initial if _truthy(d.get("approved"))]

    # Every distinct person who has received at least one email from us.
    contacted = {_s(d, "lead_email").lower() for d in sent_all if _s(d, "lead_email")}
    contacted.update(
        e for e, l in lead_by_email.items() if _s(l, "sent_at")
    )
    contacted.discard("")

    # First time each person was emailed — the basis for splitting the history
    # into campaigns (a Fall-2026-cycle contact stays in that cycle forever).
    contacted_dates: dict[str, date] = {}
    for d in sent_all:
        email = _s(d, "lead_email").lower()
        day = _day(_s(d, "sent_at"))
        if email and day and (email not in contacted_dates or day < contacted_dates[email]):
            contacted_dates[email] = day
    for email, lead in lead_by_email.items():
        day = _day(_s(lead, "sent_at"))
        if day and (email not in contacted_dates or day < contacted_dates[email]):
            contacted_dates[email] = day

    sent_alumni = sum(1 for e in contacted if alum_flag.get(e))
    # Delivered = reached a mailbox. Bounces never did, so they don't belong in
    # any reply-rate denominator.
    delivered = contacted - bounced
    responses = len(human_replied)
    engaged = human_replied | auto_replied

    # ---- timeline: sends per ISO week, zero-filled ----
    send_days = Counter()
    for d in sent_all:
        day = _day(_s(d, "sent_at"))
        if day:
            send_days[day] += 1
    draft_days = Counter()
    for d in drafts:
        day = _day(_s(d, "prepared_at"))
        if day:
            draft_days[day] += 1

    first_send = min(send_days) if send_days else None
    last_send = max(send_days) if send_days else None
    first_activity = min([*send_days, *draft_days], default=None)

    weekly: list[dict] = []
    if first_activity:
        wk = _monday(first_activity)
        end = _monday(max([*send_days, *draft_days, today]))
        while wk <= end:
            span = [wk + timedelta(days=i) for i in range(7)]
            weekly.append({
                "week_start": wk.isoformat(),
                "label": wk.strftime("%b %-d"),
                "sent": sum(send_days.get(d, 0) for d in span),
                "drafted": sum(draft_days.get(d, 0) for d in span),
            })
            wk += timedelta(days=7)

    daily = [
        {"date": d.isoformat(), "sent": n}
        for d, n in sorted(send_days.items())
    ]

    def sent_between(start: date, end: date) -> int:
        return sum(n for d, n in send_days.items() if start <= d <= end)

    last_7 = sent_between(today - timedelta(days=6), today)
    prior_7 = sent_between(today - timedelta(days=13), today - timedelta(days=7))
    last_30 = sent_between(today - timedelta(days=29), today)

    # Weekdays in the window that saw at least one send — the cadence metric.
    weekdays_in_window = 0
    if first_send:
        cur = first_send
        stop = max(last_send or first_send, today)
        while cur <= stop:
            if cur.weekday() < 5:
                weekdays_in_window += 1
            cur += timedelta(days=1)
    active_days = len(send_days)

    # ---- Apollo email lookups ----
    alum_resolved = alum_notfound = alum_pending = 0
    alumni_emails: set[str] = set()
    for a in alumni:
        email = _s(a, "email")
        if not email:
            alum_pending += 1
        elif email.upper() == NOT_FOUND:
            alum_notfound += 1
        elif "@" in email:
            alum_resolved += 1
            alumni_emails.add(email.lower())
        else:
            alum_pending += 1
    alum_attempted = alum_resolved + alum_notfound

    run_attempts = sum(int(_s(r, "reveals_attempted") or 0) for r in runs)
    run_found = sum(int(_s(r, "emails_found") or 0) for r in runs)

    apollo_sourced = sum(
        1 for e, s in source_of.items()
        if s not in FREE_SOURCES and s != "alumni_input"
    )

    # Headline rate: precise per-run data once `Runs` has history, else the
    # Alumni-tab tally (which covers every lookup we've ever made).
    find_rate = _rate(run_found, run_attempts) if run_attempts else _rate(alum_resolved, alum_attempted)
    deliverable_rate = _rate(len(delivered), len(contacted))

    apollo = {
        "find_rate": find_rate,
        "basis": "runs" if run_attempts else "alumni_tab",
        "attempted": run_attempts or alum_attempted,
        "found": run_found or alum_resolved,
        "lookup_breakdown": [
            {"label": "Email found", "count": alum_resolved, "state": "good"},
            {"label": "No email available", "count": alum_notfound, "state": "critical"},
            {"label": "Not looked up yet", "count": alum_pending, "state": "pending"},
        ],
        "alumni_rows": len(alumni),
        "discovery_leads": apollo_sourced,
        "runs_logged": len(runs),
        # A found address that bounces was still the wrong address — this is the
        # accuracy half of "can Apollo actually find people".
        "bounced": len(bounced),
        "bounce_rate": _rate(len(bounced), len(contacted)),
        "deliverable_rate": deliverable_rate,
        # Found *and* it reached a real mailbox — the end-to-end yield of one
        # Apollo credit.
        "usable_rate": (
            find_rate * deliverable_rate
            if find_rate is not None and deliverable_rate is not None
            else None
        ),
    }

    # ---- funnel (initial outreach only, so it reads monotonically) ----
    funnel = [
        {"stage": "Leads sourced", "count": len(leads)},
        {"stage": "Emails drafted", "count": len(initial)},
        {"stage": "Emails sent", "count": len(sent_initial)},
        {"stage": "Delivered", "count": len(delivered)},
        {"stage": "Replied", "count": responses},
    ]
    for i, stage in enumerate(funnel):
        prev = funnel[i - 1]["count"] if i else None
        stage["from_prev"] = _rate(stage["count"], prev) if prev else None
        stage["from_top"] = _rate(stage["count"], funnel[0]["count"])

    # ---- deliverability ----
    real_errors = Counter()
    skipped = 0
    for d in drafts:
        err = _s(d, "send_error")
        if not err:
            continue
        if err.lower().startswith(SKIP_PREFIX):
            skipped += 1
        else:
            real_errors[_clean_error(err)[:120]] += 1
    attempted_sends = len(sent_all) + sum(real_errors.values())

    # ---- segments: sent / delivered / replied, sliced by a key ----
    def segment_rows(key_fn) -> list[dict]:
        sent_by, delivered_by, reply_by, bounce_by = Counter(), Counter(), Counter(), Counter()
        for email in contacted:
            k = key_fn(email)
            if k is None:
                continue
            sent_by[k] += 1
            if email in bounced:
                bounce_by[k] += 1
            else:
                delivered_by[k] += 1
            if email in human_replied:
                reply_by[k] += 1
        return [
            {
                "label": k,
                "sent": n,
                "delivered": delivered_by.get(k, 0),
                "bounced": bounce_by.get(k, 0),
                "replies": reply_by.get(k, 0),
                "reply_rate": _rate(reply_by.get(k, 0), delivered_by.get(k, 0)),
                "bounce_rate": _rate(bounce_by.get(k, 0), n),
            }
            for k, n in sent_by.most_common()
        ]

    by_source = segment_rows(lambda e: source_of.get(e, "unknown"))
    by_audience = segment_rows(
        lambda e: "UIUC alumni" if alum_flag.get(e) else "Non-alumni"
    )

    template_counts = Counter(_s(d, "template_used") or "unknown" for d in sent_all)
    templates = [
        {"label": k.replace("_", " ").capitalize(), "count": v}
        for k, v in template_counts.most_common()
    ]

    # ---- reach ----
    companies = Counter()
    industries = Counter()
    for email in contacted:
        lead = lead_by_email.get(email)
        if not lead:
            continue
        company = _s(lead, "company")
        if company:
            companies[company] += 1
        industry = _s(lead, "industry")
        if industry:
            industries[industry.title()] += 1

    # ---- runway: how much of the Alumni bench is left to work through ----
    # Scoped to the Alumni tab on purpose. It is the hand-curated, finite list —
    # Apollo discovery can always search more, so only this side can run dry.
    known_emails = set(lead_by_email)
    bench_ready = len(alumni_emails - known_emails)
    bench_unlooked = alum_pending
    projected = bench_ready + int(bench_unlooked * (find_rate or 0.0))
    recent_pace = _rate(last_30, min(22, max(1, weekdays_in_window))) or 0.0
    pace = recent_pace if recent_pace > 0 else float(daily_target)

    runway = {
        "bench_ready": bench_ready,
        "bench_unlooked": bench_unlooked,
        "projected_contacts": projected,
        "daily_pace": round(pace, 1),
        "business_days_left": round(projected / pace, 1) if pace else None,
        # Initial outreach only: unapproved follow-up drafts are leftovers from
        # when follow-ups were enabled, not a queue anyone is working.
        "awaiting_approval": sum(
            1 for d in initial
            if not _truthy(d.get("approved")) and not _s(d, "sent_at")
        ),
        "approved_unsent": sum(
            1 for d in initial
            if _truthy(d.get("approved")) and not _s(d, "sent_at")
        ),
        "stale_follow_up_drafts": sum(
            1 for d in follow_ups
            if not _truthy(d.get("approved")) and not _s(d, "sent_at")
        ),
    }

    # ---- current campaign (the term we're sourcing for right now) ----
    campaign_name, campaign_start = campaign_window()
    try:
        start_date = date.fromisoformat(campaign_start)
    except ValueError:
        start_date = today
    campaign = _campaign_slice(
        contacted_dates, start_date, alum_flag, bounced, auto_replied,
        human_replied, interested, source_of,
    )
    campaign["name"] = campaign_name
    campaign["target_non_alumni_share"] = 1.0 - _alumni_share_setting()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "campaign": campaign,
        "window": {
            "first_send": first_send.isoformat() if first_send else None,
            "last_send": last_send.isoformat() if last_send else None,
            "weekdays": weekdays_in_window,
        },
        "headline": {
            "emails_sent": len(sent_all),
            "people_reached": len(contacted),
            "companies_reached": len(companies),
            "leads_sourced": len(leads),
            "drafts_written": len(drafts),
            "follow_ups_sent": len(sent_follow_ups),
            "last_7": last_7,
            "prior_7": prior_7,
            "last_30": last_30,
            "active_days": active_days,
            "avg_per_active_day": round(len(sent_all) / active_days, 1) if active_days else 0,
            "day_coverage": _rate(active_days, weekdays_in_window),
            "alumni_share": _rate(sent_alumni, len(contacted)),
        },
        "reply": {
            # tracked=False means nothing has ever been recorded, so every rate
            # below is unknown rather than zero. The UI must say so.
            "tracked": bool(reply_by_email) or bool(responders),
            "responses": responses,
            "rate": _rate(responses, len(delivered)),
            "interested": len(interested),
            "interested_rate": _rate(len(interested), len(delivered)),
            "auto_replies": len(auto_replied),
            "engaged": len(engaged),
            "engagement_rate": _rate(len(engaged), len(delivered)),
            "sentiment": [
                {"label": k, "count": v}
                for k, v in sorted(sentiment.items(), key=lambda kv: -kv[1])
            ],
            "by_audience": by_audience,
            "by_source": by_source,
        },
        "deliverability": {
            "sent": len(contacted),
            "delivered": len(delivered),
            "bounced": len(bounced),
            "bounce_rate": _rate(len(bounced), len(contacted)),
            "delivered_rate": deliverable_rate,
        },
        "apollo": apollo,
        "funnel": funnel,
        "timeline": {"weekly": weekly, "daily": daily},
        "quality": {
            "approval_rate": _rate(len(approved_initial), len(initial)),
            "send_success_rate": _rate(len(sent_all), attempted_sends),
            "failed_sends": sum(real_errors.values()),
            "skipped_duplicates": skipped,
            "errors": _top(real_errors, 5),
            "suppressed": len(suppression),
        },
        "mix": {
            "sources": by_source,
            "templates": templates,
            "industries": _top(industries, 8),
            "companies": _top(companies, 12),
        },
        "runway": runway,
    }


def collect(sheets, daily_target: int = 15) -> dict:
    """Pull every tab the metrics need and compute. Missing tabs read as empty."""
    import gspread

    def rows(tab: str) -> list[dict]:
        try:
            return sheets.book.worksheet(tab).get_all_records()
        except gspread.WorksheetNotFound:
            return []

    return compute(
        leads=rows("Leads"),
        drafts=rows("Drafts"),
        alumni=rows("Alumni"),
        suppression=rows("Suppression"),
        hot_leads=rows("Hot Leads"),
        runs=rows("Runs"),
        replies=rows("Replies"),
        daily_target=daily_target,
    )

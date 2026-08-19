"""Gemini-powered email personalization.

Leads are drafted in batches of DRAFT_BATCH_SIZE per Gemini call (see
`draft_many`) — the free tier's daily request quota is the binding constraint,
and one call per lead exhausted it mid-batch, losing drafts whose Apollo credits
had already been spent. We feed each contact its profile, chosen template, and
1-2 matched past projects, then ask for one JSON object per contact with
`subject` and `body`. Anything the batch drops is retried individually. The model is instructed to preserve CUBE's
voice (lifted from the manual outreach guide's McKesson example) and to
edit only the salutation, the credibility line, and the industry mention.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Iterable

from .env import env_int, env_str
from .llm import generate_json
from .models import Draft, Lead, PastProject, TemplateType
from .templates import (
    CUBE_MEMBER,
    FOLLOW_UP,
    SUBJECT_TEMPLATE,
    TARGET_TERM,
    TEMPLATES,
    render_footer,
)

log = logging.getLogger(__name__)

# Link to the CUBE info packet, appended to first-outreach emails (not follow-ups).
# Replaces the old PDF attachment.
#
# The slug still says "fall2026" and that is deliberate: the packet's contents
# are unchanged for Spring 2027, and the tinyurl is a redirect the team owns, so
# re-pointing it later updates every email already sent without touching code.
# Set PACKET_URL to override (e.g. if a genuinely different packet ships).
PACKET_URL = env_str("PACKET_URL", "http://tinyurl.com/cube-fall2026-packet")


def _check_packet_url() -> None:
    """Refuse to send outreach with no packet link.

    Deliberately does NOT check whether the URL mentions the current term — the
    link is a stable redirect reused across cycles, so matching on the slug only
    produced false alarms.
    """
    if not PACKET_URL.strip():
        log.warning("PACKET_URL is empty — outreach will go out with no info-packet link")


def _packet_line() -> str:
    return (
        "\n\nHere's our info packet with more about CUBE and our past work "
        f"if you'd like to take a look: {PACKET_URL}"
    )


DRAFT_MODEL = "gemini-2.5-flash"
# Contacts per model call. 5 keeps each response well inside the output limit
# while cutting daily Gemini requests by ~5x — the difference between fitting
# in the free tier's daily quota and losing half the batch to 429s.
DRAFT_BATCH_SIZE = max(1, env_int("DRAFT_BATCH_SIZE", 5))
DRAFT_SYSTEM = """You write cold outreach emails for CUBE Consulting, a student-run consulting group at the University of Illinois Urbana-Champaign (UIUC). Keep CUBE's voice: professional, warm, concise, and genuine — never salesy or stiff.

You are personalizing a base template. Keep its overall structure and signoff. Personalize ONLY these spots:

1. Salutation: keep "Hi {first_name}," unless there's a clear preferred-name signal.
2. {credibility_line}: ONE concise, specific sentence referencing a relevant CUBE past project, tied naturally to the contact's industry. Never exaggerate or name a client we did not match.
3. {industry}: the most natural one- or two-word phrasing of the contact's industry.
4. UIUC alumni ONLY: open the SECOND paragraph with a brief, natural acknowledgment of the shared UIUC connection, written as a COMPLETE, grammatical sentence that leads smoothly into the rest of the paragraph. The "fellow Illini" subject must be the writer/CUBE — never the company.
   - Good: "As a fellow Illini, I wanted to reach out personally. CUBE is a student-run consulting group that ..."
   - Bad:  "As a fellow Illini, CUBE is ..."  (attaches the phrase to the company)
   - Bad:  "As a fellow Illini, if you're open to it ..."  (tacked onto the closing)
   Never add any UIUC/Illini mention for non-alumni.
5. Former CUBE members: their template already greets them as a fellow CUBE alum who knows our work. Keep that warm, peer-to-peer tone, do NOT explain what CUBE is, and do NOT add a separate "fellow Illini" line (the CUBE-alum greeting already covers the shared connection).

Rules:
- The semester named in the template (e.g. "Spring 2027") is already filled in and is a hard fact: keep it EXACTLY as written, in the same places. Never change it, drop it, or replace it with "this semester"/"next semester".
- Keep the body under 200 words and tight; every sentence must read naturally with correct grammar (especially where the alumni line joins the paragraph).
- Do NOT invent facts, add paragraphs, signoffs, or postscripts.
- Output strict JSON with no markdown fences. For a single contact: {"subject": "...", "body": "..."}. When several numbered contacts are given, return {"drafts": [{"id": <contact number>, "subject": "...", "body": "..."}, ...]} with exactly one entry per contact and nothing omitted.
"""


class Drafter:
    def __init__(self, model: str = DRAFT_MODEL) -> None:
        self.model = model

    def draft(
        self,
        lead: Lead,
        template_type: TemplateType,
        matched_projects: list[PastProject],
        sender_name: str,
        sender_phone: str,
        footer: str,
    ) -> Draft:
        base_template = CUBE_MEMBER if lead.is_cube_member else TEMPLATES[template_type]
        # {term} is filled here, not by Gemini: the semester we're sourcing for
        # is the one fact in this email that must never be reworded.
        base_template = base_template.replace("{term}", TARGET_TERM)
        matches_block = "\n".join(
            f"- {p.client} ({p.semester}): keywords={', '.join(p.keywords)}; "
            f"deliverables={p.deliverables[:300]}"
            for p in matched_projects
        ) or "(none matched — write a generic credibility sentence drawing on CUBE's range)"

        prompt = f"""Contact:
- Name: {lead.name}
- First name: {lead.first_name()}
- Title: {lead.title or 'unknown'}
- Company: {lead.company}
- Industry: {lead.industry or 'unknown'}
- Location: {lead.location or 'unknown'}
- LinkedIn: {lead.linkedin or 'unknown'}
- UIUC alum: {lead.is_uiuc_alum}
- Former CUBE member: {lead.is_cube_member}

Matched past CUBE projects (use ONE of these for the credibility line):
{matches_block}

Base template (fill placeholders; do not restructure):
---
SUBJECT: {SUBJECT_TEMPLATE.format(company=lead.company, term=TARGET_TERM)}
---
{base_template}
---

Sender values to substitute:
- {{your_name}} -> {sender_name}
- {{your_number}} -> {sender_phone}
- {{contact_name}} -> {lead.first_name()}
- {{company}} -> {lead.company}
- {{industry}} -> (write naturally based on Industry above)
- {{credibility_line}} -> (write ONE sentence referencing a matched past project, OR a generic line if none matched)

Return JSON only."""

        payload = generate_json(
            model=self.model,
            system=DRAFT_SYSTEM,
            prompt=prompt,
            max_tokens=900,
        )
        return Draft(
            lead_email=lead.email,
            prepared_at=datetime.now(timezone.utc),
            template_used=template_type,
            subject=payload["subject"],
            body=payload["body"] + _packet_line() + footer,
        )

    def _contact_block(self, index: int, lead: Lead, template_type: TemplateType,
                       matched_projects: list[PastProject], sender_name: str,
                       sender_phone: str) -> str:
        """One numbered contact + its filled template, for a batched request."""
        base_template = CUBE_MEMBER if lead.is_cube_member else TEMPLATES[template_type]
        base_template = base_template.replace("{term}", TARGET_TERM)
        matches_block = "\n".join(
            f"- {p.client} ({p.semester}): keywords={', '.join(p.keywords)}; "
            f"deliverables={p.deliverables[:300]}"
            for p in matched_projects
        ) or "(none matched — write a generic credibility sentence drawing on CUBE's range)"
        return f"""### CONTACT {index}
- Name: {lead.name}
- First name: {lead.first_name()}
- Title: {lead.title or 'unknown'}
- Company: {lead.company}
- Industry: {lead.industry or 'unknown'}
- Location: {lead.location or 'unknown'}
- UIUC alum: {lead.is_uiuc_alum}
- Former CUBE member: {lead.is_cube_member}

Matched past CUBE projects (use ONE for the credibility line):
{matches_block}

SUBJECT for contact {index}: {SUBJECT_TEMPLATE.format(company=lead.company, term=TARGET_TERM)}

Base template for contact {index} (fill placeholders; do not restructure):
---
{base_template}
---
Substitutions for contact {index}: {{your_name}} -> {sender_name}; {{your_number}} -> {sender_phone}; {{contact_name}} -> {lead.first_name()}; {{company}} -> {lead.company}
"""

    def draft_many(
        self,
        items: list[tuple[Lead, TemplateType, list[PastProject]]],
        sender_name: str,
        sender_phone: str,
        footer: str,
    ) -> dict[str, Draft]:
        """Draft a whole batch of leads in ONE model call, keyed by lead email.

        Gemini's free tier caps daily requests, and one call per lead burned
        through it fast enough that a 15-lead batch could lose most of its drafts
        after their Apollo credits were already spent. Batching cuts the request
        count by DRAFT_BATCH_SIZE and makes the daily run fit comfortably inside
        the quota. Any contact the model omits is simply absent from the result,
        and the caller retries it.
        """
        if not items:
            return {}
        blocks = "\n".join(
            self._contact_block(i, lead, tmpl, matches, sender_name, sender_phone)
            for i, (lead, tmpl, matches) in enumerate(items, start=1)
        )
        prompt = (
            f"Write a personalized email for EACH of the {len(items)} contacts below.\n"
            f"Treat every contact independently — never mix one company's details into another's.\n\n"
            f"{blocks}\n"
            f'Return JSON only: {{"drafts": [{{"id": 1, "subject": "...", "body": "..."}}, ...]}} '
            f"with exactly one object per contact, `id` matching the CONTACT number."
        )
        payload = generate_json(
            model=self.model,
            system=DRAFT_SYSTEM,
            prompt=prompt,
            # ~700 tokens of email per contact, plus JSON overhead.
            max_tokens=800 * len(items) + 500,
        )
        out: dict[str, Draft] = {}
        for entry in payload.get("drafts") or []:
            try:
                idx = int(entry["id"]) - 1
                lead, tmpl, _ = items[idx]
                subject, body = entry["subject"], entry["body"]
            except (KeyError, ValueError, TypeError, IndexError):
                log.warning("Skipping malformed batch draft entry: %r", str(entry)[:120])
                continue
            if not subject or not body:
                continue
            out[lead.email.lower()] = Draft(
                lead_email=lead.email,
                prepared_at=datetime.now(timezone.utc),
                template_used=tmpl,
                subject=subject,
                body=body + _packet_line() + footer,
            )
        return out

    def draft_follow_up(
        self,
        lead: Lead,
        original_message_id: str,
        sender_name: str,
        footer: str,
    ) -> Draft:
        body = FOLLOW_UP.format(contact_name=lead.first_name(), company=lead.company, your_name=sender_name)
        # Follow-ups keep the original subject prefixed with "Re:" so Gmail threads them.
        original_subject = SUBJECT_TEMPLATE.format(company=lead.company, term=TARGET_TERM)
        return Draft(
            lead_email=lead.email,
            prepared_at=datetime.now(timezone.utc),
            template_used=TemplateType.BUSINESS,  # template label doesn't matter for follow-ups
            subject=f"Re: {original_subject}",
            body=body + footer,
            is_follow_up=True,
            in_reply_to=original_message_id,
        )


def make_footer() -> str:
    return render_footer(
        org_name=env_str("ORG_NAME", "CUBE Consulting"),
        # These two are legally load-bearing (CAN-SPAM): a blank physical
        # address or unsubscribe address on real outreach is a compliance
        # problem, so they must never degrade to "".
        address=env_str(
            "ORG_PHYSICAL_ADDRESS", "707 S 4th St, APT 1006A, Champaign IL 61820"
        ),
        unsubscribe_mailto=env_str(
            "UNSUBSCRIBE_MAILTO", "unsubscribe@cubeconsulting.org"
        ),
    )


def draft_for_leads(
    leads: Iterable[Lead],
    template_router,
    past_index,
    sender_name: str,
    sender_phone: str,
) -> list[tuple[Lead, Draft]]:
    """Draft every lead, in batches of DRAFT_BATCH_SIZE per model call.

    Leads the batch call drops are retried once on their own, so a single
    malformed entry costs one email rather than the whole batch. Order is
    preserved: the caller writes Leads and Drafts rows in step.
    """
    _check_packet_url()
    drafter = Drafter()
    footer = make_footer()

    items: list[tuple[Lead, TemplateType, list[PastProject]]] = []
    for lead in leads:
        tmpl = template_router.route(lead.industry)
        query = f"{lead.industry or ''} {lead.title or ''} {lead.company or ''}"
        items.append((lead, tmpl, past_index.top_matches(query, k=2)))

    drafts: dict[str, Draft] = {}
    for start in range(0, len(items), DRAFT_BATCH_SIZE):
        chunk = items[start:start + DRAFT_BATCH_SIZE]
        try:
            drafts.update(drafter.draft_many(chunk, sender_name, sender_phone, footer))
        except Exception as exc:  # one bad batch must not kill the rest
            log.exception("Batch drafting failed for %d leads: %s", len(chunk), exc)

    # Retry anyone the batch missed, one at a time.
    for lead, tmpl, matches in items:
        if lead.email.lower() in drafts:
            continue
        try:
            drafts[lead.email.lower()] = drafter.draft(
                lead, tmpl, matches, sender_name, sender_phone, footer
            )
        except Exception as exc:
            log.error("Drafting failed for %s: %s", lead.email, exc)

    return [(lead, drafts[lead.email.lower()])
            for lead, _, _ in items if lead.email.lower() in drafts]

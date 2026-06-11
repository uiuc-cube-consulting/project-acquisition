"""Gemini-powered email personalization.

Each lead gets exactly one Gemini call. We feed it the lead's profile, the
chosen template, and 1-2 matched past projects, then ask for a JSON object
with `subject` and `body`. The model is instructed to preserve CUBE's
voice (lifted from the manual outreach guide's McKesson example) and to
edit only the salutation, the credibility line, and the industry mention.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Iterable

from .llm import generate_json
from .models import Draft, Lead, PastProject, TemplateType
from .templates import (
    FOLLOW_UP,
    SUBJECT_TEMPLATE,
    TEMPLATES,
    render_footer,
)

log = logging.getLogger(__name__)

DRAFT_MODEL = "gemini-2.5-flash"
DRAFT_SYSTEM = """You write cold outreach emails for CUBE Consulting, a student-run consulting group at the University of Illinois Urbana-Champaign.

You are personalizing a base template. Rules:
- Preserve the overall structure and CUBE's voice exactly. The base template is the source of truth.
- Personalize ONLY:
  1. The salutation if the contact has a clear preferred-name signal (otherwise keep "Hi {first_name},").
  2. The `{credibility_line}` placeholder — ONE concise sentence referencing a relevant CUBE past project. Use the contact's industry naturally. Do NOT exaggerate. Do NOT name past clients we did not match.
  3. The `{industry}` placeholder — the most natural phrasing of the contact's industry as one or two words.
- If the contact is a UIUC alumnus, add a single phrase acknowledging that ("As a fellow Illini, ..." or similar) at the start of the second paragraph. Do NOT add a UIUC mention if they did not attend.
- Keep total body under 200 words.
- Do NOT add new paragraphs, signoffs, or postscripts.
- Output strict JSON: {"subject": "...", "body": "..."} with no markdown fences.
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
        base_template = TEMPLATES[template_type]
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

Matched past CUBE projects (use ONE of these for the credibility line):
{matches_block}

Base template (fill placeholders; do not restructure):
---
SUBJECT: {SUBJECT_TEMPLATE.format(company=lead.company)}
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
            body=payload["body"] + footer,
        )

    def draft_follow_up(
        self,
        lead: Lead,
        original_message_id: str,
        sender_name: str,
        footer: str,
    ) -> Draft:
        body = FOLLOW_UP.format(contact_name=lead.first_name(), company=lead.company, your_name=sender_name)
        # Follow-ups keep the original subject prefixed with "Re:" so Gmail threads them.
        original_subject = SUBJECT_TEMPLATE.format(company=lead.company)
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
        org_name=os.environ.get("ORG_NAME", "CUBE Consulting"),
        address=os.environ.get(
            "ORG_PHYSICAL_ADDRESS", "707 S 4th St, APT 1006A, Champaign IL 61820"
        ),
        unsubscribe_mailto=os.environ.get(
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
    drafter = Drafter()
    footer = make_footer()
    out: list[tuple[Lead, Draft]] = []
    for lead in leads:
        tmpl = template_router.route(lead.industry)
        query = f"{lead.industry or ''} {lead.title or ''} {lead.company or ''}"
        matches = past_index.top_matches(query, k=2)
        try:
            draft = drafter.draft(lead, tmpl, matches, sender_name, sender_phone, footer)
        except Exception as exc:  # don't let one bad lead kill the batch
            log.exception("Drafting failed for %s: %s", lead.email, exc)
            continue
        out.append((lead, draft))
    return out

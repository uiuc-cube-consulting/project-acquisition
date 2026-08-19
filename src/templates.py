"""Outreach email templates, copied verbatim from Outreach Message Template.docx.

The drafter (src/draft.py) hands these to Gemini as the structural starting
point. Gemini personalizes the salutation, the industry-specific line, and the
past-project credibility line, while preserving CUBE's voice.

Placeholders:
  {contact_name}, {company}, {industry}, {your_name}, {your_number},
  {credibility_line} (1-2 sentences referencing a matched past project),
  {term} (the semester we are sourcing for — substituted in Python, not by the
  model, so it can never be paraphrased or dropped)
"""
from __future__ import annotations

import os

from .env import env_str
from .models import TemplateType

# The semester these emails are sourcing projects FOR. Outreach runs a semester
# ahead: we pitch Spring 2027 during the Fall 2026 cycle. One env var flips the
# whole campaign for the next cycle.
TARGET_TERM = env_str("TARGET_TERM", "Spring 2027")

SUBJECT_TEMPLATE = "{term} collaboration between {company} and CUBE Consulting at UIUC"


BUSINESS = """Hi {contact_name},

I hope you're doing well. My name is {your_name}, and I'm the President of CUBE Consulting at the University of Illinois Urbana-Champaign.

CUBE is a student-run consulting group that partners with organizations on strategic and analytical projects each semester, and we're currently selecting the organizations we'll partner with in {term}. Our consultants have worked with high-growth startups and Fortune 500 Companies on initiatives such as market strategy, data analytics, and operational optimization. {credibility_line} We'd love to explore the opportunity to work with {company} on initiatives that drive innovation and strategic insights in {industry}.

If you're open to it, I'd love to schedule a short call to share more about our work and explore whether a {term} project could be a fit for {company}. You can reply here or reach me directly at {your_number}.

Thank you for your time,
{your_name}"""


HYBRID_PRODUCT = """Hi {contact_name},

I hope you're doing well. My name is {your_name}, and I'm the President of CUBE Consulting at the University of Illinois Urbana-Champaign.

CUBE is a student-run consulting group that partners with organizations on strategy and optimization projects each semester, and we're currently selecting the organizations we'll partner with in {term}. Our consultants have worked with high-growth startups and Fortune 500 Companies on initiatives such as product ideation and development, UI/UX design, and GTM strategy. {credibility_line} We'd love to explore the opportunity to work with {company} on initiatives that drive innovation and strategic insights in {industry}.

If you're open to it, I'd love to schedule a short call to share more about our work and explore whether a {term} project could be a fit for {company}. You can reply here or reach me directly at {your_number}.

Thank you for your time,
{your_name}"""


TECHNICAL_SOFTWARE = """Hi {contact_name},

I hope you're doing well. My name is {your_name}, and I'm the President of CUBE Consulting at the University of Illinois Urbana-Champaign.

CUBE is a student-run consulting group that partners with organizations on engineering and optimization projects each semester, and we're currently selecting the organizations we'll partner with in {term}. Our consultants have worked with high-growth startups and Fortune 500 Companies on initiatives such as AI integration, data platforms, and software development. {credibility_line} We'd love to explore the opportunity to work with {company} on initiatives that drive innovation and strategic insights in {industry}.

If you're open to it, I'd love to schedule a short call to share more about our work and explore whether a {term} project could be a fit for {company}. You can reply here or reach me directly at {your_number}.

Thank you for your time,
{your_name}"""


TECHNICAL_ENGINEERING = """Hi {contact_name},

I hope you're doing well. My name is {your_name}, and I'm the President of CUBE Consulting at the University of Illinois Urbana-Champaign.

CUBE is a student-run consulting group that partners with organizations on engineering and optimization projects each semester, and we're currently selecting the organizations we'll partner with in {term}. Our consultants have worked with high-growth startups and Fortune 500 Companies on initiatives such as system design, hardware prototyping, and CAD simulations. {credibility_line} We'd love to explore the opportunity to work with {company} on initiatives that drive innovation and strategic insights in {industry}.

If you're open to it, I'd love to schedule a short call to share more about our work and explore whether a {term} project could be a fit for {company}. You can reply here or reach me directly at {your_number}.

Thank you for your time,
{your_name}"""


CUBE_MEMBER = """Hi {contact_name},

It's great to connect with a fellow CUBE alum — I'm {your_name}, the current President of CUBE Consulting at UIUC.

Since you know firsthand the kind of work our teams take on, I'll keep this short. {credibility_line} I'd love to explore having a CUBE team partner with {company} in {term} on {industry} initiatives.

Would you be open to a quick call to catch up and hear what CUBE is working on now? You can reply here or reach me at {your_number}.

Thank you,
{your_name}"""


FOLLOW_UP = """Hi {contact_name},

Just floating this back to the top of your inbox in case it got buried. Happy to share more about CUBE's work with {company} at a time that works for you, or to send over a brief overview deck.

Thank you,
{your_name}"""


TEMPLATES: dict[TemplateType, str] = {
    TemplateType.BUSINESS: BUSINESS,
    TemplateType.HYBRID_PRODUCT: HYBRID_PRODUCT,
    TemplateType.TECHNICAL_SOFTWARE: TECHNICAL_SOFTWARE,
    TemplateType.TECHNICAL_ENGINEERING: TECHNICAL_ENGINEERING,
}


def render_footer(org_name: str, address: str, unsubscribe_mailto: str) -> str:
    return (
        f"\n\n--\n{org_name} | {address}\n"
        f"To unsubscribe, reply with \"unsubscribe\" or email {unsubscribe_mailto}."
    )

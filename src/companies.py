"""Company-level dedupe: one company, one conversation.

Dedupe used to be per-person (email address + LinkedIn URL), so nothing stopped
the pipeline from working through eleven people at PwC, five at Microsoft and
five at Deloitte. From a prospect's side that reads as spam, and it wastes both
Apollo credits and daily send slots on an account we have already pitched.

Two identifiers are tracked, because neither alone is enough:

  normalized name  works BEFORE an Apollo reveal, so we can drop a candidate
                   without spending a credit on them. Legal suffixes and country
                   tags are stripped, so "RSM US LLP", "RSM US" and "RSM" all
                   collapse to the same key.
  email domain     works only after the email is known, but it catches the cases
                   a name never will — "PwC" vs "PricewaterhouseCoopers" both
                   land on pwc.com. Free providers are ignored, since
                   gmail.com identifies a person, not an employer.

The running list lives in the Sheet's `Companies` tab, so it is auditable and
hand-editable: adding a row by hand permanently blocks that company.
"""
from __future__ import annotations

import re

# Dropped from the end of a company name. These distinguish legal entities, not
# businesses — "Acme" and "Acme, Inc." are the same prospect to us.
LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "l.l.c", "ltd", "limited", "corp", "corporation",
    "co", "company", "llp", "lp", "plc", "gmbh", "ag", "nv", "bv", "sa", "sas",
    "sarl", "srl", "spa", "pty", "pvt", "aps", "oy", "ab", "kk", "kg", "pc", "pllc",
}

# Also dropped from the end: regional tags on the same global brand.
REGION_SUFFIXES = {"us", "usa", "uk", "na", "emea", "apac", "global", "international"}

# Generic organisation descriptors: "Huron Consulting Group" is the same
# prospect as "Huron". Stripped only when what remains is still distinctive
# (see MIN_CORE_LEN) so we don't collapse "Apex Systems" into "Apex Capital".
DESCRIPTOR_SUFFIXES = {
    "consulting", "consultants", "consultancy", "group", "holdings", "holding",
    "partners", "advisors", "advisers", "solutions", "services", "associates",
    "technologies", "technology", "systems", "industries", "enterprises",
}

# A stripped core shorter than this stays untouched — short names are ambiguous.
MIN_CORE_LEN = 5

# An address at one of these says nothing about the employer.
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "protonmail.com", "proton.me", "gmx.com", "mail.com", "zoho.com",
    "comcast.net", "sbcglobal.net", "verizon.net", "att.net",
}


def normalize_company(name: str | None) -> str:
    """A stable key for a company name, or "" if there's nothing usable.

    >>> normalize_company("RSM US LLP") == normalize_company("RSM")
    True
    >>> normalize_company("The Boeing Company") == normalize_company("Boeing")
    True
    """
    if not name:
        return ""
    text = str(name).lower().strip()
    text = text.replace("&", " and ")
    # Drop a trailing ownership clause: "HealthScape Advisors, a Chartis Company".
    text = re.sub(r",\s*(?:an?|part of)\s+.*$", " ", text)
    # Drop anything parenthetical ("Acme (formerly Foo)") and punctuation.
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"[^\w\s-]", " ", text)
    text = re.sub(r"[\s-]+", " ", text).strip()
    tokens = text.split()
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    # Re-join dotted abbreviations that punctuation-stripping blew apart, so
    # "BP p.l.c." -> ["bp","p","l","c"] -> ["bp","plc"] and peels like any suffix.
    tail: list[str] = []
    while len(tokens) > 1 and len(tokens[-1]) == 1:
        tail.insert(0, tokens.pop())
    if tail:
        joined = "".join(tail)
        tokens.append(joined) if joined in LEGAL_SUFFIXES or joined in REGION_SUFFIXES else tokens.extend(tail)
    # Peel suffixes off the end repeatedly: "rsm us llp" -> "rsm us" -> "rsm".
    while len(tokens) > 1 and (tokens[-1] in LEGAL_SUFFIXES or tokens[-1] in REGION_SUFFIXES):
        tokens.pop()
    # Then the generic descriptors, but only while the remaining core stays
    # long enough to identify a company on its own.
    while len(tokens) > 1 and tokens[-1] in DESCRIPTOR_SUFFIXES:
        core = " ".join(tokens[:-1])
        if len(core.replace(" ", "")) < MIN_CORE_LEN:
            break
        tokens.pop()
    return " ".join(tokens)


def email_domain(email: str | None) -> str:
    """The employer domain from an email, or "" for free/personal providers."""
    if not email or "@" not in str(email):
        return ""
    domain = str(email).rsplit("@", 1)[-1].strip().lower()
    if not domain or domain in FREE_EMAIL_DOMAINS:
        return ""
    # Treat mail subdomains as the parent ("mail.acme.com" -> "acme.com").
    parts = domain.split(".")
    if len(parts) > 2 and parts[0] in ("mail", "email", "smtp", "corp", "us"):
        domain = ".".join(parts[1:])
    return domain


class CompanyRegistry:
    """The set of companies we've already contacted, by name key and by domain."""

    def __init__(self, names: set[str] | None = None, domains: set[str] | None = None):
        self.names: set[str] = set(names or ())
        self.domains: set[str] = set(domains or ())

    def __len__(self) -> int:
        return len(self.names)

    def seen(self, company: str | None = None, email: str | None = None) -> bool:
        """True if this company has already been contacted, by either identifier."""
        key = normalize_company(company)
        if key and key in self.names:
            return True
        domain = email_domain(email)
        return bool(domain and domain in self.domains)

    def claim(self, company: str | None = None, email: str | None = None) -> None:
        """Mark a company as taken, so nothing later in the same run picks it."""
        key = normalize_company(company)
        if key:
            self.names.add(key)
        domain = email_domain(email)
        if domain:
            self.domains.add(domain)

    @classmethod
    def from_rows(cls, lead_rows: list[dict], company_rows: list[dict] | None = None):
        """Build from the Leads tab plus the `Companies` tab.

        Every lead row counts: `prepare` only writes a lead once it has a draft,
        so a row means we are committed to emailing that company.
        """
        reg = cls()
        for row in lead_rows:
            reg.claim(row.get("company"), row.get("email"))
        for row in company_rows or []:
            reg.claim(row.get("company"), None)
            domain = str(row.get("domain") or "").strip().lower()
            if domain:
                reg.domains.add(domain)
        return reg

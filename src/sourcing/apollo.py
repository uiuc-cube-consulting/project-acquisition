"""Apollo People Search wrapper.

API docs: https://docs.apollo.io/reference/people-search

Two phases, on purpose, to conserve Apollo credits:

  1. `search_candidates` hits `/v1/mixed_people/search` (People Search API —
     requires a *master* API key on a paid plan). The search does NOT consume
     credits and returns no emails; we turn each result into a lightweight
     `Candidate` (no email yet).
  2. After the orchestrator scores + selects the few candidates we'll actually
     email, `bulk_reveal` resolves their emails via `/v1/people/bulk_match`
     (Bulk People Enrichment, up to 10 per call). THIS is what costs credits —
     one per matched person — so we only spend on leads we actually contact.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml

from ..models import Lead

log = logging.getLogger(__name__)

APOLLO_BASE = "https://api.apollo.io/v1"
UIUC_SCHOOLS = {
    "university of illinois urbana-champaign",
    "university of illinois at urbana-champaign",
    "university of illinois",
}


def _is_uiuc(schools: Iterable[str]) -> bool:
    """True if any school string points to UIUC.

    Apollo's education data is sourced from LinkedIn, so this is effectively the
    LinkedIn cross-check. We match the Urbana-Champaign campus specifically (not
    UIC / UIS) plus the "UIUC" shorthand, tolerant of comma/hyphen variants.
    """
    for s in schools:
        t = (s or "").lower()
        if t.strip() in UIUC_SCHOOLS:
            return True
        if "illinois" in t and any(c in t for c in ("urbana", "champaign", "uiuc")):
            return True
        if "uiuc" in t.split():
            return True
    return False


class ApolloClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ["APOLLO_API_KEY"]
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Cache-Control": "no-cache",
                "Content-Type": "application/json",
                "X-Api-Key": self.api_key,
            }
        )

    def search_people(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        r = self.session.post(f"{APOLLO_BASE}/mixed_people/search", json=params, timeout=30)
        r.raise_for_status()
        return r.json().get("people", [])

    def bulk_match(
        self, people: list[dict[str, Any]], reveal_personal_emails: bool = False
    ) -> list[dict[str, Any] | None]:
        """Reveal emails for up to 10 people in one call (Apollo Bulk People
        Enrichment, /people/bulk_match). Costs 1 credit per matched person.
        Returns enriched person dicts aligned to `people` (None where no match).

        reveal_personal_emails defaults to False — work emails are what we want
        for B2B outreach, and personal reveals can draw a separate credit pool.
        """
        details: list[dict[str, Any]] = []
        for p in people:
            if p.get("id"):
                details.append({"id": p["id"]})
            elif p.get("linkedin_url"):
                details.append({"linkedin_url": p["linkedin_url"]})
            else:
                details.append({
                    "first_name": p.get("first_name"),
                    "last_name": p.get("last_name"),
                    "organization_name": (p.get("organization") or {}).get("name"),
                })
        payload = {"reveal_personal_emails": reveal_personal_emails, "details": details}
        r = self.session.post(f"{APOLLO_BASE}/people/bulk_match", json=payload, timeout=60)
        if r.status_code != 200:
            log.warning("Apollo bulk_match failed (%s): %s", r.status_code, r.text[:300])
            return [None] * len(people)
        return r.json().get("matches", [None] * len(people))


def _to_lead(person: dict[str, Any], source: str, assume_uiuc: bool = False) -> Lead | None:
    email = person.get("email")
    if not email or "email_not_unlocked" in email or "domain.com" in email:
        return None

    schools = _parse_schools(person)
    # assume_uiuc: came from a search already filtered to UIUC alumni, so trust
    # that even when Apollo's search payload omits the education array.
    is_uiuc = assume_uiuc or _is_uiuc(schools)

    org = person.get("organization") or {}
    industry = org.get("industry") or person.get("industry")
    location = _parse_location(person)

    return Lead(
        name=person.get("name") or " ".join(
            filter(None, [person.get("first_name"), person.get("last_name")])
        ),
        title=person.get("title"),
        company=org.get("name") or "",
        email=email,
        linkedin=person.get("linkedin_url"),
        industry=industry,
        location=location,
        company_stage=org.get("latest_funding_stage") or org.get("stage"),
        is_uiuc_alum=is_uiuc,
        schools=schools,
        source=source,
        date_added=datetime.now(timezone.utc),
    )


def load_profiles(path: str | Path = "config/search_profiles.yaml") -> list[dict[str, Any]]:
    return yaml.safe_load(Path(path).read_text())["profiles"]


def get_uiuc_profile(profiles: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The dedicated UIUC-alumni search profile, run every day as the primary source."""
    for p in profiles:
        if p.get("uiuc_only"):
            return p
    return None


def pick_profile_for_today(profiles: list[dict[str, Any]], day_index: int) -> dict[str, Any]:
    """Rotate the *secondary* (breadth) profiles by day. The UIUC profile is
    excluded here because it runs every day, not on rotation."""
    apollo_profiles = [
        p for p in profiles
        if p.get("source") != "cube_alumni_sheet" and not p.get("uiuc_only")
    ]
    return apollo_profiles[day_index % len(apollo_profiles)]


def _parse_location(person: dict[str, Any]) -> str | None:
    return (
        person.get("city")
        and person.get("state")
        and f"{person['city']}, {person['state']}"
    ) or person.get("location_name") or person.get("present_raw_address")


def _parse_schools(person: dict[str, Any]) -> list[str]:
    schools = []
    for ed in person.get("education") or []:
        school = (ed.get("school", {}) or {}).get("name") or ed.get("school_name")
        if school:
            schools.append(school)
    return schools


@dataclass
class Candidate:
    """A pre-reveal search hit. Carries everything scoring needs, but NOT a
    usable email yet — `reveal` unlocks that (and only then spends a credit)."""

    person: dict[str, Any]          # raw Apollo record, kept for the reveal call
    source: str
    enrich: bool
    name: str
    title: str | None
    company: str
    industry: str | None
    location: str | None
    company_stage: str | None
    linkedin: str | None
    apollo_id: str | None
    is_uiuc_alum: bool
    schools: list[str] = field(default_factory=list)
    score: float = 0.0


def _to_candidate(person: dict[str, Any], profile: dict[str, Any]) -> Candidate:
    org = person.get("organization") or {}
    schools = _parse_schools(person)
    return Candidate(
        person=person,
        source=profile["name"],
        enrich=bool(profile.get("enrich")),
        name=person.get("name") or " ".join(
            filter(None, [person.get("first_name"), person.get("last_name")])
        ),
        title=person.get("title"),
        company=org.get("name") or "",
        industry=org.get("industry") or person.get("industry"),
        location=_parse_location(person),
        company_stage=org.get("latest_funding_stage") or org.get("stage"),
        linkedin=person.get("linkedin_url"),
        apollo_id=person.get("id"),
        # uiuc_only profiles are pre-filtered to alumni, so trust that flag even
        # when the search payload omits the education array.
        is_uiuc_alum=bool(profile.get("uiuc_only")) or _is_uiuc(schools),
        schools=schools,
    )


def search_candidates(
    client: ApolloClient,
    profile: dict[str, Any],
    max_results: int = 50,
) -> list[Candidate]:
    """Search only (no credit spend). Returns pre-reveal Candidates."""
    people = client.search_people(profile["params"])
    log.info("Apollo returned %d people for profile %s", len(people), profile["name"])
    return [_to_candidate(p, profile) for p in people[:max_results]]


def bulk_reveal(client: ApolloClient, candidates: list[Candidate]) -> list[Lead | None]:
    """Reveal emails for candidates via Apollo bulk_match (10 per call, 1 credit
    per matched person). Returns Lead|None aligned to `candidates`."""
    out: list[Lead | None] = []
    for i in range(0, len(candidates), 10):
        chunk = candidates[i:i + 10]
        matches = client.bulk_match([c.person for c in chunk])
        for cand, person in zip(chunk, matches):
            out.append(
                _to_lead(person, source=cand.source, assume_uiuc=cand.is_uiuc_alum)
                if person else None
            )
    return out

"""Apollo People Search wrapper.

API docs: https://docs.apollo.io/reference/people-search

We hit `/v1/mixed_people/search` with a profile's params and return Leads.
Apollo's free tier of the API masks emails ("email_not_unlocked@..."); the
paid plan returns real emails. If we get a masked email back we call the
`/v1/people/match` endpoint with the LinkedIn URL to force email reveal.
"""
from __future__ import annotations

import logging
import os
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

    def enrich_person(self, person: dict[str, Any]) -> dict[str, Any]:
        """Force-reveal email/phone if the search returned a masked record."""
        payload: dict[str, Any] = {"reveal_personal_emails": True}
        if person.get("linkedin_url"):
            payload["linkedin_url"] = person["linkedin_url"]
        elif person.get("id"):
            payload["id"] = person["id"]
        else:
            return person
        r = self.session.post(f"{APOLLO_BASE}/people/match", json=payload, timeout=30)
        if r.status_code != 200:
            log.warning("Apollo enrich failed for %s: %s", person.get("name"), r.text[:200])
            return person
        return r.json().get("person") or person


def _to_lead(person: dict[str, Any], source: str) -> Lead | None:
    email = person.get("email")
    if not email or "email_not_unlocked" in email or "domain.com" in email:
        return None

    schools = []
    employment = person.get("employment_history") or []
    education = person.get("education") or []
    for ed in education:
        school = (ed.get("school", {}) or {}).get("name") or ed.get("school_name")
        if school:
            schools.append(school)
    is_uiuc = any(s.lower() in UIUC_SCHOOLS for s in schools)

    org = person.get("organization") or {}
    industry = org.get("industry") or person.get("industry")
    location = (
        person.get("city")
        and person.get("state")
        and f"{person['city']}, {person['state']}"
    ) or person.get("location_name") or person.get("present_raw_address")

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


def pick_profile_for_today(profiles: list[dict[str, Any]], day_index: int) -> dict[str, Any]:
    """Rotate through profiles by weekday so we hit every source across a week."""
    apollo_profiles = [p for p in profiles if p.get("source") != "cube_alumni_sheet"]
    return apollo_profiles[day_index % len(apollo_profiles)]


def fetch_leads(
    client: ApolloClient,
    profile: dict[str, Any],
    max_results: int = 25,
) -> Iterable[Lead]:
    people = client.search_people(profile["params"])
    log.info("Apollo returned %d people for profile %s", len(people), profile["name"])
    count = 0
    for person in people:
        if count >= max_results:
            break
        if profile.get("enrich") and (
            not person.get("email") or "email_not_unlocked" in (person.get("email") or "")
        ):
            person = client.enrich_person(person)
        lead = _to_lead(person, source=profile["name"])
        if lead:
            yield lead
            count += 1

"""People Data Labs (PDL) Person Search wrapper.

API docs: https://docs.peopledatalabs.com/docs/person-search-api

We POST an Elasticsearch query to `/v5/person/search`. Unlike Apollo, PDL bills
**per record returned** (1 credit each) and the search response already contains
the email — there is no separate "reveal" step. So the cost lever is the `size`
parameter: we fetch only as many records as we need, and the orchestrator runs
the broad/breadth search only when UIUC alumni don't already fill the daily
target.

Note: PDL only returns email fields on paid tiers; on the free tier `work_email`
comes back empty and these records are unusable for outreach.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml

from ..models import Lead

log = logging.getLogger(__name__)

PDL_BASE = "https://api.peopledatalabs.com/v5"

# PDL normalizes school names; these are the Urbana-Champaign variants we match
# in the search query. Detection on returned records uses _is_uiuc (broader).
UIUC_SCHOOL_TERMS = [
    "university of illinois at urbana-champaign",
    "university of illinois urbana-champaign",
    "university of illinois urbana champaign",
]


def _is_uiuc(schools) -> bool:
    """True if any school string points to the UIUC campus (not UIC / UIS).

    PDL's education data is LinkedIn/resume-derived, so this is our cross-check.
    """
    for s in schools:
        t = (s or "").lower()
        if "illinois" in t and any(c in t for c in ("urbana", "champaign", "uiuc")):
            return True
        if "uiuc" in t.split():
            return True
    return False


# ---------------- profile / query helpers ----------------

def load_profiles(path: str | Path = "config/search_profiles.yaml") -> list[dict[str, Any]]:
    return yaml.safe_load(Path(path).read_text())["profiles"]


def get_uiuc_profile(profiles: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The dedicated UIUC-alumni profile, run every day as the primary source."""
    for p in profiles:
        if p.get("uiuc_only"):
            return p
    return None


def pick_profile_for_today(profiles: list[dict[str, Any]], day_index: int) -> dict[str, Any]:
    """Rotate the breadth profiles by day. The UIUC profile is excluded here
    because it runs every day, not on rotation."""
    breadth = [
        p for p in profiles
        if p.get("source") != "cube_alumni_sheet" and not p.get("uiuc_only")
    ]
    return breadth[day_index % len(breadth)]


def build_query(profile: dict[str, Any]) -> dict[str, Any]:
    """Translate a search profile into a PDL Elasticsearch bool query."""
    must: list[dict[str, Any]] = []

    if profile.get("uiuc_only"):
        must.append({
            "bool": {
                "should": [
                    {"match_phrase": {"education.school.name": t}}
                    for t in UIUC_SCHOOL_TERMS
                ],
                "minimum_should_match": 1,
            }
        })

    if profile.get("job_title_levels"):
        must.append({"terms": {"job_title_levels": [l.lower() for l in profile["job_title_levels"]]}})
    if profile.get("location_regions"):
        must.append({"terms": {"location_region": [r.lower() for r in profile["location_regions"]]}})
    if profile.get("industries"):
        must.append({"terms": {"job_company_industry": [i.lower() for i in profile["industries"]]}})

    # Only people with a work email are worth a credit for outreach.
    must.append({"exists": {"field": "work_email"}})
    return {"bool": {"must": must}}


# ---------------- client ----------------

class PDLClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ["PDL_API_KEY"]
        self.session = requests.Session()
        self.session.headers.update(
            {"X-Api-Key": self.api_key, "Content-Type": "application/json"}
        )

    def search(self, query: dict[str, Any], size: int) -> list[dict[str, Any]]:
        body = {"query": query, "size": max(1, min(size, 100))}
        r = self.session.post(f"{PDL_BASE}/person/search", json=body, timeout=30)
        if r.status_code != 200:
            log.warning("PDL search failed (%s): %s", r.status_code, r.text[:300])
            return []
        return r.json().get("data", [])


# ---------------- record -> Lead ----------------

def _best_email(record: dict[str, Any]) -> str | None:
    if record.get("work_email"):
        return record["work_email"]
    for e in record.get("emails") or []:
        addr = e.get("address") if isinstance(e, dict) else e
        if addr:
            return addr
    personal = record.get("personal_emails") or []
    return personal[0] if personal else None


def _record_to_lead(record: dict[str, Any], source: str, assume_uiuc: bool = False) -> Lead | None:
    email = _best_email(record)
    if not email:
        return None

    schools = []
    for ed in record.get("education") or []:
        name = (ed.get("school") or {}).get("name") if isinstance(ed, dict) else None
        if name:
            schools.append(name)

    return Lead(
        name=record.get("full_name") or " ".join(
            filter(None, [record.get("first_name"), record.get("last_name")])
        ),
        title=record.get("job_title"),
        company=record.get("job_company_name") or "",
        email=email,
        linkedin=record.get("linkedin_url"),
        industry=record.get("job_company_industry"),
        location=record.get("location_name") or record.get("location_region"),
        is_uiuc_alum=assume_uiuc or _is_uiuc(schools),
        schools=schools,
        source=source,
        date_added=datetime.now(timezone.utc),
    )


def search_leads(client: PDLClient, profile: dict[str, Any], size: int = 25) -> list[Lead]:
    """Search PDL for a profile and return ready-to-use Leads (emails included).

    Costs ~`size` credits (one per record returned). Records without a usable
    email or that fail Lead validation are dropped.
    """
    records = client.search(build_query(profile), size=size)
    log.info("PDL returned %d records for profile %s", len(records), profile["name"])
    uiuc_only = bool(profile.get("uiuc_only"))
    out: list[Lead] = []
    for rec in records:
        try:
            lead = _record_to_lead(rec, source=profile["name"], assume_uiuc=uiuc_only)
        except Exception as exc:  # malformed record / invalid email — skip
            log.warning("Skipping malformed PDL record: %s", exc)
            continue
        if lead:
            out.append(lead)
    return out

"""Hunter.io email lookups — the fallback when Apollo can't reveal emails.

API docs: https://hunter.io/api-documentation/v2

Apollo's search hides last names (`last_name_obfuscated`) and company domains
until its paid reveal runs, so Hunter can't find emails for Apollo discovery
leads. It covers the two sources where we already know who and where:

  * Alumni tab rows (full name + company, maybe a LinkedIn URL): `find_email`
    (Email Finder).
  * The big-company list in config/enterprise_targets.yaml (name + domain):
    `enterprise_leads` (Domain Search) returns people at the domain with their
    emails and titles, so it sources and reveals in one call.

Credits: Email Finder costs 1 per email found and Domain Search 1 per call that
returns anyone; misses are free. The free plan has ~50 a month, so each run
only spends its share of what is left in the billing cycle (`_run_allowance`).
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

from ..companies import CompanyRegistry
from ..env import env_int
from ..models import Lead
from .enterprise import targets_for_today

log = logging.getLogger(__name__)

HUNTER_BASE = "https://api.hunter.io/v2"


class HunterError(RuntimeError):
    """A Hunter call failed outright (bad key, rate or usage limit)."""


def _min_score() -> int:
    # Hunter's confidence score. Unverified guesses below this bounce too often
    # to be worth the sender reputation.
    return env_int("HUNTER_MIN_SCORE", 70)


def _usable(email: str | None, score: int | None, status: str | None) -> bool:
    if not email or status == "invalid":
        return False
    return status == "valid" or (score or 0) >= _min_score()


def _run_allowance(remaining: float, reset: date | None, today: date, cap: int) -> int:
    """Spread the credits left over the weekdays left in the billing cycle."""
    if remaining < 1:
        return 0
    days = 1
    if reset and reset > today:
        days = max(1, sum(
            (today + timedelta(d)).weekday() < 5 for d in range((reset - today).days)
        ))
    return min(cap, math.ceil(remaining / days))


def _linkedin_handle(url: str | None) -> str | None:
    if not url or "/in/" not in url:
        return None
    return url.split("/in/", 1)[1].strip("/").split("/")[0].split("?")[0] or None


class HunterClient:
    def __init__(self, api_key: str | None = None, max_credits: int = 15) -> None:
        self.api_key = api_key or os.environ["HUNTER_API_KEY"].strip()
        self.session = requests.Session()
        self.session.headers.update({"X-API-KEY": self.api_key})
        self.max_credits = max_credits
        self._allowance: int | None = None  # loaded on first use
        self.credits_used = 0
        self.found = 0
        self.disabled = False  # set when Hunter itself fails; no more calls this run

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        r = self.session.get(f"{HUNTER_BASE}/{path}", params=params, timeout=30)
        if r.status_code == 404:
            return None  # Email Finder: no matching person
        if r.status_code != 200:
            raise HunterError(f"{path} failed ({r.status_code}): {r.text[:300]}")
        return r.json().get("data") or {}

    def can_spend(self) -> bool:
        if self.disabled:
            return False
        if self._allowance is None:
            try:
                data = self._get("account", {}) or {}
            except (HunterError, requests.RequestException) as exc:
                log.error("Hunter account check failed; not using Hunter this run: %s", exc)
                self.disabled = True
                return False
            reqs = data.get("requests") or {}
            bucket = reqs.get("credits") or reqs.get("searches") or {}
            remaining = bucket.get("remaining")
            if remaining is None:
                remaining = (bucket.get("available") or 0) - (bucket.get("used") or 0)
            reset = date.fromisoformat(data["reset_date"]) if data.get("reset_date") else None
            today = datetime.now(timezone.utc).date()
            self._allowance = _run_allowance(remaining, reset, today, self.max_credits)
            log.info(
                "Hunter %s plan: %s credits left until %s; spending up to %d this run",
                data.get("plan_name"), remaining, reset, self._allowance,
            )
        return self.credits_used < self._allowance

    def _call(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return self._get(path, params)
        except (HunterError, requests.RequestException) as exc:
            log.error("Hunter lookup failed; not using Hunter for the rest of this run: %s", exc)
            self.disabled = True
            return None

    def find_email(
        self, first_name: str, last_name: str, company: str = "", linkedin: str | None = None,
    ) -> str | None:
        """Email Finder: the person's email, or None if Hunter has no confident one."""
        if not self.can_spend():
            return None
        handle = _linkedin_handle(linkedin)
        if handle:
            params = {"linkedin_handle": handle}
        elif first_name and last_name and company:
            params = {"first_name": first_name, "last_name": last_name, "company": company}
        else:
            return None
        data = self._call("email-finder", params)
        if not data or not data.get("email"):
            return None
        self.credits_used += 1  # a found email is charged even if we reject it
        status = (data.get("verification") or {}).get("status")
        if not _usable(data["email"], data.get("score"), status):
            log.info("Hunter: skipping low-confidence %s (score %s, %s)",
                     data["email"], data.get("score"), status)
            return None
        self.found += 1
        return data["email"]

    def enterprise_leads(
        self,
        config: dict[str, Any],
        registry: CompanyRegistry | None,
        day_index: int,
        need: int,
    ) -> list[Lead]:
        """Domain Search today's not-yet-contacted target companies until `need`
        companies have a usable contact or the run's credits are spent. Up to two
        people per company; the second is a backup if the first is excluded."""
        targets = config.get("companies") or []
        titles = [t.lower() for t in config.get("titles") or []]
        out: list[Lead] = []
        companies_found = 0
        for target in targets_for_today(targets, registry, day_index, len(targets)):
            if companies_found >= need or not self.can_spend():
                break
            data = self._call("domain-search", {
                "domain": target["domain"], "type": "personal", "limit": 10,
                "seniority": "senior,executive", "required_field": "full_name,position",
            })
            emails = (data or {}).get("emails") or []
            if not emails:
                continue
            self.credits_used += 1
            people = [
                e for e in emails
                if _usable(e.get("value"), e.get("confidence"),
                           (e.get("verification") or {}).get("status"))
            ]
            # Titles from the target config first (the people most likely to
            # sponsor a project), then executives, then Hunter's confidence.
            people.sort(key=lambda e: (
                any(t in (e.get("position") or "").lower() for t in titles),
                e.get("seniority") == "executive",
                e.get("confidence") or 0,
            ), reverse=True)
            leads = [_to_lead(e, target) for e in people[:2]]
            if leads:
                companies_found += 1
                self.found += len(leads)
                out.extend(leads)
        log.info("Hunter enterprise search: %d companies with a usable contact", companies_found)
        return out


def _to_lead(e: dict[str, Any], target: dict[str, str]) -> Lead:
    return Lead(
        name=f"{e.get('first_name') or ''} {e.get('last_name') or ''}".strip(),
        title=e.get("position"),
        company=target["name"],
        email=e["value"],
        linkedin=e.get("linkedin"),
        source="hunter_enterprise",
        date_added=datetime.now(timezone.utc),
    )

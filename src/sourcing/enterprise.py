"""Big-name company sourcing: the curated list in config/enterprise_targets.yaml.

Apollo's generic "large company" searches don't reach recognizable names:
`q_organization_keyword_tags` also matches company NAMES, so a big_tech search
returned firms literally called "Artificial Intelligence", and half of one
page was a single contractor. Searching by the company's own domain
(`q_organization_domains_list`) is precise, so the well-known targets are an
explicit list instead.

Each run searches a few not-yet-contacted companies from that list (one free
search per company) and keeps each company's best two people. The results feed
the enterprise quota in `prepare`.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from ..companies import CompanyRegistry
from .apollo import ApolloClient, Candidate, _to_candidate

log = logging.getLogger(__name__)

PROFILE = {"name": "enterprise_targets", "tier": "enterprise", "enrich": True}


def load_targets(path: str | Path = "config/enterprise_targets.yaml") -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text())


def targets_for_today(
    targets: list[dict[str, str]],
    registry: CompanyRegistry | None,
    day_index: int,
    count: int,
) -> list[dict[str, str]]:
    """`count` uncontacted companies, starting at a rotation point set by the day."""
    fresh = [
        t for t in targets
        if registry is None or not registry.seen(t["name"], f"x@{t['domain']}")
    ]
    if not fresh:
        return []
    start = day_index % len(fresh)
    return [fresh[(start + i) % len(fresh)] for i in range(min(count, len(fresh)))]


def enterprise_candidates(
    client: ApolloClient,
    registry: CompanyRegistry | None,
    day_index: int,
    count: int,
    config: dict[str, Any] | None = None,
) -> tuple[list[Candidate], list[str]]:
    """Search today's target companies. Returns (candidates, companies searched).

    Up to two people per company: the second is a backup in case the first
    one's email can't be revealed. Company dedupe stops both being emailed.
    """
    config = config or load_targets()
    picked = targets_for_today(config.get("companies") or [], registry, day_index, count)
    out: list[Candidate] = []
    matched = 0
    for target in picked:
        params = {
            "q_organization_domains_list": [target["domain"]],
            "person_titles": config.get("titles") or [],
            "person_seniorities": config.get("seniorities") or [],
            "per_page": 10,
        }
        try:
            people = client.search_people(params)
        except Exception as exc:
            log.warning("Apollo search failed for %s: %s", target["name"], exc)
            continue
        # People Apollo already holds an email for reveal reliably, so a credit
        # spent on them is rarely wasted.
        people.sort(key=lambda p: bool(p.get("has_email")), reverse=True)
        found = [_to_candidate(p, PROFILE) for p in people[:2]]
        for cand in found:
            cand.company = cand.company or target["name"]
        matched += bool(found)
        out.extend(found)
    log.info(
        "Enterprise targets searched: %d companies, %d with a matching contact",
        len(picked), matched,
    )
    return out, [t["name"] for t in picked]

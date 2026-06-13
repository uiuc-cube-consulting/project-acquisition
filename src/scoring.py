"""Weighted lead scoring + hard-filter logic.

Reads config/scoring.yaml. Higher score => more likely to make today's
batch of 10. Hard filters drop leads outright (current student, in
suppression list, contacted recently).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import yaml

from .models import Lead


class Scorer:
    def __init__(self, config_path: str | Path = "config/scoring.yaml") -> None:
        self.cfg = yaml.safe_load(Path(config_path).read_text())
        self.weights = self.cfg["weights"]
        self.target_stages = {s.lower() for s in self.cfg.get("target_stages", [])}
        self.senior_kw = [k.lower() for k in self.cfg.get("senior_title_keywords", [])]
        # Hard-filter recency window
        self.recent_days = 180
        for rule in self.cfg.get("exclude", []):
            if isinstance(rule, dict) and "already_contacted_within_days" in rule:
                self.recent_days = rule["already_contacted_within_days"]

    def score(self, lead: Lead, past_project_keywords: set[str]) -> float:
        s = 0.0
        title_lower = (lead.title or "").lower()
        is_senior = any(kw in title_lower for kw in self.senior_kw)

        if lead.is_uiuc_alum:
            s += self.weights["uiuc_alum"]
            if is_senior:
                s += self.weights["uiuc_alum_executive"]

        if lead.location and "illinois" in lead.location.lower():
            s += self.weights["illinois_based"]
        if lead.location and any(c in lead.location.lower() for c in ("chicago", "champaign", "urbana")):
            s += self.weights["illinois_based"] / 2  # smaller bonus, additive

        if lead.company_stage and lead.company_stage.lower() in self.target_stages:
            s += self.weights["startup_stage_match"]

        if is_senior:
            s += self.weights["senior_title"]

        industry_tokens = set((lead.industry or "").lower().split())
        if industry_tokens & past_project_keywords:
            s += self.weights["industry_matches_past_project"]

        return s

    def is_excluded(
        self,
        lead: Lead,
        suppression_emails: set[str],
        already_contacted_at: dict[str, datetime],
    ) -> tuple[bool, str]:
        if lead.email.lower() in suppression_emails:
            return True, "in suppression list"
        if lead.email.lower().endswith("@illinois.edu") and not lead.is_uiuc_alum:
            # Current student / unaffiliated illinois.edu address — skip.
            # (Alums tend to have personal/work emails, not illinois.edu.)
            return True, "current uiuc student address"
        last = already_contacted_at.get(lead.email.lower())
        if last and (datetime.now(timezone.utc) - last) < timedelta(days=self.recent_days):
            return True, f"contacted within last {self.recent_days} days"
        return False, ""


def pick_top(
    leads: Iterable[Lead],
    target: int,
    scorer: Scorer,
    past_project_keywords: set[str],
) -> list[Lead]:
    scored = []
    for lead in leads:
        lead.score = scorer.score(lead, past_project_keywords)
        scored.append(lead)
    # Hard-prioritize UIUC alumni: every alum sorts ahead of every non-alum,
    # with the weighted score as the tiebreaker within each tier. Non-alumni
    # only fill the batch once we run out of alumni to reach `target`.
    scored.sort(key=lambda l: (l.is_uiuc_alum, l.score), reverse=True)
    return scored[:target]

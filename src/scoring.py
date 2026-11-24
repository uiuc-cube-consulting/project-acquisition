"""Weighted lead scoring + hard-filter logic.

Reads config/scoring.yaml. Higher score => more likely to make today's
batch (size: DAILY_PREPARE_TARGET). Hard filters drop leads outright
(current student, in suppression list, contacted recently).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .models import Lead

# Cities that earn the smaller location bonus, but only when the location is in Illinois.
_IL_CITIES = re.compile(r"\b(?:chicago|champaign|urbana)\b", re.IGNORECASE)
# "Illinois" spelled out, or the postal code "IL" as its own word. The code is
# matched case-sensitively so ordinary words such as "il" are never taken for it.
_IN_ILLINOIS = re.compile(r"(?i:\billinois\b)|\bIL\b")


def _keyword_pattern(keywords: list[str]) -> re.Pattern[str] | None:
    """One case-insensitive regex matching any keyword as a whole word or phrase.

    Longer keywords come first so a phrase ("Vice President") is tried before
    a shorter keyword it contains. Returns None for an empty list, since an
    empty alternation would match every title.
    """
    if not keywords:
        return None
    alternatives = "|".join(re.escape(k) for k in sorted(keywords, key=len, reverse=True))
    return re.compile(rf"\b(?:{alternatives})\b", re.IGNORECASE)


class Scorer:
    def __init__(self, config_path: str | Path = "config/scoring.yaml") -> None:
        self.cfg = yaml.safe_load(Path(config_path).read_text())
        self.weights = self.cfg["weights"]
        self.target_stages = {s.lower() for s in self.cfg.get("target_stages", [])}
        self.senior_kw = [k.lower() for k in self.cfg.get("senior_title_keywords", [])]
        self.junior_kw = [k.lower() for k in self.cfg.get("junior_title_keywords", [])]
        self._senior_re = _keyword_pattern(self.senior_kw)
        self._junior_re = _keyword_pattern(self.junior_kw)
        # Hard-filter recency window
        self.recent_days = 180
        for rule in self.cfg.get("exclude", []):
            if isinstance(rule, dict) and "already_contacted_within_days" in rule:
                self.recent_days = rule["already_contacted_within_days"]

    def is_senior(self, title: str | None) -> bool:
        """True if the title has a senior keyword and no junior keyword.

        A junior keyword cancels seniority, so "Executive Assistant to the CEO"
        is not senior even though it contains "CEO".
        """
        if not title or self._senior_re is None:
            return False
        if self._junior_re is not None and self._junior_re.search(title):
            return False
        return bool(self._senior_re.search(title))

    def score(self, lead: Lead, past_project_keywords: set[str]) -> float:
        s = 0.0
        is_senior = self.is_senior(lead.title)

        if lead.is_uiuc_alum:
            s += self.weights["uiuc_alum"]
            if is_senior:
                s += self.weights["uiuc_alum_executive"]

        location = lead.location or ""
        in_illinois = bool(_IN_ILLINOIS.search(location))
        if in_illinois:
            s += self.weights["illinois_based"]
            if _IL_CITIES.search(location):
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

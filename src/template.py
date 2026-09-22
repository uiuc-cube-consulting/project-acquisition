"""Industry-tag → template-type router.

Reads config/industry_template_map.yaml and matches keywords against
Apollo's `industry` field (case-insensitive).

Matching rule:
  1. Keywords match whole words/phrases only, so "mobile" does NOT
     match inside "automobiles".
  2. Every keyword in every template is checked; the LONGEST matching
     keyword wins ("electronics" beats "consumer").
  3. Ties (equal-length keywords) go to whichever template appears first
     in the YAML.
  4. No match, or an empty industry → the `default` template.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .models import TemplateType

_TYPE_BY_KEY = {
    "business": TemplateType.BUSINESS,
    "hybrid_product": TemplateType.HYBRID_PRODUCT,
    "technical_software": TemplateType.TECHNICAL_SOFTWARE,
    "technical_engineering": TemplateType.TECHNICAL_ENGINEERING,
}


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    # Lookarounds instead of \b so keywords that start or end with
    # symbols (e.g. "&", "-") still match correctly.
    return re.compile(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])")


class TemplateRouter:
    def __init__(self, config_path: str | Path = "config/industry_template_map.yaml") -> None:
        self.cfg = yaml.safe_load(Path(config_path).read_text())
        self.default = _TYPE_BY_KEY[self.cfg.get("default", "business")]
        self.mappings = {
            _TYPE_BY_KEY[k]: [s.lower().strip() for s in v]
            for k, v in self.cfg.get("mappings", {}).items()
        }
        # (template, keyword length, compiled pattern), kept in YAML order
        self._patterns = [
            (tmpl_type, len(kw), _keyword_pattern(kw))
            for tmpl_type, keywords in self.mappings.items()
            for kw in keywords
        ]

    def route(self, industry: str | None) -> TemplateType:
        if not industry:
            return self.default
        lowered = " ".join(industry.lower().split())  # normalize whitespace
        best, best_len = None, 0
        for tmpl_type, kw_len, pattern in self._patterns:
            # strict ">" means an equal-length match later in the YAML
            # can't steal the win: ties go to YAML order
            if kw_len > best_len and pattern.search(lowered):
                best, best_len = tmpl_type, kw_len
        return best or self.default
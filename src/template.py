"""Industry-tag → template-type router.

Reads config/industry_template_map.yaml. Substring match (case-insensitive)
on Apollo's `industry` field. Defaults to BUSINESS when nothing matches.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .models import TemplateType

_TYPE_BY_KEY = {
    "business": TemplateType.BUSINESS,
    "hybrid_product": TemplateType.HYBRID_PRODUCT,
    "technical_software": TemplateType.TECHNICAL_SOFTWARE,
    "technical_engineering": TemplateType.TECHNICAL_ENGINEERING,
}


class TemplateRouter:
    def __init__(self, config_path: str | Path = "config/industry_template_map.yaml") -> None:
        self.cfg = yaml.safe_load(Path(config_path).read_text())
        self.default = _TYPE_BY_KEY[self.cfg.get("default", "business")]
        self.mappings = {
            _TYPE_BY_KEY[k]: [s.lower() for s in v]
            for k, v in self.cfg.get("mappings", {}).items()
        }

    def route(self, industry: str | None) -> TemplateType:
        if not industry:
            return self.default
        lowered = industry.lower()
        for tmpl_type, substrings in self.mappings.items():
            if any(sub in lowered for sub in substrings):
                return tmpl_type
        return self.default

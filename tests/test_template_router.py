from pathlib import Path

import pytest

from src.models import TemplateType
from src.template import TemplateRouter

CONFIG = Path(__file__).resolve().parents[1] / "config" / "industry_template_map.yaml"

BUS = TemplateType.BUSINESS
HYB = TemplateType.HYBRID_PRODUCT
SW = TemplateType.TECHNICAL_SOFTWARE
ENG = TemplateType.TECHNICAL_ENGINEERING


@pytest.fixture(scope="module")
def router():
    return TemplateRouter(CONFIG)


@pytest.mark.parametrize(
    "industry, expected",
    [
        # Rows from issue #14
        ("consumer electronics", ENG),
        ("automobiles", ENG),
        ("computer & network security", SW),
        ("defense & space", ENG),
        ("pharmaceuticals", BUS),
        ("e-learning", HYB),
        ("telecommunications", ENG),
        # Already correct before the fix (regression guard)
        ("computer software", SW),
        ("oil & energy", ENG),
        ("financial services", BUS),
        # Matching-rule checks
        ("Consumer Electronics", ENG),       # case-insensitive
        ("  defense  &  space ", ENG),       # extra whitespace
        ("mobile games", HYB),               # whole word still matches
    ],
)
def test_routes_industry(router, industry, expected):
    assert router.route(industry) == expected


@pytest.mark.parametrize("industry", [None, "", "   ", "underwater basket weaving"])
def test_falls_back_to_default(router, industry):
    assert router.route(industry) == BUS


def test_tie_goes_to_yaml_order(tmp_path):
    cfg = tmp_path / "map.yaml"
    cfg.write_text(
        "default: business\n"
        "mappings:\n"
        "  hybrid_product: ['alpha']\n"
        "  technical_software: ['omega']\n"
    )
    r = TemplateRouter(cfg)
    assert r.route("alpha omega") == HYB
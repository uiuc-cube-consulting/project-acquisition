"""Lead scoring: senior-title matching, the Illinois location bonus, and hard filters.

No network and no Sheet: leads are built by hand with made-up example.com data.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from src.models import Lead
from src.scoring import Scorer

CONFIG = Path(__file__).resolve().parents[1] / "config" / "scoring.yaml"
WEIGHTS = yaml.safe_load(CONFIG.read_text())["weights"]


@pytest.fixture
def scorer():
    return Scorer(CONFIG)


def lead(**overrides):
    fields = dict(name="Test Person", company="Example Co", email="test@example.com")
    fields.update(overrides)
    return Lead(**fields)


# --- Seniority -------------------------------------------------------------

@pytest.mark.parametrize(
    "title, senior",
    [
        # The four titles from the issue
        ("Marketing Coordinator", False),           # "coo" inside "coordinator"
        ("Executive Assistant to the CEO", False),  # junior keyword cancels "CEO"
        ("Partnerships Associate", False),          # "partner" inside "partnerships"
        ("Director of Sales", True),                # still senior; "cto" in "director" no longer the reason
        # Whole-word matches that must keep working
        ("COO", True),
        ("COO & Co-Founder", True),
        ("coo", True),
        ("Co-Founder", True),
        ("Founder and CEO", True),
        ("CTO / Head of Engineering", True),
        ("VP, Marketing", True),
        ("SVP of Operations", True),
        ("Vice President of Product", True),
        ("Managing Partner", True),
        ("Head of Growth", True),
        # Not senior
        ("Software Engineer", False),
        ("Chief of Staff Intern", False),
        ("International Sales Manager", False),     # "intern" inside "international" is not junior...
        ("Account Executive", False),               # ...and nothing senior here either
        ("Head Coach", False),                      # "Head of" is a phrase, not "head"
        ("", False),
        (None, False),
    ],
)
def test_is_senior(scorer, title, senior):
    assert scorer.is_senior(title) is senior


def test_international_director_is_senior(scorer):
    # "International" must not trip the junior keyword "Intern".
    assert scorer.is_senior("Director, International Partnerships") is True


def test_senior_title_adds_weight(scorer):
    assert scorer.score(lead(title="VP of Sales"), set()) == WEIGHTS["senior_title"]
    assert scorer.score(lead(title="Marketing Coordinator"), set()) == 0


def test_coordinator_alum_does_not_get_executive_bonus(scorer):
    coordinator = lead(title="Marketing Coordinator", is_uiuc_alum=True)
    vp = lead(title="VP of Marketing", is_uiuc_alum=True)
    assert scorer.score(coordinator, set()) == WEIGHTS["uiuc_alum"]
    assert scorer.score(vp, set()) == (
        WEIGHTS["uiuc_alum"] + WEIGHTS["uiuc_alum_executive"] + WEIGHTS["senior_title"]
    )


def test_empty_keyword_lists_mark_nothing_senior(tmp_path):
    cfg = yaml.safe_load(CONFIG.read_text())
    cfg["senior_title_keywords"] = []
    cfg.pop("junior_title_keywords", None)
    path = tmp_path / "scoring.yaml"
    path.write_text(yaml.safe_dump(cfg))
    assert Scorer(path).is_senior("CEO") is False


# --- Location --------------------------------------------------------------

STATE = WEIGHTS["illinois_based"]
CITY = WEIGHTS["illinois_based"] / 2


@pytest.mark.parametrize(
    "location, expected",
    [
        ("Chicago, Illinois, United States", STATE + CITY),
        ("Champaign, IL", STATE + CITY),
        ("Urbana, Illinois", STATE + CITY),
        ("Springfield, Illinois", STATE),
        ("Naperville, IL 60540", STATE),
        ("Urbana, Ohio", 0),                         # the bug: no city bonus outside Illinois
        ("Chicago Heights, Ohio", 0),
        ("Austin, Texas", 0),
        ("Brasil", 0),                               # "il" is not the postal code
        ("", 0),
        (None, 0),
    ],
)
def test_location_bonus(scorer, location, expected):
    assert scorer.score(lead(location=location), set()) == expected


# --- Total score -----------------------------------------------------------

def test_total_score_matches_config_weights(scorer):
    best = lead(
        title="Co-Founder & CEO",
        is_uiuc_alum=True,
        location="Chicago, Illinois",
        company_stage="Seed",
        industry="Clean Energy",
    )
    expected = (
        WEIGHTS["uiuc_alum"]
        + WEIGHTS["uiuc_alum_executive"]
        + WEIGHTS["illinois_based"]
        + WEIGHTS["illinois_based"] / 2
        + WEIGHTS["startup_stage_match"]
        + WEIGHTS["senior_title"]
        + WEIGHTS["industry_matches_past_project"]
    )
    assert scorer.score(best, {"energy"}) == expected


def test_stage_and_industry(scorer):
    assert scorer.score(lead(company_stage="series a"), set()) == WEIGHTS["startup_stage_match"]
    assert scorer.score(lead(company_stage="Series D"), set()) == 0
    assert scorer.score(lead(industry="Retail"), {"energy"}) == 0


# --- Hard filters ----------------------------------------------------------

NOW = datetime.now(timezone.utc)


def test_suppressed_email_is_excluded(scorer):
    excluded, reason = scorer.is_excluded(lead(email="Test@Example.com"), {"test@example.com"}, {})
    assert excluded and reason == "in suppression list"


def test_current_student_address_is_excluded(scorer):
    excluded, reason = scorer.is_excluded(lead(email="student@illinois.edu"), set(), {})
    assert excluded and reason == "current uiuc student address"


def test_alum_with_illinois_edu_address_is_kept(scorer):
    assert scorer.is_excluded(lead(email="alum@illinois.edu", is_uiuc_alum=True), set(), {}) == (False, "")


def test_recently_contacted_is_excluded(scorer):
    contacted = {"test@example.com": NOW - timedelta(days=30)}
    excluded, reason = scorer.is_excluded(lead(), set(), contacted)
    assert excluded and reason == f"contacted within last {scorer.recent_days} days"


def test_contacted_long_ago_is_kept(scorer):
    contacted = {"test@example.com": NOW - timedelta(days=scorer.recent_days + 1)}
    assert scorer.is_excluded(lead(), set(), contacted) == (False, "")


def test_recency_window_comes_from_config(scorer):
    assert scorer.recent_days == 180


def test_clean_lead_is_kept(scorer):
    assert scorer.is_excluded(lead(), set(), {}) == (False, "")

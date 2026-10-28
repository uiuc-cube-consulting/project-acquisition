from src.sourcing.apollo import _to_lead


def person(**overrides):
    base = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.org",
        "organization": {"name": "Analytical Engines"},
    }
    return {**base, **overrides}


def test_email_status_is_kept():
    lead = _to_lead(person(email_status="verified"), source="test")
    assert lead.email_status == "verified"


def test_missing_email_status_is_none():
    lead = _to_lead(person(), source="test")
    assert lead.email_status is None


def test_locked_email_still_skipped():
    assert _to_lead(person(email="email_not_unlocked@domain.com", email_status="verified"), source="test") is None

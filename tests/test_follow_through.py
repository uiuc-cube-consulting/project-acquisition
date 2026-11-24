
from datetime import date

from src.follow_through import business_days_between, reminder_reasons


def test_business_days_excludes_weekends():
    assert business_days_between(
        date(2026, 11, 6), date(2026, 11, 10)
    ) == 2


def test_business_days_excludes_blackout_dates():
    assert business_days_between(
        date(2026, 11, 9),
        date(2026, 11, 12),
        ["2026-11-11"],
    ) == 2


def test_unowned_reminder():
    lead = {"owner": "", "flagged_at": "2026-11-09"}

    assert "Unowned" in reminder_reasons(lead, date(2026, 11, 12))


def test_no_next_step_reminder():
    lead = {
        "owner": "Alex",
        "next_step": "",
        "flagged_at": "2026-11-06",
    }

    assert "No next step" in reminder_reasons(lead, date(2026, 11, 12))


def test_due_reminder():
    lead = {"next_touch": "2026-11-10"}

    assert "Due" in reminder_reasons(lead, date(2026, 11, 12))


def test_reply_owed_reminder():
    lead = {
        "latest_message_at": "2026-11-06",
        "latest_sender_is_prospect": True,
    }

    assert "Reply owed" in reminder_reasons(lead, date(2026, 11, 12))
def test_unowned_reminder_does_not_fire_too_early():
    lead = {"owner": "", "flagged_at": "2026-11-09"}

    assert "Unowned" not in reminder_reasons(lead, date(2026, 11, 10))


def test_reply_owed_does_not_fire_after_exactly_two_business_days():
    lead = {
        "latest_message_at": "2026-11-06",
        "latest_sender_is_prospect": True,
    }

    assert "Reply owed" not in reminder_reasons(lead, date(2026, 11, 10))
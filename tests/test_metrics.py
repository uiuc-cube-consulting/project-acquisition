from src.metrics import compute


def lead(email, email_status=None):
    row = {"email": email, "name": email.split("@")[0], "source": "test"}
    if email_status is not None:
        row["email_status"] = email_status
    return row


def sent(email):
    return {"lead_email": email, "sent_at": "2026-10-01T15:00:00+00:00", "prepared_at": "2026-10-01T14:00:00+00:00"}


def bounce(email):
    return {"lead_email": email, "category": "bounce"}


def test_bounce_rate_by_email_status():
    leads = [
        lead("a@x.com", "verified"),
        lead("b@x.com", "verified"),
        lead("c@x.com", "guessed"),
        lead("d@x.com", "guessed"),
        lead("e@x.com", ""),  # row written before the column existed
        lead("f@x.com"),      # sheet without the column at all
    ]
    drafts = [sent(l["email"]) for l in leads]
    replies = [bounce("c@x.com"), bounce("e@x.com")]

    m = compute(leads=leads, drafts=drafts, alumni=[], replies=replies)
    by_status = {s["label"]: s for s in m["reply"]["by_email_status"]}

    assert set(by_status) == {"verified", "guessed", "unknown"}
    assert (by_status["verified"]["sent"], by_status["verified"]["bounced"]) == (2, 0)
    assert by_status["verified"]["bounce_rate"] == 0.0
    assert (by_status["guessed"]["sent"], by_status["guessed"]["bounced"]) == (2, 1)
    assert by_status["guessed"]["bounce_rate"] == 0.5
    assert (by_status["unknown"]["sent"], by_status["unknown"]["bounced"]) == (2, 1)
    assert by_status["unknown"]["bounce_rate"] == 0.5

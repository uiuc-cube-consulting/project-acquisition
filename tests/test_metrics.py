from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from src.metrics import compute, reply_timing


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


def timing_lead(index=0, **overrides):
    return {"email": f"person{index}@example.com", "status": "sent",
            "sent_at": "2026-10-06T14:00:00Z", **overrides}


def timing_cell(metrics, weekday=1, hour=9):
    return next(c for c in metrics["heatmap"]["cells"]
                if c["weekday"] == weekday and c["hour"] == hour)


def test_timing_groups_by_send_time_and_does_not_mutate_inputs():
    leads = [timing_lead(0, status="replied", replied_at="2026-10-07T11:30:00Z"),
             timing_lead(1), timing_lead(2, sent_at="2026-10-09T20:00:00Z")]
    original = deepcopy(leads)
    result = reply_timing(leads)
    cell = timing_cell(result)
    assert (cell["sent"], cell["replies"], cell["reply_rate"]) == (2, 1, 0.5)
    assert result["by_weekday"][1]["sent"] == 2
    assert result["by_hour"][9]["sent"] == 2
    assert timing_cell(result, 4, 15)["sent"] == 1
    assert result["time_to_first_reply"]["median_seconds"] == 21.5 * 3600
    assert leads == original


@pytest.mark.parametrize("sent_at,replied_at,weekday,hour,seconds", [
    ("2026-03-06T15:00:00Z", "2026-03-09T14:00:00Z", 4, 9, 71 * 3600),
    ("2026-10-30T14:00:00Z", "2026-11-02T15:00:00Z", 4, 9, 73 * 3600),
    ("2026-03-08T01:30:00-06:00", "2026-03-08T03:30:00-05:00", 6, 1, 3600),
    ("2026-11-01T01:30:00-05:00", "2026-11-01T01:30:00-06:00", 6, 1, 3600),
    ("2026-10-06T02:30:00Z", "2026-10-06T11:00:00Z", 0, 21, 8.5 * 3600),
    ("2026-10-06T14:00:00", "2026-10-06T15:00:00", 1, 9, 3600),
])
def test_timing_central_dates_dst_and_elapsed_time(sent_at, replied_at, weekday, hour, seconds):
    result = reply_timing([timing_lead(sent_at=sent_at, replied_at=replied_at)])
    assert timing_cell(result, weekday, hour)["replies"] == 1
    assert result["time_to_first_reply"]["median_seconds"] == seconds


def test_timing_histogram_boundaries_and_median():
    start = datetime(2026, 10, 6, 14, tzinfo=timezone.utc)
    durations = [0, 3599, 3600, 14399, 14400, 86399, 86400, 259199, 259200]
    leads = [timing_lead(i, replied_at=(start + timedelta(seconds=s)).isoformat())
             for i, s in enumerate(durations)]
    latency = reply_timing(leads)["time_to_first_reply"]
    assert latency["n"] == 9
    assert latency["median_seconds"] == 14400
    assert [b["count"] for b in latency["histogram"]] == [2, 2, 2, 2, 1]
    assert sum(b["share"] for b in latency["histogram"]) == pytest.approx(1)
    assert reply_timing(leads[:2])["time_to_first_reply"]["median_seconds"] == 1799.5


def test_timing_deduplicates_earliest_initial_and_excludes_sent_followups():
    leads = [timing_lead(0), timing_lead(0, email=" PERSON0@EXAMPLE.COM "),
             timing_lead(1), timing_lead(2, last_follow_up_at="2026-10-08T14:00:00Z"),
             timing_lead(3, status="followed-up")]
    drafts = [
        {"lead_email": "person0@example.com", "sent_at": "2026-10-05T15:00:00Z"},
        {"lead_email": "person0@example.com", "sent_at": "", "is_follow_up": "TRUE"},
        {"lead_email": "person1@example.com", "sent_at": "2026-10-08T14:00:00Z",
         "is_follow_up": "TRUE"},
        {"lead_email": "draft-only@example.com", "sent_at": "2026-10-06T14:00:00Z",
         "is_follow_up": "FALSE"},
    ]
    result = reply_timing(leads, drafts)
    assert result["totals"]["sent"] == 2
    assert timing_cell(result, 0, 10)["sent"] == 1
    assert result["excluded"]["follow_up_recipients"] == 3


def test_timing_mailbox_categories_override_stale_lead_status():
    leads = [timing_lead(i, status="replied", replied_at="2026-10-06T15:00:00Z")
             for i in range(4)]
    replies = [{"lead_email": f"person{i}@example.com", "category": category}
               for i, category in enumerate(["human", "auto_reply", "bounce"])]
    # A later human reply takes precedence over an earlier automatic reply.
    replies.append({"lead_email": "person0@example.com", "category": "auto_reply"})
    totals = reply_timing(leads, replies=replies)["totals"]
    assert (totals["sent"], totals["delivered"], totals["replies"]) == (4, 3, 2)
    assert totals["reply_rate"] == pytest.approx(2 / 3)


def test_timing_first_human_reply_and_positive_classification_coverage():
    leads = [timing_lead(i) for i in range(10)]
    replies = [
        {"lead_email": "person0@example.com", "category": "human",
         "received_at": "2026-10-07T14:00:00Z", "classification": "negative"},
        {"lead_email": "person0@example.com", "category": "human",
         "received_at": "2026-10-06T15:00:00Z", "classification": "positive"},
        {"lead_email": "person1@example.com", "category": "human",
         "received_at": "2026-10-06T16:00:00Z"},
    ]
    result = reply_timing(leads, replies=replies)
    cell = timing_cell(result)
    assert cell["positive_replies"] == 1
    assert cell["classification_coverage"] == 0.5
    assert cell["positive_reply_rate"] is None
    assert result["time_to_first_reply"]["median_seconds"] == 5400
    replies[-1]["classification"] = "neutral"
    cell = timing_cell(reply_timing(leads, replies=replies))
    assert cell["positive_reply_rate"] == 0.1
    assert cell["positive_rate_available"]


def test_timing_small_sample_threshold_is_configurable_and_hot_is_positive():
    leads = [timing_lead(i) for i in range(10)]
    leads[0]["status"] = "hot"
    assert timing_cell(reply_timing(leads[:9]))["low_sample"]
    cell = timing_cell(reply_timing(leads))
    assert not cell["low_sample"]
    assert cell["positive_reply_rate"] == 0.1
    assert timing_cell(reply_timing(leads, min_sends=11))["positive_reply_rate"] is None
    assert not timing_cell(reply_timing(leads[:9], min_sends=5))["low_sample"]
    for threshold in [0, -1, 2.5, True]:
        with pytest.raises(ValueError, match="positive integer"):
            reply_timing([], min_sends=threshold)


def test_timing_empty_invalid_and_missing_data():
    empty = reply_timing([])
    assert empty["totals"]["reply_rate"] is None
    assert empty["time_to_first_reply"]["median_seconds"] is None
    assert len(empty["heatmap"]["cells"]) == 40
    assert all(b["share"] is None for b in empty["time_to_first_reply"]["histogram"])
    leads = [timing_lead(0, sent_at="invalid"), timing_lead(1, sent_at=""),
             timing_lead(2, status="replied", replied_at="invalid"),
             timing_lead(3, replied_at="2026-10-06T13:00:00Z"),
             timing_lead(4, status="bounced"), {"sent_at": "2026-10-06T14:00:00Z"}]
    result = reply_timing(leads)
    assert result["excluded"] == {
        "missing_email_rows": 1, "follow_up_recipients": 0, "invalid_send_recipients": 1,
        "unsent_recipients": 1, "missing_reply_timestamps": 1, "negative_reply_delays": 1,
    }
    assert result["totals"]["sent"] == 3
    assert result["totals"]["replies"] == 2
    assert result["totals"]["bounced"] == 1
    assert result["time_to_first_reply"]["n"] == 0
    assert timing_cell(reply_timing([timing_lead()]))["reply_rate"] == 0
    assert timing_cell(reply_timing([timing_lead(status="bounced")]))["reply_rate"] is None


def test_compute_adds_same_pure_timing_metrics():
    leads = [timing_lead(status="hot", replied_at="2026-10-06T15:00:00Z")]
    result = compute(leads, [], [], reply_timing_min_sends=1)
    assert result["reply_timing"] == reply_timing(leads, min_sends=1)
    assert result["reply_timing"]["totals"]["positive_reply_rate"] == 1


def test_repeated_fall_dst_hour_combines_sends_without_losing_elapsed_time():
    leads = [
        timing_lead(0, sent_at="2026-11-01T01:15:00-05:00",
                    replied_at="2026-11-01T01:45:00-06:00"),
        timing_lead(1, sent_at="2026-11-01T01:15:00-06:00",
                    replied_at="2026-11-01T02:45:00-06:00"),
    ]
    result = reply_timing(leads)
    assert timing_cell(result, 6, 1)["sent"] == 2
    assert result["time_to_first_reply"]["median_seconds"] == 5400


@pytest.mark.parametrize("classification", ["neutral", "negative", "unsubscribe"])
def test_fully_classified_zero_positive_rate_differs_from_unavailable(classification):
    leads = [timing_lead(i) for i in range(10)]
    replies = [{"lead_email": "person0@example.com", "category": "human",
                "received_at": "2026-10-06T15:00:00Z", "classification": classification}]
    result = reply_timing(leads, replies=replies)
    cell = timing_cell(result)
    assert cell["positive_rate_available"]
    assert cell["positive_reply_rate"] == 0
    assert cell["classification_coverage"] == 1

"""Positive replies land on Hot Leads once and alert the board once.

No network: scan_mailbox is replaced with canned records, the Sheet with
in-memory worksheets, and the sender with a recorder.
"""
from datetime import datetime, timezone

import pytest

from src import replies
from src.replies import HUMAN, ReplyRecord, ScanResult, sync_replies
from src.sheets import HOT_LEADS_HEADERS, SheetClient

LEADS = [
    {"email": "pat@example.com", "name": "Pat Doe", "company": "Acme"},
    {"email": "sam@example.com", "name": "Sam Roe", "company": "Beta"},
]


class FakeWorksheet:
    def __init__(self, records=(), header=None):
        self.records = list(records)
        self.rows = [header] if header else []

    def get_all_records(self):
        return self.records

    def col_values(self, col):
        return [r[col - 1] for r in self.rows]

    def append_rows(self, rows, value_input_option=None):
        self.rows.extend(rows)


class FakeBook:
    def __init__(self, tabs):
        self.tabs = tabs

    def worksheet(self, title):
        return self.tabs[title]


class FakeSheets(SheetClient):
    """The real hot-lead methods over in-memory tabs; the other writes are recorded."""

    def __init__(self):
        self.sheet_id = "test-sheet"
        self.hot = FakeWorksheet(header=HOT_LEADS_HEADERS)
        self.book = FakeBook({
            "Drafts": FakeWorksheet(),
            "Leads": FakeWorksheet(LEADS),
            "Hot Leads": self.hot,
        })
        self.replies_written = None

    def replace_replies(self, records):
        self.replies_written = list(records)

    def bulk_update_leads(self, updates):
        return len(updates)

    @property
    def hot_rows(self):
        return [dict(zip(HOT_LEADS_HEADERS, r)) for r in self.hot.rows[1:]]


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, body, **kwargs):
        self.sent.append({"to": to, "subject": subject, "body": body})


def reply(email, classification, msg_id):
    return ReplyRecord(
        lead_email=email,
        category=HUMAN,
        received_at=datetime(2026, 10, 1, 15, tzinfo=timezone.utc),
        subject="Re: Student consulting project",
        snippet="Sounds interesting, can we talk next week?",
        from_email=email,
        classification=classification,
        classification_reason="asks for a call",
        message_id=f"<{msg_id}@mail.example.com>",
    )


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setenv("DIGEST_RECIPIENT", "board@example.com")
    sheets, sender = FakeSheets(), FakeSender()

    def scan(*records, dry_run=False):
        monkeypatch.setattr(replies, "scan_mailbox", lambda **kw: ScanResult(records=list(records)))
        sync_replies(sheets, classify=False, dry_run=dry_run, sender=sender)

    return sheets, sender, scan


def test_positive_reply_adds_row_and_alerts(run):
    sheets, sender, scan = run
    scan(reply("pat@example.com", "positive", "abc123"))

    link = "https://mail.google.com/mail/u/0/#search/rfc822msgid:abc123%40mail.example.com"
    [row] = sheets.hot_rows
    assert (row["name"], row["company"], row["email"]) == ("Pat Doe", "Acme", "pat@example.com")
    assert row["thread_link"] == link
    assert row["owner"] == row["next_step"] == ""

    [mail] = sender.sent
    assert mail["to"] == "board@example.com"
    assert "Pat Doe" in mail["subject"]
    for text in ("Acme", "asks for a call", "can we talk next week", link):
        assert text in mail["body"]


def test_rescan_does_not_repeat(run):
    sheets, sender, scan = run
    scan(reply("pat@example.com", "positive", "abc123"))
    scan(reply("pat@example.com", "positive", "abc123"))

    assert len(sheets.hot_rows) == 1
    assert len(sender.sent) == 1


@pytest.mark.parametrize("classification", ["neutral", "negative"])
def test_non_positive_reply_does_nothing(run, classification):
    sheets, sender, scan = run
    scan(reply("pat@example.com", classification, "abc123"))

    assert sheets.hot_rows == []
    assert sender.sent == []


def test_two_positives_one_email(run):
    sheets, sender, scan = run
    scan(reply("pat@example.com", "positive", "abc123"),
         reply("sam@example.com", "positive", "def456"))

    assert len(sheets.hot_rows) == 2
    [mail] = sender.sent
    assert "2 hot leads" in mail["subject"]
    assert "Pat Doe" in mail["body"] and "Sam Roe" in mail["body"]


def test_dry_run_writes_and_sends_nothing(run):
    sheets, sender, scan = run
    scan(reply("pat@example.com", "positive", "abc123"), dry_run=True)

    assert sheets.hot_rows == []
    assert sheets.replies_written is None
    assert sender.sent == []


def test_failed_alert_is_retried_next_scan(run):
    sheets, sender, scan = run
    sender.send = lambda **kw: (_ for _ in ()).throw(RuntimeError("SMTP down"))
    with pytest.raises(RuntimeError):
        scan(reply("pat@example.com", "positive", "abc123"))
    assert sheets.hot_rows == []

    del sender.send  # back to the recording method
    scan(reply("pat@example.com", "positive", "abc123"))
    assert len(sheets.hot_rows) == 1
    assert len(sender.sent) == 1

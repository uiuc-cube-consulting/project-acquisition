"""Tests for cmd_send's last-moment suppression check (issue #31).

Everything is faked: no Google Sheet, no SMTP, no network.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src import gmail_send, main
from src.models import Draft, TemplateType


def _draft(email: str, follow_up: bool = False) -> Draft:
    return Draft(
        lead_email=email,
        prepared_at=datetime.now(timezone.utc),
        template_used=TemplateType.BUSINESS,
        subject="Hello",
        body="Body",
        approved=True,
        is_follow_up=follow_up,
        in_reply_to="<orig@example.com>" if follow_up else None,
    )


class FakeSheets:
    """Stands in for SheetClient. Set `drafts` and `suppressed` per test."""
    drafts: list = []
    suppressed: set = set()

    def __init__(self):
        self.errors: dict[int, str] = {}
        self.sent_rows: list[int] = []
        FakeSheets.last = self

    def drafts_activity_today(self):
        return 0, 0

    def list_approved_pending(self):
        return [(i, d) for i, d in FakeSheets.drafts if i not in self.sent_rows and i not in self.errors]

    def get_sent_emails(self):
        return set()

    def get_suppression_emails(self):
        return set(FakeSheets.suppressed)

    def mark_draft_error(self, row_idx, error):
        self.errors[row_idx] = error

    def mark_draft_sent(self, row_idx, message_id):
        self.sent_rows.append(row_idx)

    def update_lead_status(self, *args, **kwargs):
        pass


class FakeSender:
    sent_to: list[str] = []

    def send(self, to, **kwargs):
        FakeSender.sent_to.append(to)
        return "<msg-id@example.com>", "thread-1"


@pytest.fixture
def run_send(monkeypatch):
    FakeSender.sent_to = []
    monkeypatch.setattr(main, "SheetClient", FakeSheets)
    monkeypatch.setattr(gmail_send, "GmailSender", FakeSender)
    # These run after a successful send and would otherwise hit the network.
    monkeypatch.setattr(main, "send_daily_summary", lambda **kw: None)
    monkeypatch.setattr(main, "_refresh_dashboard", lambda sheets: None)

    def _run(drafts, suppressed):
        FakeSheets.drafts = drafts
        FakeSheets.suppressed = suppressed
        assert main.cmd_send(dry_run=False, force=True) == 0
        return FakeSheets.last

    return _run


def test_suppressed_initial_draft_not_sent(run_send):
    sheets = run_send([(2, _draft("gone@example.com"))], {"gone@example.com"})
    assert FakeSender.sent_to == []
    assert sheets.errors == {2: "skipped: suppressed"}


def test_suppressed_follow_up_not_sent(run_send):
    sheets = run_send([(3, _draft("gone@example.com", follow_up=True))], {"gone@example.com"})
    assert FakeSender.sent_to == []
    assert sheets.errors == {3: "skipped: suppressed"}


def test_mixed_case_email_still_caught(run_send):
    sheets = run_send([(4, _draft("Gone.Person@Example.com"))], {"gone.person@example.com"})
    assert FakeSender.sent_to == []
    assert sheets.errors == {4: "skipped: suppressed"}


def test_unsuppressed_draft_sent(run_send):
    sheets = run_send(
        [(5, _draft("hello@example.com")), (6, _draft("gone@example.com"))],
        {"gone@example.com"},
    )
    assert FakeSender.sent_to == ["hello@example.com"]
    assert sheets.sent_rows == [5]
    assert sheets.errors == {6: "skipped: suppressed"}

"""Send retries: exhausted / permanent / skipped drafts leave the pending queue (#52)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import smtplib

from src.gmail_send import format_send_error, is_permanent_send_failure
from src.models import Draft, TemplateType
from src.sheets import (
    DRAFTS_HEADERS,
    SheetClient,
    draft_row_still_pending,
)


def test_draft_row_still_pending_filters():
    base = {"approved": "TRUE", "sent_at": "", "send_error": "", "send_attempts": 0}
    assert draft_row_still_pending(base, max_attempts=3)

    assert not draft_row_still_pending({**base, "send_error": "skipped: suppressed"})
    assert not draft_row_still_pending({**base, "send_error": "permanent: user unknown"})
    assert not draft_row_still_pending({**base, "send_attempts": 3}, max_attempts=3)
    assert draft_row_still_pending({**base, "send_attempts": 2, "send_error": "timeout"}, max_attempts=3)


def test_permanent_send_failure_detection():
    assert is_permanent_send_failure(smtplib.SMTPRecipientsRefused({"a@b.c": (550, b"user unknown")}))
    assert is_permanent_send_failure(smtplib.SMTPResponseException(550, b"mailbox unavailable"))
    assert is_permanent_send_failure(RuntimeError("Invalid address: nope"))
    assert not is_permanent_send_failure(TimeoutError("timed out"))
    assert format_send_error(RuntimeError("user unknown")).startswith("permanent:")
    assert not format_send_error(TimeoutError("timed out")).startswith("permanent:")


class _Cell:
    def __init__(self, value=""):
        self.value = value


class FakeWorksheet:
    def __init__(self, headers, records):
        self.headers = list(headers)
        self.records = [dict(r) for r in records]
        self.col_count = len(headers)
        self.updates: list[tuple] = []
        # row-major cell store for mark_draft_error
        self._cells: dict[tuple[int, int], str] = {}
        for i, row in enumerate(records, start=2):
            for h, v in row.items():
                if h in headers:
                    self._cells[(i, headers.index(h) + 1)] = v

    def get_all_records(self):
        return list(self.records)

    def row_values(self, n):
        return list(self.headers) if n == 1 else []

    def add_cols(self, n):
        self.col_count += n

    def update(self, range_name, values):
        if range_name == "A1":
            self.headers = list(values[0])
            self.col_count = max(self.col_count, len(self.headers))

    def cell(self, row, col):
        return _Cell(self._cells.get((row, col), "0"))

    def update_cell(self, row, col, value):
        self.updates.append((row, col, value))
        self._cells[(row, col)] = value
        if 2 <= row < 2 + len(self.records) and col <= len(self.headers):
            key = self.headers[col - 1]
            self.records[row - 2][key] = value


class FakeBook:
    def __init__(self, tabs):
        self._tabs = tabs
        self._titles = set(tabs)

    def worksheets(self):
        return [type("WS", (), {"title": t})() for t in self._tabs]

    def worksheet(self, title):
        return self._tabs[title]


class PendingSheets(SheetClient):
    def __init__(self, drafts_records):
        self.sheet_id = "test"
        self.drafts = FakeWorksheet(DRAFTS_HEADERS, drafts_records)
        self.book = FakeBook({"Drafts": self.drafts})


def _row(**over):
    base = {
        "prepared_at": "2026-11-03T10:00:00+00:00",
        "lead_email": "pat@example.com",
        "template_used": "business",
        "subject": "Hi",
        "body": "Body",
        "approved": "TRUE",
        "sent_at": "",
        "send_error": "",
        "message_id": "",
        "is_follow_up": "FALSE",
        "in_reply_to": "",
        "send_attempts": 0,
    }
    base.update(over)
    return base


def test_list_approved_pending_skips_final_and_exhausted(monkeypatch):
    monkeypatch.setenv("MAX_SEND_ATTEMPTS", "3")
    sheets = PendingSheets([
        _row(lead_email="ok@example.com"),
        _row(lead_email="skip@example.com", send_error="skipped: suppressed"),
        _row(lead_email="perm@example.com", send_error="permanent: user unknown", send_attempts=1),
        _row(lead_email="tired@example.com", send_error="timeout", send_attempts=3),
        _row(lead_email="retry@example.com", send_error="timeout", send_attempts=2),
    ])
    pending = sheets.list_approved_pending()
    emails = [d.lead_email for _, d in pending]
    assert emails == ["ok@example.com", "retry@example.com"]


def test_mark_draft_error_increments_attempts():
    sheets = PendingSheets([_row(lead_email="a@example.com", send_attempts=1)])
    sheets.mark_draft_error(2, "temporary glitch")
    assert sheets.drafts.records[0]["send_error"] == "temporary glitch"
    assert int(sheets.drafts.records[0]["send_attempts"]) == 2


def test_bootstrap_appends_send_attempts_header():
    old = [h for h in DRAFTS_HEADERS if h != "send_attempts"]
    ws = FakeWorksheet(old, [])
    assert "send_attempts" not in ws.headers

    class BootSheets(SheetClient):
        def __init__(self):
            self.sheet_id = "test"
            self.book = FakeBook({tab: (ws if tab == "Drafts" else FakeWorksheet([], []))
                                  for tab in ("Leads", "Drafts", "Hot Leads", "Suppression",
                                              "Prospects", "Alumni", "Replies", "Runs", "Companies")})
            # minimal stubs for other tabs
            for title, headers in (
                ("Leads", ["email"]),
                ("Hot Leads", ["email"]),
                ("Suppression", ["email"]),
                ("Prospects", ["email"]),
                ("Alumni", ["name"]),
                ("Replies", ["email"]),
                ("Runs", ["run_at"]),
                ("Companies", ["company"]),
            ):
                if title != "Drafts":
                    self.book._tabs[title] = FakeWorksheet(headers, [])

    sheets = BootSheets()
    sheets.bootstrap()
    assert ws.headers[-1] == "send_attempts"
    assert "send_attempts" in ws.headers
    assert ws.headers.index("in_reply_to") == old.index("in_reply_to")

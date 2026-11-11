"""Tests for suppression and send bookkeeping (issues #31 and #51).

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
    sheet_id = "fake-sheet"
    drafts: list = []
    suppressed: set = set()

    def __init__(self):
        self.errors: dict[int, str] = {}
        self.sent_rows: list[int] = []
        FakeSheets.last = self

    def drafts_activity_today(self):
        return 0, 0

    def list_approved_pending(self):
        return [(i, d) for i, d in FakeSheets.drafts if i not in self.sent_rows]

    def get_sent_emails(self):
        return set()

    def get_suppression_emails(self):
        return set(FakeSheets.suppressed)

    def bulk_update_drafts(self, updates):
        for row, fields in updates.items():
            if "sent_at" in fields:
                self.sent_rows.append(row)
            elif "send_error" in fields:
                self.errors[row] = fields["send_error"]

    def bulk_update_leads(self, updates, **kwargs):
        return len(updates)


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


@pytest.mark.parametrize("failed_tabs", [set(), {"Drafts"}, {"Leads"}, {"Drafts", "Leads"}])
@pytest.mark.parametrize("follow_up", [False, True])
def test_bookkeeping_and_second_run(monkeypatch, failed_tabs, follow_up):
    class DurableSheets(FakeSheets):
        def __init__(self):
            super().__init__()
            self.leads = set()
            self.calls = []
            self.reads = 0

        def get_sent_emails(self):
            return self.leads

        def bulk_update_drafts(self, updates):
            self.calls.append("Drafts")
            if "Drafts" in failed_tabs:
                raise RuntimeError("offline")
            super().bulk_update_drafts(updates)

        def bulk_update_leads(self, updates, **kwargs):
            self.calls.append("Leads")
            if "Leads" in failed_tabs:
                raise RuntimeError("offline")
            self.leads.update(updates)

        def list_approved_pending(self):
            self.reads += 1
            if self.reads == 2:
                raise RuntimeError("summary read failed")
            return super().list_approved_pending()

    FakeSheets.drafts = [(2, _draft("person@example.com", follow_up=follow_up))]
    FakeSheets.suppressed = set()
    sheets = DurableSheets()
    summaries = []
    FakeSender.sent_to = []
    monkeypatch.setattr(main, "SheetClient", lambda: sheets)
    monkeypatch.setattr(gmail_send, "GmailSender", FakeSender)
    monkeypatch.setattr(main, "send_daily_summary", lambda **kw: summaries.append(kw))
    monkeypatch.setattr(main, "_refresh_dashboard", lambda _: None)
    assert main.cmd_send(False, force=True) == bool(failed_tabs)
    assert sheets.calls == ["Drafts", "Leads"]
    assert summaries[0]["drafts_pending"] is None
    assert len(summaries[0]["unrecorded"]) == len(failed_tabs)
    main.cmd_send(False, force=True)
    assert len(FakeSender.sent_to) == (
        2 if "Drafts" in failed_tabs and (follow_up or "Leads" in failed_tabs) else 1
    )


def test_quota_retry_batches_without_gmail(monkeypatch):
    from types import SimpleNamespace
    from gspread.exceptions import APIError
    from src import sheets as module
    from src.sheets import SheetClient

    attempts = []
    sleeps = []

    def write(payload):
        attempts.append(payload)
        if len(attempts) < 3:
            raise APIError(SimpleNamespace(status_code=429, json=lambda: {
                "error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}))

    client = SheetClient.__new__(SheetClient)
    client.book = SimpleNamespace(values_batch_update=write)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    client.bulk_update_drafts({i: {"sent_at": "2026-11-11", "message_id": f"<{i}@example.com>"}
                               for i in range(2, 102)})
    assert sleeps == [1, 2]
    assert len(attempts) == 3
    assert len(attempts[0]["data"]) == 200
    assert attempts[0] == attempts[-1]


@pytest.mark.parametrize("code, expected", [(429, 4), (403, 1)])
def test_retry_is_bounded_and_only_for_429(monkeypatch, code, expected):
    from types import SimpleNamespace
    from gspread.exceptions import APIError
    from src import sheets
    calls = []

    def fail():
        calls.append(1)
        raise APIError(SimpleNamespace(status_code=code, json=lambda: {
            "error": {"code": code, "message": "failed", "status": "ERROR"}}))

    monkeypatch.setattr(sheets.time, "sleep", lambda _: None)
    with pytest.raises(APIError):
        sheets.retry_sheet_write(fail)
    assert len(calls) == expected


def test_dry_run_writes_nothing(monkeypatch):
    FakeSheets.drafts = [(2, _draft("person@example.com")), (3, _draft("gone@example.com"))]
    FakeSheets.suppressed = {"gone@example.com"}
    sheets = FakeSheets()
    monkeypatch.setattr(main, "SheetClient", lambda: sheets)
    monkeypatch.setattr(gmail_send, "GmailSender", FakeSender)
    monkeypatch.setattr(sheets, "bulk_update_drafts", lambda _: pytest.fail("dry-run write"))
    monkeypatch.setattr(sheets, "bulk_update_leads", lambda *a, **kw: pytest.fail("dry-run write"))
    assert main.cmd_send(True, force=True) == 0


@pytest.mark.parametrize("size", [1, 40])
def test_skips_and_send_failures_still_use_two_batches(monkeypatch, size):
    class Sheets(FakeSheets):
        def __init__(self):
            super().__init__()
            self.calls = []

        def bulk_update_drafts(self, updates):
            self.calls.append(("Drafts", updates))

        def bulk_update_leads(self, updates, **kwargs):
            self.calls.append(("Leads", updates))

    class Sender:
        def send(self, to, **kwargs):
            if to.startswith("fail"):
                raise RuntimeError("SMTP rejected")
            return f"<{to}>", "thread"

    FakeSheets.drafts = [(i + 2, _draft(f"skip{i}@example.com")) for i in range(size)]
    FakeSheets.drafts += [(size + 2, _draft("fail@example.com")),
                         (size + 3, _draft("ok@example.com"))]
    FakeSheets.suppressed = {f"skip{i}@example.com" for i in range(size)}
    sheets = Sheets()
    monkeypatch.setattr(main, "SheetClient", lambda: sheets)
    monkeypatch.setattr(gmail_send, "GmailSender", Sender)
    monkeypatch.setattr(main, "send_daily_summary", lambda **_: None)
    monkeypatch.setattr(main, "_refresh_dashboard", lambda _: None)
    assert main.cmd_send(False, force=True) == 0
    assert [tab for tab, _ in sheets.calls] == ["Drafts", "Leads"]
    assert len(sheets.calls[0][1]) == size + 2
    assert set(sheets.calls[1][1]) == {"ok@example.com"}


def test_summary_includes_unrecorded_without_sheet_access(monkeypatch):
    from src import summary
    messages = []

    class Sender:
        def __init__(self, **kwargs):
            pass

        def send(self, **kwargs):
            messages.append(kwargs)

    monkeypatch.setenv("APPROVER_EMAIL", "review@example.com")
    monkeypatch.setattr(summary, "GmailSender", Sender)
    summary.send_daily_summary(1, 0, None, ["person@example.com; message ID <id@example.com>; Drafts"], "fake")
    assert "Sent but not recorded — check the Sheet" in messages[0]["body"]
    assert "person@example.com" in messages[0]["body"]
    assert "unknown" in messages[0]["body"]


def _quota_error():
    from types import SimpleNamespace
    from gspread.exceptions import APIError
    return APIError(SimpleNamespace(status_code=429, json=lambda: {
        "error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}))


class MemoryBook:
    """Fake Sheets transport; real SheetClient reads/writes share durable rows."""

    def __init__(self, drafts):
        from src.sheets import DRAFTS_HEADERS, LEADS_HEADERS, _draft_to_row
        self.headers = {"Drafts": DRAFTS_HEADERS, "Leads": LEADS_HEADERS}
        self.rows = {
            "Drafts": [dict(zip(DRAFTS_HEADERS, _draft_to_row(d))) for d in drafts],
            "Leads": [{"email": d.lead_email} for d in drafts],
        }
        self.failures = {}
        self.writes = []
        self.reads = []

    def worksheet(self, tab):
        from types import SimpleNamespace

        def records():
            self.reads.append(tab)
            return [dict(row) for row in self.rows[tab]]

        return SimpleNamespace(get_all_records=records, col_values=lambda _: ["email"])

    def values_batch_update(self, payload):
        from gspread.utils import a1_to_rowcol
        tab = payload["data"][0]["range"].split("!")[0]
        self.writes.append((tab, payload))
        if self.failures.get(tab, 0):
            self.failures[tab] -= 1
            raise _quota_error()
        for entry in payload["data"]:
            tab, cell = entry["range"].split("!")
            row, col = a1_to_rowcol(cell)
            self.rows[tab][row - 2][self.headers[tab][col - 1]] = entry["values"][0][0]


@pytest.fixture
def memory_send(monkeypatch):
    from src import sheets as module
    summaries = []
    sent = []
    sleeps = []

    class Sender:
        def send(self, to, **kwargs):
            sent.append(to)
            return f"<message-{len(sent)}@example.com>", f"thread-{len(sent)}"

    monkeypatch.setenv("DAILY_SEND_CAP", "100")
    monkeypatch.setattr(gmail_send, "GmailSender", Sender)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(main, "send_daily_summary", lambda **kw: summaries.append(kw))
    monkeypatch.setattr(main, "_refresh_dashboard", lambda _: None)

    def setup(drafts):
        book = MemoryBook(drafts)

        def client():
            instance = module.SheetClient.__new__(module.SheetClient)
            instance.book = book
            instance.sheet_id = "fake-sheet"
            return instance

        monkeypatch.setattr(main, "SheetClient", client)
        return book, sent, summaries, sleeps

    return setup


@pytest.mark.parametrize("follow_up", [False, True])
def test_real_sheet_helpers_recover_429_without_resending(memory_send, follow_up):
    book, sent, summaries, sleeps = memory_send([_draft("person@example.com", follow_up)])
    # A follow-up must preserve the initial contact date, not clear it.
    book.rows["Leads"][0]["sent_at"] = "2026-01-01T00:00:00+00:00" if follow_up else ""
    book.failures = {"Drafts": 2, "Leads": 1}
    assert main.cmd_send(False, force=True) == 0
    assert sleeps == [1, 2, 1]
    assert sent == ["person@example.com"]
    draft, lead = book.rows["Drafts"][0], book.rows["Leads"][0]
    assert draft["message_id"] == lead["message_id"] == "<message-1@example.com>"
    assert lead["thread_id"] == "thread-1"
    assert draft["sent_at"]
    assert lead["status"] == ("followed-up" if follow_up else "sent")
    if follow_up:
        assert lead["sent_at"] == "2026-01-01T00:00:00+00:00"
        assert lead["last_follow_up_at"] == draft["sent_at"]
    else:
        assert lead["sent_at"] == draft["sent_at"]
    assert summaries[0]["unrecorded"] == []
    assert main.cmd_send(False, force=True) == 0
    assert sent == ["person@example.com"]


@pytest.mark.parametrize("follow_up", [False, True])
@pytest.mark.parametrize("failed_tabs", [{"Drafts"}, {"Leads"}, {"Drafts", "Leads"}])
def test_real_sheet_helpers_persistent_429_and_second_run(memory_send, follow_up, failed_tabs, caplog):
    book, sent, summaries, _ = memory_send([_draft("person@example.com", follow_up)])
    if follow_up:
        book.rows["Leads"][0]["sent_at"] = "2026-01-01T00:00:00+00:00"
    book.failures = {tab: float("inf") for tab in failed_tabs}
    assert main.cmd_send(False, force=True) == 1
    assert sent == ["person@example.com"]
    assert len(summaries[0]["unrecorded"]) == len(failed_tabs)
    for tab in failed_tabs:
        assert sum(name == tab for name, _ in book.writes) == 4
        assert any(f"not confirmed in {tab}" in entry for entry in summaries[0]["unrecorded"])
    assert "message-1@example.com" in caplog.text
    # Recover the service, then start a fresh SheetClient against durable state.
    book.failures = {}
    assert main.cmd_send(False, force=True) == 0
    # A Leads sent_at guards only initial mail; follow-ups intentionally bypass it.
    resend = "Drafts" in failed_tabs and (follow_up or "Leads" in failed_tabs)
    assert len(sent) == (2 if resend else 1)


@pytest.mark.parametrize("size", [1, 40])
def test_real_sheet_helpers_have_constant_call_count(memory_send, size):
    book, sent, _, _ = memory_send([_draft(f"person{i}@example.com") for i in range(size)])
    assert main.cmd_send(False, force=True) == 0
    assert len(sent) == size
    assert [tab for tab, _ in book.writes] == ["Drafts", "Leads"]
    assert len(book.reads) == 6  # activity, pending, dedupe x2, bulk Leads, summary


def test_interruption_before_batch_writes_can_resend_all_accepted_mail(memory_send, monkeypatch):
    book, sent, summaries, _ = memory_send([
        _draft("first@example.com"), _draft("second@example.com"),
    ])
    normal_sender = gmail_send.GmailSender

    class InterruptedSender(normal_sender):
        def send(self, to, **kwargs):
            if to == "second@example.com":
                raise KeyboardInterrupt("simulated process interruption")
            return super().send(to, **kwargs)

    monkeypatch.setattr(gmail_send, "GmailSender", InterruptedSender)
    with pytest.raises(KeyboardInterrupt):
        main.cmd_send(False, force=True)
    assert sent == ["first@example.com"]
    assert book.writes == []
    assert summaries == []
    # Nothing durable distinguishes the accepted first send from an unsent draft.
    monkeypatch.setattr(gmail_send, "GmailSender", normal_sender)
    assert main.cmd_send(False, force=True) == 0
    assert sent == ["first@example.com", "first@example.com", "second@example.com"]


def test_missing_lead_is_reported_but_other_leads_are_recorded(memory_send):
    book, sent, summaries, _ = memory_send([
        _draft("present@example.com"), _draft("missing@example.com"),
    ])
    book.rows["Leads"].pop()
    assert main.cmd_send(False, force=True) == 1
    assert len(sent) == 2
    assert book.rows["Leads"][0]["sent_at"]
    assert len(summaries[0]["unrecorded"]) == 1
    assert "missing@example.com" in summaries[0]["unrecorded"][0]
    assert "not confirmed in Leads" in summaries[0]["unrecorded"][0]
    assert all(row["sent_at"] for row in book.rows["Drafts"])
    assert main.cmd_send(False, force=True) == 0
    assert len(sent) == 2


def test_approval_dedupe_and_daily_cap_use_durable_rows(memory_send, monkeypatch):
    book, sent, _, _ = memory_send([
        _draft("already@example.com"), _draft("unapproved@example.com"),
        _draft("first@example.com"), _draft("first@example.com"),
        _draft("later@example.com"),
    ])
    book.rows["Drafts"][0]["sent_at"] = datetime.now(timezone.utc).isoformat()
    book.rows["Drafts"][1]["approved"] = "FALSE"
    monkeypatch.setenv("DAILY_SEND_CAP", "2")
    assert main.cmd_send(False, force=True) == 0
    assert sent == ["first@example.com"]
    assert main.cmd_send(False, force=True) == 0
    assert sent == ["first@example.com"]


def test_post_send_leads_read_failure_still_attempts_summary(memory_send, monkeypatch):
    book, sent, summaries, _ = memory_send([_draft("person@example.com")])
    worksheet = book.worksheet

    def failing_worksheet(tab):
        ws = worksheet(tab)
        if tab == "Leads" and "Leads" in book.reads:
            def fail():
                raise RuntimeError("Leads read unavailable")
            ws.get_all_records = fail
        return ws

    monkeypatch.setattr(book, "worksheet", failing_worksheet)
    assert main.cmd_send(False, force=True) == 1
    assert sent == ["person@example.com"]
    assert "not confirmed in Leads" in summaries[0]["unrecorded"][0]
    assert book.rows["Drafts"][0]["sent_at"]

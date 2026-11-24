"""Follow-through reminders (#54): the pure rules, thread detection, and the
`remind` command over in-memory tabs. No network; addresses are example.com."""
from datetime import date, datetime, timezone
from email.message import Message

import pytest

from src import main
from src.follow_through import (
    DUE, NO_NEXT_STEP, REPLY_OWED, UNOWNED, business_days_between, by_owner,
    due_reminders, format_digest, owner_address, reminder_reasons,
)
from src.replies import HUMAN, ReplyRecord, _fetch_items, _note_latest, waiting_since

# Fri 2026-11-06, weekend, Mon 11-09 ... Wed 11-11 (Veterans Day in the config)
VETERANS_DAY = [date(2026, 11, 11)]


# ---------------- business days ----------------

def test_business_days_skip_weekends():
    assert business_days_between(date(2026, 11, 6), date(2026, 11, 10)) == 2


def test_business_days_skip_blackouts():
    assert business_days_between(date(2026, 11, 9), date(2026, 11, 12), VETERANS_DAY) == 2
    assert business_days_between(date(2026, 11, 9), date(2026, 11, 12), ["2026-11-11"]) == 2


def test_business_days_read_utc_timestamps_as_central_dates():
    # 03:00 UTC Tuesday is still Monday evening in Chicago.
    assert business_days_between("2026-11-10T03:00:00+00:00", date(2026, 11, 10)) == 1


def test_business_days_never_negative_or_crash_on_junk():
    assert business_days_between(date(2026, 11, 10), date(2026, 11, 9)) == 0
    assert business_days_between("", date(2026, 11, 9)) == 0
    assert business_days_between("not a date", date(2026, 11, 9)) == 0


# ---------------- the four rules ----------------

@pytest.mark.parametrize("today, expected", [
    (date(2026, 11, 10), False),  # Mon, Tue: 2 business days, not more
    (date(2026, 11, 11), True),   # 3
])
def test_reply_owed_after_more_than_two_business_days(today, expected):
    lead = {"owner": "Alex", "next_step": "call", "flagged_at": "2026-11-06",
            "waiting_since": "2026-11-06T15:00:00+00:00"}
    assert (REPLY_OWED in reminder_reasons(lead, today)) is expected


def test_reply_owed_counts_blackouts_out():
    lead = {"owner": "Alex", "next_step": "call", "waiting_since": "2026-11-09"}
    # Tue, Wed (blackout), Thu: only 2 business days.
    assert REPLY_OWED not in reminder_reasons(lead, date(2026, 11, 12), VETERANS_DAY)
    assert REPLY_OWED in reminder_reasons(lead, date(2026, 11, 13), VETERANS_DAY)


def test_answered_thread_owes_nothing():
    lead = {"owner": "Alex", "next_step": "call", "waiting_since": ""}
    assert reminder_reasons(lead, date(2026, 12, 1)) == []


def test_reply_owed_threshold_is_configurable():
    lead = {"owner": "Alex", "next_step": "call", "waiting_since": "2026-11-09"}
    assert REPLY_OWED in reminder_reasons(lead, date(2026, 11, 11), reply_owed_days=1)


def test_unowned():
    lead = {"owner": "", "flagged_at": "2026-11-09"}
    assert UNOWNED not in reminder_reasons(lead, date(2026, 11, 10))
    assert UNOWNED in reminder_reasons(lead, date(2026, 11, 11))


def test_unowned_across_a_weekend():
    lead = {"owner": "", "flagged_at": "2026-11-06"}  # Friday
    assert UNOWNED not in reminder_reasons(lead, date(2026, 11, 9))  # Monday


def test_no_next_step():
    lead = {"owner": "Alex", "next_step": "", "flagged_at": "2026-11-06"}
    assert NO_NEXT_STEP not in reminder_reasons(lead, date(2026, 11, 11))
    assert NO_NEXT_STEP in reminder_reasons(lead, date(2026, 11, 12))


def test_next_step_clears_no_next_step():
    lead = {"owner": "Alex", "next_step": "send deck", "flagged_at": "2026-11-01"}
    assert reminder_reasons(lead, date(2026, 11, 20)) == []


def test_due_today_or_past():
    assert DUE in reminder_reasons({"owner": "A", "next_step": "x", "next_touch": "2026-11-12"},
                                   date(2026, 11, 12))
    assert DUE not in reminder_reasons({"owner": "A", "next_step": "x", "next_touch": "2026-11-13"},
                                       date(2026, 11, 12))


# ---------------- ledger and grouping ----------------

HOT = [
    {"email": "pat@example.com", "name": "Pat Doe", "company": "Acme",
     "flagged_at": "2026-11-02", "owner": "", "next_step": ""},
    {"email": "sam@example.com", "name": "Sam Roe", "company": "Beta",
     "flagged_at": "2026-11-02", "owner": "lee@example.com", "next_step": "call",
     "next_touch": "2026-11-10"},
]
REPLIES = [
    {"lead_email": "pat@example.com", "category": HUMAN, "waiting_since": "2026-11-03T15:00:00+00:00"},
    {"lead_email": "sam@example.com", "category": HUMAN, "waiting_since": ""},
]


def test_due_reminders_combine_tabs():
    got = {(r.email, r.reason) for r in due_reminders(HOT, REPLIES, [], date(2026, 11, 10))}
    assert got == {
        ("pat@example.com", REPLY_OWED),
        ("pat@example.com", UNOWNED),
        ("sam@example.com", DUE),
    }


@pytest.mark.parametrize("last_sent, repeats", [
    ("2026-11-10T14:00:00+00:00", False),  # same day
    ("2026-11-09T14:00:00+00:00", False),  # 1 business day ago
    ("2026-11-06T14:00:00+00:00", True),   # Fri -> Tue is 2
])
def test_ledger_limits_repeats_to_every_two_business_days(last_sent, repeats):
    history = [{"lead_email": "PAT@example.com", "reminder_type": UNOWNED, "sent_at": last_sent}]
    got = {(r.email, r.reason) for r in due_reminders(HOT, REPLIES, history, date(2026, 11, 10))}
    assert (("pat@example.com", UNOWNED) in got) is repeats
    assert ("pat@example.com", REPLY_OWED) in got  # other reasons unaffected


def test_duplicate_hot_lead_rows_remind_once():
    got = due_reminders(HOT + [dict(HOT[0])], REPLIES, [], date(2026, 11, 10))
    assert sum(r.email == "pat@example.com" and r.reason == UNOWNED for r in got) == 1


def test_owner_address():
    assert owner_address(" Lee@Example.com ") == "lee@example.com"
    assert owner_address("Lee") is None
    assert owner_address("lee@localhost") is None


def test_by_owner_groups_only_email_owners():
    reminders = due_reminders(HOT, REPLIES, [], date(2026, 11, 10))
    assert {k: [r.email for r in v] for k, v in by_owner(reminders).items()} == {
        "lee@example.com": ["sam@example.com"],
    }


def test_digest_groups_by_reason():
    body = format_digest(due_reminders(HOT, REPLIES, [], date(2026, 11, 10)), "https://sheet")
    assert body.index("Reply owed (1)") < body.index("Due (1)") < body.index("Unowned (1)")
    assert "No next step" not in body
    assert "Pat Doe — Acme <pat@example.com>" in body
    assert "https://sheet" in body


# ---------------- thread detection (replies scan) ----------------

def _head(sender, when):
    m = Message()
    m["From"] = sender
    m["Date"] = when
    return m


def _record(thread_id="111"):
    return ReplyRecord(
        lead_email="pat@example.com", category=HUMAN,
        received_at=datetime(2026, 11, 3, 15, tzinfo=timezone.utc),
        subject="Re: hi", snippet="", from_email="pat@example.com", thread_id=thread_id,
    )


def test_prospect_wrote_last_so_waiting_on_us():
    latest = {}
    _note_latest(latest, "111", _head("outreach@example.com", "Mon, 2 Nov 2026 15:00:00 +0000"))
    _note_latest(latest, "111", _head("Pat Doe <Pat@example.com>", "Tue, 3 Nov 2026 15:00:00 +0000"))
    assert waiting_since(_record(), latest) == datetime(2026, 11, 3, 15, tzinfo=timezone.utc)


def test_our_later_reply_clears_it_whatever_the_fetch_order():
    latest = {}
    _note_latest(latest, "111", _head("Board Member <board@example.com>", "Wed, 4 Nov 2026 09:00:00 -0600"))
    _note_latest(latest, "111", _head("pat@example.com", "Tue, 3 Nov 2026 15:00:00 +0000"))
    assert waiting_since(_record(), latest) is None


def test_no_thread_id_never_guesses():
    assert waiting_since(_record(thread_id=""), {}) is None


def test_fetch_items_reads_thread_id_before_or_after_the_literal():
    chunk = [
        (b"7 (X-GM-THRID 111 BODY[HEADER.FIELDS (FROM)] {10}", b"From: a\r\n\r\n"),
        b")",
        (b"8 (BODY[HEADER.FIELDS (FROM)] {10}", b"From: b\r\n\r\n"),
        b" X-GM-THRID 222)",
        (b"9 (BODY[] {10}", b"From: c\r\n\r\n"),
        b")",
    ]
    assert [(s, t) for s, t, _ in _fetch_items(chunk)] == [(b"7", "111"), (b"8", "222"), (b"9", "")]


# ---------------- the command ----------------

class FakeSheets:
    sheet_id = "test-sheet"

    def __init__(self, hot=HOT, replies=REPLIES, history=()):
        self.hot, self.replies, self.history = list(hot), list(replies), list(history)
        self.logged = []

    def get_hot_leads(self):
        return self.hot

    def get_replies(self):
        return self.replies

    def get_reminder_history(self):
        return self.history

    def append_reminders(self, items):
        self.logged.extend(items)


class FakeSender:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    def send(self, to, subject, body, **kwargs):
        if to in self.fail_for:
            raise RuntimeError("smtp down")
        self.sent.append({"to": to, "subject": subject, "body": body})


@pytest.fixture
def tuesday(monkeypatch):
    monkeypatch.setenv("DIGEST_RECIPIENT", "board@example.com")
    monkeypatch.setattr(main, "now_ct", lambda: datetime(2026, 11, 10, 17, tzinfo=timezone.utc))


def test_remind_sends_one_digest_plus_owner_email_and_logs(tuesday):
    sheets, sender = FakeSheets(), FakeSender()
    assert main.cmd_remind(dry_run=False, sheets=sheets, sender=sender) == 0
    assert [m["to"] for m in sender.sent] == ["board@example.com", "lee@example.com"]
    assert sender.sent[0]["subject"] == "Follow-through: 2 hot leads need attention"
    assert "pat@example.com" not in sender.sent[1]["body"]
    assert sorted(sheets.logged) == [
        ("pat@example.com", REPLY_OWED), ("pat@example.com", UNOWNED), ("sam@example.com", DUE),
    ]


def test_remind_second_run_sends_nothing(tuesday):
    sheets, sender = FakeSheets(), FakeSender()
    main.cmd_remind(dry_run=False, sheets=sheets, sender=sender)
    rerun = FakeSheets(history=[
        {"lead_email": e, "reminder_type": r, "sent_at": "2026-11-10T17:00:00+00:00"}
        for e, r in sheets.logged
    ])
    again = FakeSender()
    assert main.cmd_remind(dry_run=False, sheets=rerun, sender=again) == 0
    assert again.sent == [] and rerun.logged == []


def test_remind_empty_list_sends_nothing(tuesday):
    sheets, sender = FakeSheets(hot=[]), FakeSender()
    assert main.cmd_remind(dry_run=False, sheets=sheets, sender=sender) == 0
    assert sender.sent == [] and sheets.logged == []


def test_remind_dry_run_prints_and_writes_nothing(tuesday, capsys):
    sheets, sender = FakeSheets(), FakeSender()
    assert main.cmd_remind(dry_run=True, sheets=sheets, sender=sender) == 0
    out = capsys.readouterr().out
    assert "To: board@example.com" in out and "Reply owed (1)" in out
    assert sender.sent == [] and sheets.logged == []


def test_remind_skips_weekends_unless_forced(monkeypatch):
    monkeypatch.setenv("DIGEST_RECIPIENT", "board@example.com")
    monkeypatch.setattr(main, "now_ct", lambda: datetime(2026, 11, 14, 17, tzinfo=timezone.utc))
    sheets, sender = FakeSheets(), FakeSender()
    assert main.cmd_remind(dry_run=False, sheets=sheets, sender=sender) == 0
    assert sender.sent == []
    assert main.cmd_remind(dry_run=False, force=True, sheets=sheets, sender=sender) == 0
    assert sender.sent


def test_failed_owner_email_still_logs_and_exits_nonzero(tuesday):
    sheets, sender = FakeSheets(), FakeSender(fail_for={"lee@example.com"})
    assert main.cmd_remind(dry_run=False, sheets=sheets, sender=sender) == 1
    assert [m["to"] for m in sender.sent] == ["board@example.com"]
    assert len(sheets.logged) == 3

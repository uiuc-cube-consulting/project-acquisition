"""Tests for message building in src/gmail_send.py"""
from __future__ import annotations

import pytest

from src import gmail_send
from src.gmail_send import GmailSender, build_message
from src.templates import DEFAULT_UNSUBSCRIBE_MAILTO, unsubscribe_mailto

FROM = "sender@cube.example.com"
TO = "recipient@example.com"
UNSUB = "unsubscribe@example.com"

@pytest.fixture(autouse=True)
def no_smtp(monkeypatch):
    """Fail any test that tries to open a real SMTP connection."""
    def _boom(*args, **kwargs):
        raise AssertionError("tests must never open an SMTP connection")
    monkeypatch.setattr(gmail_send.smtplib, "SMTP_SSL", _boom)


def _msg(**kwargs):
    return build_message(TO, "Subject", "Body text", from_addr=FROM, **kwargs)


def test_list_unsubscribe_present_for_outreach():
    msg = _msg(unsubscribe_mailto=UNSUB)
    assert msg["List-Unsubscribe"] == f"<mailto:{UNSUB}?subject=unsubscribe>"


def test_list_unsubscribe_absent_by_default():
    assert _msg()["List-Unsubscribe"] is None


def test_list_unsubscribe_absent_for_blank_address():
    assert _msg(unsubscribe_mailto="  ")["List-Unsubscribe"] is None


def test_no_one_click_post_header():
    assert _msg(unsubscribe_mailto=UNSUB)["List-Unsubscribe-Post"] is None


def test_follow_up_threading_headers_still_set():
    parent = "<original-123@cube.example.com>"
    msg = _msg(in_reply_to=parent, unsubscribe_mailto=UNSUB)
    assert msg["In-Reply-To"] == parent
    assert msg["References"] == parent


def test_no_threading_headers_on_first_outreach():
    msg = _msg()
    assert msg["In-Reply-To"] is None
    assert msg["References"] is None


def test_message_id_uses_sender_domain():
    msg_id = _msg()["Message-ID"]
    assert msg_id.startswith("<") and msg_id.endswith("@cube.example.com>")


def test_basic_headers_and_body():
    msg = _msg()
    assert msg["From"] == FROM
    assert msg["To"] == TO
    assert msg["Subject"] == "Subject"
    assert msg.get_content().strip() == "Body text"


def test_sender_dry_run_does_not_connect(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", FROM)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "not-a-real-password")
    sender = GmailSender(send_interval_seconds=1)
    msg_id, thread_id = sender.send(
        TO, "Subject", "Body", dry_run=True, unsubscribe_mailto=UNSUB
    )
    assert msg_id == thread_id
    assert msg_id.endswith("@cube.example.com>")


def test_unsubscribe_mailto_env_override(monkeypatch):
    monkeypatch.setenv("UNSUBSCRIBE_MAILTO", UNSUB)
    assert unsubscribe_mailto() == UNSUB


def test_unsubscribe_mailto_default(monkeypatch):
    monkeypatch.delenv("UNSUBSCRIBE_MAILTO", raising=False)
    assert unsubscribe_mailto() == DEFAULT_UNSUBSCRIBE_MAILTO


def test_unsubscribe_mailto_blank_falls_back(monkeypatch):
    monkeypatch.setenv("UNSUBSCRIBE_MAILTO", "")
    assert unsubscribe_mailto() == DEFAULT_UNSUBSCRIBE_MAILTO
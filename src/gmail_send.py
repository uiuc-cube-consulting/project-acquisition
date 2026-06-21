"""SMTP email sender (send-only).

Sends from a single Gmail account using an App Password — no domain-wide
delegation, no OAuth, no inbox reading. Approval happens in the Sheet (set the
`approved` column to yes/TRUE), not by email reply.

Setup: on the sending Google account, turn on 2-Step Verification, create an App
Password (https://myaccount.google.com/apppasswords), then set:
  GMAIL_ADDRESS=you@gmail.com
  GMAIL_APP_PASSWORD=the 16-char app password
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Optional

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


class GmailSender:
    def __init__(self, send_interval_seconds: int | None = None) -> None:
        self.address = os.environ["GMAIL_ADDRESS"]
        self.password = os.environ["GMAIL_APP_PASSWORD"]
        self.interval = int(send_interval_seconds or os.environ.get("SEND_INTERVAL_SECONDS", "30"))
        self._last_sent_at: float = 0.0

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to: Optional[str] = None,
        thread_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> tuple[str, str]:
        """Send an email via Gmail SMTP. Returns (message_id, thread_id).

        We don't read mailboxes, so thread_id is just the message-id (kept for
        signature compatibility and recorded in the Sheet). `in_reply_to` still
        threads follow-ups in the recipient's client via standard headers.
        """
        msg = EmailMessage()
        msg["From"] = self.address
        msg["To"] = to
        msg["Subject"] = subject
        message_id = make_msgid(domain=self.address.split("@")[1])
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)

        if dry_run:
            log.info("[DRY RUN] would send to=%s subject=%s len=%d", to, subject, len(body))
            return message_id, thread_id or message_id

        self._throttle()
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as smtp:
            smtp.login(self.address, self.password)
            smtp.send_message(msg)
        log.info("Sent to %s", to)
        return message_id, thread_id or message_id

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_sent_at
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last_sent_at = time.time()

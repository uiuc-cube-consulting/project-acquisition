"""Gmail API sender using Workspace domain-wide delegation.

Service account impersonates IMPERSONATE_EMAIL (e.g. projectacquisition@cubeconsulting.org).
The service account's `client_id` must be authorized in the Workspace Admin
console for these scopes:

  https://www.googleapis.com/auth/gmail.send
  https://www.googleapis.com/auth/gmail.readonly
  https://www.googleapis.com/auth/gmail.modify

See README for setup steps.
"""
from __future__ import annotations

import base64
import logging
import os
import time
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from .sheets import load_service_account_info

log = logging.getLogger(__name__)

SEND_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]


def _service():
    info = load_service_account_info()
    creds = Credentials.from_service_account_info(info, scopes=SEND_SCOPES)
    delegated = creds.with_subject(os.environ["IMPERSONATE_EMAIL"])
    return build("gmail", "v1", credentials=delegated, cache_discovery=False)


class GmailSender:
    def __init__(self, send_interval_seconds: int | None = None) -> None:
        self.svc = _service()
        self.from_addr = os.environ["IMPERSONATE_EMAIL"]
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
        """Send an email. Returns (message_id, thread_id)."""
        self._throttle()
        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain=self.from_addr.split("@")[1])
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)

        if dry_run:
            log.info("[DRY RUN] would send to=%s subject=%s len=%d", to, subject, len(body))
            return msg["Message-ID"], thread_id or "dry-thread"

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        resp = self.svc.users().messages().send(userId="me", body=payload).execute()
        return msg["Message-ID"], resp["threadId"]

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_sent_at
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last_sent_at = time.time()

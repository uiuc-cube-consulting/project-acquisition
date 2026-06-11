"""Poll Gmail for replies on tracked threads + classify with Gemini.

Approach:
  1. Read the Leads tab; collect all thread_ids where status is sent / followed-up
  2. For each, fetch the latest message via the Gmail API
  3. If the latest message is from someone other than us, treat as a reply
  4. Classify with Gemini into one of ReplyClass
  5. Write the reply back into Sheets:
       - positive => Hot Leads + Leads.status = hot
       - unsubscribe => Suppression + Leads.status = suppressed
       - negative => Leads.status = closed
       - neutral / OOO => leave as sent (don't follow up if OOO)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Iterable

from googleapiclient.errors import HttpError

from .gmail_send import _service
from .llm import generate_json
from .models import LeadStatus, Reply, ReplyClass
from .sheets import SheetClient

log = logging.getLogger(__name__)

CLASSIFY_MODEL = "gemini-2.5-flash-lite"
CLASSIFY_SYSTEM = """You classify replies to a cold outreach email from CUBE Consulting (a student consulting group at UIUC). Output one of:
  positive   - the recipient wants a call, more info, to discuss a project, or expresses warm interest
  neutral    - they ask a clarifying question that isn't a clear yes or no, or hand off to someone else
  negative   - they decline, say not interested, or push back
  ooo        - automated out-of-office or auto-reply, not a real response
  unsubscribe - they ask to be removed, asked us to stop contacting them, or expressed annoyance about being contacted

Return strict JSON: {"class": "...", "reason": "one short sentence"}
"""


def _classify(body: str) -> tuple[ReplyClass, str]:
    data = generate_json(
        model=CLASSIFY_MODEL,
        system=CLASSIFY_SYSTEM,
        prompt=body[:4000],
        max_tokens=200,
    )
    cls_val = data.get("class", "neutral").lower()
    try:
        cls = ReplyClass(cls_val)
    except ValueError:
        cls = ReplyClass.NEUTRAL
    return cls, data.get("reason", "")


def _fetch_latest_inbound(svc, thread_id: str, our_address: str) -> dict | None:
    try:
        thread = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    except HttpError as e:
        log.warning("Thread %s not found: %s", thread_id, e)
        return None
    messages = thread.get("messages", [])
    # Walk backwards for first message NOT from us
    for msg in reversed(messages):
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        from_addr = headers.get("from", "")
        if our_address.lower() not in from_addr.lower():
            return msg
    return None


def _extract_body(msg: dict) -> str:
    """Best-effort text extraction from a Gmail message payload."""
    import base64

    def walk(part):
        mime = part.get("mimeType", "")
        if mime == "text/plain" and "data" in part.get("body", {}):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode(errors="replace")
        for sub in part.get("parts", []):
            t = walk(sub)
            if t:
                return t
        return ""

    return walk(msg["payload"]) or msg.get("snippet", "")


def check_replies(dry_run: bool = False) -> list[Reply]:
    sheets = SheetClient()
    svc = _service()
    our_address = os.environ["IMPERSONATE_EMAIL"]

    leads_ws = sheets.book.worksheet("Leads")
    rows = leads_ws.get_all_records()
    tracked = [
        r for r in rows
        if r.get("thread_id")
        and r.get("status") in (LeadStatus.SENT.value, LeadStatus.FOLLOWED_UP.value)
    ]
    log.info("Checking %d threads for replies", len(tracked))

    out: list[Reply] = []
    for r in tracked:
        msg = _fetch_latest_inbound(svc, r["thread_id"], our_address)
        if not msg:
            continue
        body = _extract_body(msg)
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        from_addr = headers.get("from", "")
        cls, reason = _classify(body)
        reply = Reply(
            thread_id=r["thread_id"],
            from_email=from_addr,
            received_at=datetime.now(timezone.utc),
            snippet=msg.get("snippet", "")[:500],
            classification=cls,
            classification_reason=reason,
        )
        out.append(reply)

        if dry_run:
            log.info("[DRY RUN] would update %s -> %s (%s)", r["email"], cls.value, reason)
            continue

        thread_link = f"https://mail.google.com/mail/u/0/#inbox/{r['thread_id']}"
        if cls == ReplyClass.POSITIVE:
            sheets.append_hot_lead(
                name=r["name"], company=r["company"], email=r["email"],
                linkedin=r.get("linkedin"), reply_excerpt=reply.snippet,
                thread_link=thread_link,
            )
            sheets.update_lead_status(r["email"], LeadStatus.HOT, replied_at=reply.received_at)
        elif cls == ReplyClass.UNSUBSCRIBE:
            sheets.add_suppression(r["email"], "unsubscribe reply")
            sheets.update_lead_status(r["email"], LeadStatus.SUPPRESSED, replied_at=reply.received_at)
        elif cls == ReplyClass.NEGATIVE:
            sheets.update_lead_status(r["email"], LeadStatus.CLOSED, replied_at=reply.received_at)
        elif cls == ReplyClass.OUT_OF_OFFICE:
            # Leave status alone; reply_check still records the reply timestamp
            sheets.update_lead_status(r["email"], LeadStatus.SENT, replied_at=reply.received_at)
        else:  # neutral
            sheets.update_lead_status(r["email"], LeadStatus.REPLIED, replied_at=reply.received_at)

    return out

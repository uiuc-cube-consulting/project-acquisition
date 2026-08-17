"""Inbox reply scanner (read-only IMAP).

The pipeline sends over SMTP and never reads mail, so nothing ever recorded what
came *back* — which made the reply rate look like zero when it isn't. This module
closes that loop: it reads the sending mailbox over IMAP with the same Gmail App
Password, matches each incoming message to a lead we emailed, and sorts it into
one of three buckets.

  bounce      the address doesn't exist / permanently rejects us. This is the
              real measure of how accurate the sourced emails are — a bounce
              means Apollo handed us a wrong address.
  auto_reply  out-of-office and other machine answers. The person exists and the
              mail landed, but they haven't actually read it yet.
  human       a person typed something back. This is the hit rate.

Human replies are then classified by Gemini into positive / neutral / negative /
unsubscribe so "21 replies" can be split into "who actually wants to talk".

Everything is written to the `Replies` tab (one row per person per category) and
mirrored onto the `Leads` tab (`status`, `replied_at`) so every downstream
consumer — the Sheet dashboard and the standalone HTML one — reads real numbers
without needing IMAP access.

Read-only by construction: the mailbox is opened with readonly=True, so this can
never delete, move, or mark anything in the inbox.

Setup: the same GMAIL_ADDRESS + GMAIL_APP_PASSWORD used for sending. IMAP must be
enabled on the account (Gmail → Settings → Forwarding and POP/IMAP).
"""
from __future__ import annotations

import email
import imaplib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime

from .models import LeadStatus, ReplyClass

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
# "All Mail" rather than INBOX so archived replies still count.
MAILBOX = '"[Gmail]/All Mail"'

CLASSIFY_MODEL = "gemini-3.5-flash-lite"

BOUNCE = "bounce"
AUTO_REPLY = "auto_reply"
HUMAN = "human"

# Ordered by how much they matter: a person who eventually typed a reply counts
# as a human reply even if they also auto-replied first.
CATEGORY_RANK = {HUMAN: 3, AUTO_REPLY: 2, BOUNCE: 1}

_BOUNCE_SENDER = re.compile(r"(mailer-daemon|postmaster|mail delivery)", re.I)
_AUTO_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto[-\s]?reply|autoreply|out of (the )?office|"
    r"automatische|réponse automatique|respuesta automática)",
    re.I,
)
# Subject decorations both mail clients and gateways bolt on to our own subject.
_SUBJECT_PREFIX = re.compile(
    r"^\s*((re|fw|fwd|aw|sv|automatic reply|auto[-\s]?reply|out of (the )?office|"
    r"undeliverable|returned mail|delivery status notification)\s*:\s*|"
    r"\[(external|ext|e)\]\s*:?\s*|ext:\s*|\[external\]\s*)+",
    re.I,
)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def normalize_subject(subject: str) -> str:
    """Strip Re:/Automatic reply:/[EXTERNAL] noise so a reply's subject can be
    compared against the subject we originally sent."""
    prev = None
    out = subject or ""
    while out != prev:
        prev = out
        out = _SUBJECT_PREFIX.sub("", out)
    return re.sub(r"\s+", " ", out).strip().lower()


@dataclass
class ReplyRecord:
    """One classified inbound message, already matched to a lead."""

    lead_email: str
    category: str
    received_at: datetime
    subject: str
    snippet: str
    from_email: str
    bounce_reason: str = ""
    classification: str = ""
    classification_reason: str = ""


@dataclass
class ScanResult:
    records: list[ReplyRecord] = field(default_factory=list)
    scanned: int = 0
    unmatched: int = 0


# ---------------- message parsing ----------------

def _body_text(msg: Message) -> str:
    """Flatten the readable parts (including any delivery-status report)."""
    chunks: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype in ("text/plain", "message/delivery-status", "message/rfc822", "text/rfc822-headers"):
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if payload:
                chunks.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
            elif ctype == "message/delivery-status":
                # Some servers nest the status blocks as sub-Messages.
                for sub in part.get_payload() or []:
                    if isinstance(sub, Message):
                        chunks.append(str(sub))
    return "\n".join(chunks)


def _quoted_stripped(text: str) -> str:
    """Drop the quoted original so the snippet is what the person actually wrote."""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"^\s*(on .{5,80}wrote:|from:\s|-{3,}\s*original message)", stripped, re.I):
            break
        lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _bounce_details(body: str, msg: Message) -> tuple[str, str, bool]:
    """(failed_recipient, diagnostic, is_permanent) out of a delivery report.

    `Action: delayed` / a 4.x.x status is a *retry*, not a bounce — the mail may
    still land, so those are dropped rather than counted against deliverability.
    """
    recipient = ""
    m = re.search(r"(?:Final|Original)-Recipient:\s*rfc822;\s*<?([^\s>]+)", body, re.I)
    if m:
        recipient = m.group(1).strip().lower()
    if not recipient:
        failed = msg.get("X-Failed-Recipients")
        if failed:
            recipient = parseaddr(failed)[1].lower()

    action = ""
    m = re.search(r"^Action:\s*(\w+)", body, re.I | re.M)
    if m:
        action = m.group(1).lower()

    status = ""
    m = re.search(r"^Status:\s*([245])\.\d+\.\d+", body, re.I | re.M)
    if m:
        status = m.group(1)

    diagnostic = ""
    m = re.search(r"Diagnostic-Code:\s*[^;]*;\s*(.{0,200})", body, re.I | re.S)
    if m:
        diagnostic = re.sub(r"\s+", " ", m.group(1)).strip()
    if not diagnostic:
        m = re.search(r"\b(55\d[\s-][\d.]+[^\n]{0,120})", body)
        diagnostic = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""

    permanent = status == "5" or (action == "failed" and status != "4")
    if action == "delayed" or status == "4":
        permanent = False
    return recipient, diagnostic[:200], permanent


def classify_message(
    msg: Message,
    *,
    our_message_ids: set[str],
    sent_emails: set[str],
    sent_subjects: set[str],
    our_address: str,
) -> ReplyRecord | None:
    """Turn one raw message into a ReplyRecord, or None if it isn't a response
    to our outreach (newsletters, LinkedIn noise, our own sent copies)."""
    from_email = parseaddr(_decode(msg.get("From")))[1].lower()
    if not from_email or from_email == our_address.lower():
        return None

    subject = _decode(msg.get("Subject"))
    body = _body_text(msg)
    in_reply_to = (msg.get("In-Reply-To") or "").strip()
    references = (msg.get("References") or "").split()

    threaded = in_reply_to in our_message_ids or any(r in our_message_ids for r in references)
    subject_match = normalize_subject(subject) in sent_subjects

    is_bounce = bool(_BOUNCE_SENDER.search(from_email)) or msg.get_content_type() == "multipart/report"
    auto_header = " ".join(filter(None, [
        msg.get("Auto-Submitted", ""),
        msg.get("X-Autoreply", ""),
        msg.get("X-Autorespond", ""),
    ])).lower()
    is_auto = bool(_AUTO_SUBJECT.match(subject)) or "auto-replied" in auto_header or "auto-generated" in auto_header

    if is_bounce:
        recipient, diagnostic, permanent = _bounce_details(body, msg)
        if not recipient:
            # Last resort: an address in the report that we know we emailed.
            for candidate in re.findall(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+", body):
                if candidate.lower() in sent_emails:
                    recipient = candidate.lower()
                    break
        if not recipient or recipient not in sent_emails:
            return None
        if not permanent:
            return None  # transient delay — the message may still be delivered
        return ReplyRecord(
            lead_email=recipient,
            category=BOUNCE,
            received_at=_received_at(msg),
            subject=subject[:200],
            snippet="",
            from_email=from_email,
            bounce_reason=diagnostic,
        )

    # Non-bounce: it must tie back to something we sent.
    if not (threaded or subject_match or from_email in sent_emails):
        return None

    lead_email = from_email if from_email in sent_emails else ""
    if not lead_email:
        # Replied from a different address (alias, assistant, shared mailbox).
        # Keep it — it is still a response — attributed to the sending address.
        lead_email = from_email

    return ReplyRecord(
        lead_email=lead_email,
        category=AUTO_REPLY if is_auto else HUMAN,
        received_at=_received_at(msg),
        subject=subject[:200],
        snippet=_quoted_stripped(body)[:600],
        from_email=from_email,
    )


def _maybe_related(
    head: Message,
    our_message_ids: set[str],
    sent_emails: set[str],
    sent_subjects: set[str],
    our_address: str,
) -> bool:
    """Cheap header-only pre-filter for `scan_mailbox` phase 1.

    Deliberately permissive — `classify_message` makes the real decision once it
    has the body. It only has to be right about what to *discard*.
    """
    from_email = parseaddr(_decode(head.get("From")))[1].lower()
    if not from_email or from_email == our_address.lower():
        return False
    if _BOUNCE_SENDER.search(from_email):
        return True  # bounce reports name their victim in the body, not the headers
    if from_email in sent_emails:
        return True
    if (head.get("In-Reply-To") or "").strip() in our_message_ids:
        return True
    if any(r in our_message_ids for r in (head.get("References") or "").split()):
        return True
    return normalize_subject(_decode(head.get("Subject"))) in sent_subjects


def _received_at(msg: Message) -> datetime:
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        if dt:
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    return datetime.now(timezone.utc)


# ---------------- IMAP ----------------

def scan_mailbox(
    *,
    since: date,
    our_message_ids: set[str],
    sent_emails: set[str],
    sent_subjects: set[str],
    address: str | None = None,
    password: str | None = None,
    batch: int = 100,
) -> ScanResult:
    """Read the mailbox (read-only) and return every message that answers our
    outreach. Never mutates the mailbox."""
    address = address or os.environ["GMAIL_ADDRESS"]
    password = (password or os.environ["GMAIL_APP_PASSWORD"]).replace(" ", "")

    result = ScanResult()
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(address, password)
        conn.select(MAILBOX, readonly=True)
        typ, data = conn.search(None, f'(SINCE {since.strftime("%d-%b-%Y")})')
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        ids = data[0].split()
        result.scanned = len(ids)
        log.info("Scanning %d messages since %s", len(ids), since)

        # Phase 1 — headers only. A mailbox is mostly newsletters and LinkedIn
        # noise; pulling every full body would make this a multi-minute job, so
        # we cheaply narrow to messages that could plausibly answer our outreach.
        candidates: list[bytes] = []
        header_fields = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT IN-REPLY-TO REFERENCES CONTENT-TYPE X-FAILED-RECIPIENTS)])"
        for start in range(0, len(ids), batch):
            window = ids[start:start + batch]
            typ, chunk = conn.fetch(b",".join(window).decode(), header_fields)
            if typ != "OK":
                continue
            fetched = [item for item in chunk if isinstance(item, tuple)]
            for msg_id, item in zip(window, fetched):
                try:
                    head = email.message_from_bytes(item[1])
                except Exception:
                    continue
                if _maybe_related(head, our_message_ids, sent_emails, sent_subjects, address):
                    candidates.append(msg_id)
        log.info("%d of %d messages look related; fetching those in full", len(candidates), len(ids))

        # Phase 2 — full bodies for the shortlist, where the real classification
        # happens (bounce reports only name the failed recipient in the body).
        for start in range(0, len(candidates), batch):
            seq = b",".join(candidates[start:start + batch]).decode()
            typ, chunk = conn.fetch(seq, "(BODY.PEEK[])")
            if typ != "OK":
                continue
            for item in chunk:
                if not isinstance(item, tuple):
                    continue
                try:
                    msg = email.message_from_bytes(item[1])
                    record = classify_message(
                        msg,
                        our_message_ids=our_message_ids,
                        sent_emails=sent_emails,
                        sent_subjects=sent_subjects,
                        our_address=address,
                    )
                except Exception as exc:  # one malformed message can't stop the scan
                    log.warning("Skipping unparseable message: %s", exc)
                    continue
                if record:
                    result.records.append(record)
                else:
                    result.unmatched += 1
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return result


# ---------------- sentiment ----------------

CLASSIFY_SYSTEM = """You label replies to a cold outreach email from CUBE Consulting,
a student consulting group at the University of Illinois asking companies to sponsor
a semester-long student consulting project.

Return JSON: {"classification": "...", "reason": "short phrase"}

classification is exactly one of:
  positive     — interested, wants a call/more info, asks a qualifying question,
                 or refers us to a specific colleague who can say yes
  neutral      — acknowledges without commitment ("will keep you in mind",
                 "circle back next year", forwarded internally with no answer)
  negative     — declines, not a fit, wrong person with no referral, or asks us to stop
  unsubscribe  — explicitly demands removal from the list
  ooo          — an out-of-office / automated absence notice
Judge only what the sender wrote, not politeness."""


def classify_sentiment(records: list[ReplyRecord]) -> None:
    """Fill `classification` on human replies via Gemini. Best-effort: any
    failure leaves the record unclassified rather than failing the sync."""
    from .llm import generate_json

    if not os.environ.get("GEMINI_API_KEY"):
        log.info("GEMINI_API_KEY unset — skipping reply sentiment classification")
        return

    targets = [r for r in records if r.category == HUMAN and r.snippet]
    valid = {c.value for c in ReplyClass}
    for record in targets:
        try:
            out = generate_json(
                model=CLASSIFY_MODEL,
                system=CLASSIFY_SYSTEM,
                prompt=f"Subject: {record.subject}\n\nReply:\n{record.snippet}",
                max_tokens=200,
            )
        except Exception as exc:
            log.warning("Classification failed for %s: %s", record.lead_email, exc)
            continue
        label = str(out.get("classification", "")).strip().lower()
        if label in valid:
            record.classification = label
            record.classification_reason = str(out.get("reason", ""))[:200]
    log.info("Classified %d human replies", sum(1 for r in targets if r.classification))


# ---------------- orchestration ----------------

def dedupe(records: list[ReplyRecord]) -> list[ReplyRecord]:
    """One row per person: a human reply outranks an auto-reply, which outranks a
    bounce. Keeps the earliest timestamp for the winning category."""
    best: dict[str, ReplyRecord] = {}
    for record in sorted(records, key=lambda r: r.received_at):
        current = best.get(record.lead_email)
        if current is None or CATEGORY_RANK[record.category] > CATEGORY_RANK[current.category]:
            best[record.lead_email] = record
    return sorted(best.values(), key=lambda r: r.received_at)


def sync_replies(sheets, *, since: date | None = None, classify: bool = True,
                 dry_run: bool = False) -> dict:
    """Scan the mailbox, then persist what came back to the Sheet.

    Writes the `Replies` tab (rebuilt each run — it's derived data) and updates
    each matched lead's `status` / `replied_at` on the `Leads` tab.
    """
    drafts = sheets.book.worksheet("Drafts").get_all_records()
    leads = sheets.book.worksheet("Leads").get_all_records()

    our_message_ids = {
        str(d.get("message_id", "")).strip()
        for d in drafts if str(d.get("message_id", "")).strip()
    }
    sent_emails = {
        str(d.get("lead_email", "")).strip().lower()
        for d in drafts if str(d.get("sent_at", "")).strip() and d.get("lead_email")
    }
    sent_emails.update(
        str(l.get("email", "")).strip().lower()
        for l in leads if str(l.get("sent_at", "")).strip() and l.get("email")
    )
    sent_emails.discard("")
    sent_subjects = {
        normalize_subject(str(d.get("subject", "")))
        for d in drafts if str(d.get("sent_at", "")).strip() and d.get("subject")
    }
    sent_subjects.discard("")

    if since is None:
        sent_dates = []
        for d in drafts:
            raw = str(d.get("sent_at", "")).strip()
            if raw:
                try:
                    sent_dates.append(datetime.fromisoformat(raw.replace("Z", "+00:00")).date())
                except ValueError:
                    pass
        # A day of slack so a reply that beat the clock isn't missed.
        since = (min(sent_dates) - timedelta(days=1)) if sent_dates else (
            datetime.now(timezone.utc).date() - timedelta(days=90)
        )

    scan = scan_mailbox(
        since=since,
        our_message_ids=our_message_ids,
        sent_emails=sent_emails,
        sent_subjects=sent_subjects,
    )
    records = dedupe(scan.records)
    log.info(
        "Matched %d responses from %d scanned messages (%d human, %d auto, %d bounced)",
        len(records), scan.scanned,
        sum(1 for r in records if r.category == HUMAN),
        sum(1 for r in records if r.category == AUTO_REPLY),
        sum(1 for r in records if r.category == BOUNCE),
    )

    if classify:
        classify_sentiment(records)

    if dry_run:
        for r in records:
            print(f"{r.category:11s} {r.classification or '-':11s} {r.lead_email:45s} {r.subject[:60]}")
        return _summary(records, scan)

    sheets.replace_replies(records)

    # Mirror onto Leads so the status column tells the truth at a glance.
    updates: dict[str, dict] = {}
    for r in records:
        if r.category == BOUNCE:
            updates[r.lead_email] = {"status": LeadStatus.BOUNCED.value}
        elif r.category == HUMAN:
            updates[r.lead_email] = {
                "status": LeadStatus.HOT.value
                if r.classification == ReplyClass.POSITIVE.value
                else LeadStatus.REPLIED.value,
                "replied_at": r.received_at.isoformat(timespec="seconds"),
            }
        else:  # auto-reply: the mail landed, but they haven't answered yet
            continue
    changed = sheets.bulk_update_leads(updates)
    log.info("Updated %d lead rows from inbox scan", changed)
    return _summary(records, scan)


def _summary(records: list[ReplyRecord], scan: ScanResult) -> dict:
    from collections import Counter

    return {
        "scanned": scan.scanned,
        "matched": len(records),
        "human": sum(1 for r in records if r.category == HUMAN),
        "auto_reply": sum(1 for r in records if r.category == AUTO_REPLY),
        "bounce": sum(1 for r in records if r.category == BOUNCE),
        "by_classification": dict(
            Counter(r.classification for r in records if r.classification)
        ),
    }

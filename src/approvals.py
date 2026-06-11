"""Email-reply based approval.

The `prepare` job emails APPROVER_EMAIL a numbered list of every draft (full
subject + body) and records the Gmail thread it went out on (see
SheetClient.record_digest). The approver just replies in that thread:

    "approve all"            -> send everything
    "approve 1, 3, 5"        -> send those
    "1-4"                    -> send 1,2,3,4
    "skip 2" / "all but 2"  -> send everything except 2
    "none" / "no"           -> send nothing today

Before sending, the `send` job reads that reply straight from Gmail, parses
which numbers were approved, and flips those Drafts rows to approved=TRUE in the
same Sheet. No spreadsheet editing, no file uploads — the whole loop runs off
the inbox the digest already landed in.
"""
from __future__ import annotations

import json
import logging
import os
import re

from .gmail_send import _service
from .llm import generate_json
from .reply_check import _extract_body
from .sheets import SheetClient

log = logging.getLogger(__name__)

PARSE_MODEL = "gemini-2.5-flash-lite"

PARSE_SYSTEM = """You parse a human's reply approving cold-email drafts for sending.
You are given a numbered list of drafts and the reply text. Return STRICT JSON:
{"approved": [<draft numbers to SEND>]}

Rules:
  - "approve all" / "all" / "send all" / "yes" / "looks good" / "lgtm" => every number
  - "none" / "skip all" / "no" / "don't send" => []
  - "approve 1, 3" / "1 and 3" / "send 2,4" => exactly those numbers
  - ranges like "1-3" mean 1,2,3
  - "skip 2" / "all except 2" / "all but 2,4" => every number EXCEPT those
  - if a draft is named by email address or company, map it to its number
  - only include numbers that appear in the provided list
Return JSON only, no prose."""


def _approver_email() -> str:
    return (
        os.environ.get("APPROVER_EMAIL")
        or os.environ.get("DIGEST_RECIPIENT")
        or os.environ["IMPERSONATE_EMAIL"]
    )


def _fetch_approver_reply(svc, thread_id: str, approver_email: str) -> str | None:
    """Return the latest message body in the thread sent BY the approver."""
    thread = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    for msg in reversed(thread.get("messages", [])):
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        if approver_email.lower() in headers.get("from", "").lower():
            return _extract_body(msg)
    return None


def _strip_quoted(text: str) -> str:
    """Drop the quoted original so we only parse what the approver actually typed."""
    out = []
    for line in text.splitlines():
        if re.match(r"\s*on .+wrote:\s*$", line, re.IGNORECASE):
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip() or text.strip()


def parse_approval(reply_text: str, items: list[dict]) -> set[int]:
    """Map the approver's reply to the set of draft numbers to send."""
    valid = {int(it["n"]) for it in items}
    text = _strip_quoted(reply_text)

    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        try:
            listing = "\n".join(
                f'{it["n"]}. {it["lead_email"]} — {it["subject"]}' for it in items
            )
            data = generate_json(
                model=PARSE_MODEL,
                system=PARSE_SYSTEM,
                prompt=f"Drafts:\n{listing}\n\nReply:\n{text[:2000]}",
                max_tokens=300,
            )
            approved = {int(n) for n in data.get("approved", [])}
            return approved & valid
        except Exception as exc:
            log.warning("Gemini approval parse failed (%s); falling back to regex", exc)

    return _regex_parse(text, valid)


def _regex_parse(text: str, valid: set[int]) -> set[int]:
    t = text.lower()
    if re.search(r"\b(approve all|send all|all of them|everything|lgtm|looks good)\b", t) or t.strip() in ("all", "yes"):
        return set(valid)
    if re.search(r"\b(skip all|none|don'?t send)\b", t) or t.strip() in ("no", "none"):
        return set()

    found: set[int] = set()
    for m in re.finditer(r"(\d+)\s*-\s*(\d+)", t):
        found.update(range(int(m.group(1)), int(m.group(2)) + 1))
    for m in re.finditer(r"\d+", re.sub(r"\d+\s*-\s*\d+", "", t)):
        found.add(int(m.group()))
    found &= valid

    if re.search(r"\b(skip|except|but|all but)\b", t):
        return valid - found
    return found


def apply_email_approvals(dry_run: bool = False) -> int:
    """Read the approver's reply for the latest digest and approve those drafts.

    Returns the number of drafts approved this run. Safe to call when no reply
    has arrived yet (returns 0 and leaves the digest unprocessed for next run).
    """
    sheets = SheetClient()
    pending = sheets.latest_unprocessed_digest()
    if not pending:
        log.info("No pending approval digest to process")
        return 0

    row_idx, record = pending
    thread_id = str(record.get("thread_id") or "").strip()
    items = json.loads(record.get("items_json") or "[]")
    if not thread_id or not items:
        log.info("Digest row %d has no thread/items; skipping", row_idx)
        return 0

    approver = _approver_email()
    svc = _service()
    reply = _fetch_approver_reply(svc, thread_id, approver)
    if not reply:
        log.info("No reply from %s in thread %s yet — nothing approved this run", approver, thread_id)
        return 0

    approved_nums = parse_approval(reply, items)
    by_n = {int(it["n"]): it for it in items}
    rows_to_approve = [int(by_n[n]["drafts_row"]) for n in approved_nums if n in by_n]
    log.info(
        "Approver %s approved %d of %d drafts: %s",
        approver, len(rows_to_approve), len(items), sorted(approved_nums),
    )

    if dry_run:
        log.info("[DRY RUN] would approve Drafts rows %s and mark digest processed", rows_to_approve)
        return len(rows_to_approve)

    if rows_to_approve:
        sheets.approve_draft_rows(rows_to_approve)
    sheets.mark_digest_processed(row_idx)
    return len(rows_to_approve)

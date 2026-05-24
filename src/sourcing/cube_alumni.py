"""CUBE Alumni Sheet loader.

The CUBE exec folder has a Google Sheet with past member contact info.
We pull rows where status is open (i.e. graduated, working, has email)
and convert them to Lead records. UIUC alum is always True here.

Expected columns (case-insensitive, robust to ordering):
  Name, Email, Company, Title, LinkedIn, Industry, Location, Graduation Year
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Iterable

import gspread
from google.oauth2.service_account import Credentials

from ..models import Lead

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _client(service_account_info: dict) -> gspread.Client:
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    return gspread.authorize(creds)


def fetch_alumni_leads(
    service_account_info: dict,
    sheet_id: str | None = None,
    worksheet_name: str = "Alumni",
) -> Iterable[Lead]:
    sheet_id = sheet_id or os.environ.get("ALUMNI_SHEET_ID")
    if not sheet_id:
        log.info("ALUMNI_SHEET_ID not set; skipping CUBE alumni source")
        return []

    gc = _client(service_account_info)
    ws = gc.open_by_key(sheet_id).worksheet(worksheet_name)
    records = ws.get_all_records()

    now = datetime.now(timezone.utc)
    for row in records:
        keyed = {k.lower().strip(): (v or "") for k, v in row.items()}
        email = str(keyed.get("email", "")).strip()
        name = str(keyed.get("name", "")).strip()
        if not email or not name or "@" not in email:
            continue
        yield Lead(
            name=name,
            title=str(keyed.get("title", "")).strip() or None,
            company=str(keyed.get("company", "")).strip() or "",
            email=email,
            linkedin=str(keyed.get("linkedin", "")).strip() or None,
            industry=str(keyed.get("industry", "")).strip() or None,
            location=str(keyed.get("location", "")).strip() or None,
            is_uiuc_alum=True,
            schools=["University of Illinois Urbana-Champaign"],
            source="cube_alumni",
            date_added=now,
        )

"""Pydantic models for leads, drafts, replies, and config records."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class LeadStatus(str, Enum):
    NEW = "new"
    DRAFTED = "drafted"
    SENT = "sent"
    REPLIED = "replied"
    FOLLOWED_UP = "followed-up"
    HOT = "hot"
    CLOSED = "closed"
    SUPPRESSED = "suppressed"


class TemplateType(str, Enum):
    BUSINESS = "business"
    HYBRID_PRODUCT = "hybrid_product"
    TECHNICAL_SOFTWARE = "technical_software"
    TECHNICAL_ENGINEERING = "technical_engineering"


class ReplyClass(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    OUT_OF_OFFICE = "ooo"
    UNSUBSCRIBE = "unsubscribe"


class Lead(BaseModel):
    name: str
    title: Optional[str] = None
    company: str
    email: EmailStr
    linkedin: Optional[str] = None
    industry: Optional[str] = None
    location: Optional[str] = None
    company_stage: Optional[str] = None
    is_uiuc_alum: bool = False
    schools: list[str] = Field(default_factory=list)
    source: str = "unknown"
    date_added: Optional[datetime] = None
    score: float = 0.0
    status: LeadStatus = LeadStatus.NEW
    sent_at: Optional[datetime] = None
    replied_at: Optional[datetime] = None
    last_follow_up_at: Optional[datetime] = None
    thread_id: Optional[str] = None
    message_id: Optional[str] = None

    def first_name(self) -> str:
        return self.name.split()[0] if self.name else ""


class PastProject(BaseModel):
    semester: str
    client: str
    keywords: list[str]
    deliverables: str


class Draft(BaseModel):
    lead_email: EmailStr
    prepared_at: datetime
    template_used: TemplateType
    subject: str
    body: str
    approved: bool = False
    sent_at: Optional[datetime] = None
    send_error: Optional[str] = None
    message_id: Optional[str] = None
    is_follow_up: bool = False
    in_reply_to: Optional[str] = None  # Message-ID of the original send (for threading)


class Reply(BaseModel):
    thread_id: str
    from_email: str
    received_at: datetime
    snippet: str
    classification: ReplyClass
    classification_reason: str = ""

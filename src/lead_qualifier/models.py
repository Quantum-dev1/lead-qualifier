"""Data models shared by the API, the qualifier and the outputs."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Tier(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    SPAM = "spam"


class LeadIn(BaseModel):
    """An inbound lead as received from a web form, an email parser or a chat widget.

    Every text field is *untrusted*: it is written by whoever filled in the form.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    source: Literal["form", "email", "chat", "other"] = "form"
    external_id: str | None = Field(default=None, max_length=200)
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    company: str | None = Field(default=None, max_length=200)
    website: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=50)
    subject: str | None = Field(default=None, max_length=500)
    message: str = Field(min_length=1, max_length=20_000)
    # Optional structured form fields. Free text, parsed like the message.
    budget: str | None = Field(default=None, max_length=200)
    timeline: str | None = Field(default=None, max_length=200)
    company_size: str | None = Field(default=None, max_length=200)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not EMAIL_RE.match(value):
            raise ValueError("not a valid email address")
        return value.lower()

    def full_text(self) -> str:
        """All free-text content of the lead, used for rule matching."""
        parts = [self.subject, self.message, self.budget, self.timeline, self.company_size]
        return "\n".join(p for p in parts if p)


class ExtractedFields(BaseModel):
    company: str | None = None
    contact_name: str | None = None
    budget: str | None = None
    budget_usd: float | None = None
    timeline: str | None = None
    timeline_months: float | None = None
    company_size: int | None = None
    industry: str | None = None
    need: str | None = None


class RuleHit(BaseModel):
    """One line of the deterministic score breakdown."""

    criterion: str
    points: float
    detail: str


class GuardReport(BaseModel):
    spam_score: float = 0.0
    is_spam: bool = False
    injection_detected: bool = False
    signals: list[str] = Field(default_factory=list)


class LLMAssessment(BaseModel):
    """What the language model returned, after validation and clamping."""

    fit_score: float = Field(ge=0, le=100)
    tier: Tier
    is_spam: bool = False
    extracted: ExtractedFields = Field(default_factory=ExtractedFields)
    reasoning: str = ""
    red_flags: list[str] = Field(default_factory=list)
    reply_draft: str = ""


class Qualification(BaseModel):
    lead_id: str | None = None
    score: float
    tier: Tier
    rule_score: float
    llm_score: float | None = None
    llm_used: bool = False
    needs_review: bool = False
    extracted: ExtractedFields
    reasons: list[str]
    red_flags: list[str]
    breakdown: list[RuleHit]
    guard: GuardReport
    reply_draft: str
    model: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OutputResult(BaseModel):
    output: str
    ok: bool
    detail: str = ""
    external_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

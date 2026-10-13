"""Ideal Customer Profile (ICP) and scoring rules, loaded from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from .models import Tier


class Band(BaseModel):
    """`score` is a fraction (0..1) of the criterion weight."""

    min: float | None = None
    max: float | None = None
    score: float = Field(ge=0, le=1)


class BudgetRule(BaseModel):
    weight: float = 30
    currency: str = "USD"
    # Approximate conversion rates into `currency`. Edit to taste.
    fx: dict[str, float] = Field(default_factory=lambda: {"USD": 1.0})
    bands: list[Band] = Field(default_factory=list)
    unknown_score: float = 0.4


class TimelineRule(BaseModel):
    weight: float = 15
    bands: list[Band] = Field(default_factory=list)  # matched on `max` months
    unknown_score: float = 0.4
    exploring_score: float = 0.15


class CompanySizeRule(BaseModel):
    weight: float = 15
    bands: list[Band] = Field(default_factory=list)  # matched on `min` employees
    unknown_score: float = 0.4


class IndustryRule(BaseModel):
    weight: float = 20
    # industry name -> keywords that indicate it
    target: dict[str, list[str]] = Field(default_factory=dict)
    unknown_score: float = 0.35


class NeedRule(BaseModel):
    weight: float = 20
    # service name -> keywords that indicate the lead needs it
    services: dict[str, list[str]] = Field(default_factory=dict)
    full_match_count: int = 2


class RedFlag(BaseModel):
    id: str
    description: str
    patterns: list[str]
    penalty: float = 20
    # When true, a match makes the lead "cold" no matter what else it says.
    disqualify: bool = False


class SpamRules(BaseModel):
    patterns: list[str] = Field(default_factory=list)
    pattern_weight: float = 0.35
    max_links: int = 3
    link_weight: float = 0.3
    threshold: float = 0.6
    disposable_domains: list[str] = Field(default_factory=list)


class Tiers(BaseModel):
    hot: float = 70
    warm: float = 45

    def for_score(self, score: float) -> Tier:
        if score >= self.hot:
            return Tier.HOT
        if score >= self.warm:
            return Tier.WARM
        return Tier.COLD


class Weights(BaseModel):
    rules: float = 0.6
    llm: float = 0.4

    @model_validator(mode="after")
    def _normalized(self) -> Weights:
        total = self.rules + self.llm
        if total <= 0:
            raise ValueError("weights.rules + weights.llm must be > 0")
        self.rules, self.llm = self.rules / total, self.llm / total
        return self


class CompanyProfile(BaseModel):
    name: str = "Our team"
    offer: str = "software development"
    signature: str = "— The team"


class ICP(BaseModel):
    company: CompanyProfile = Field(default_factory=CompanyProfile)
    description: str = ""
    weights: Weights = Field(default_factory=Weights)
    budget: BudgetRule = Field(default_factory=BudgetRule)
    timeline: TimelineRule = Field(default_factory=TimelineRule)
    company_size: CompanySizeRule = Field(default_factory=CompanySizeRule)
    industry: IndustryRule = Field(default_factory=IndustryRule)
    need: NeedRule = Field(default_factory=NeedRule)
    corporate_email_bonus: float = 5
    free_email_domains: list[str] = Field(default_factory=list)
    red_flags: list[RedFlag] = Field(default_factory=list)
    spam: SpamRules = Field(default_factory=SpamRules)
    tiers: Tiers = Field(default_factory=Tiers)
    # tier -> list of output names (slack, webhook, hubspot, pipedrive, ...)
    routing: dict[Tier, list[str]] = Field(default_factory=dict)
    # Fallback replies when no LLM is available. `{name}` and `{company}` are filled in.
    reply_templates: dict[Tier, str] = Field(default_factory=dict)
    # Extra CRM settings, passed through to the adapters.
    crm: dict[str, Any] = Field(default_factory=dict)

    @property
    def max_rule_points(self) -> float:
        return (
            self.budget.weight
            + self.timeline.weight
            + self.company_size.weight
            + self.industry.weight
            + self.need.weight
        )


def load_icp(path: str | Path) -> ICP:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    return ICP.model_validate(data)

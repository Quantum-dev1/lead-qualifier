"""Deterministic, explainable rule scoring against the ICP."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .extract import (
    best_category,
    email_domain,
    matched_keywords,
    parse_budget,
    parse_company_size,
    parse_timeline,
)
from .icp import ICP, Band
from .models import ExtractedFields, LeadIn, RuleHit


@dataclass
class RuleResult:
    score: float
    breakdown: list[RuleHit]
    extracted: ExtractedFields
    red_flags: list[str] = field(default_factory=list)
    disqualified: bool = False
    exploring: bool = False


def _band_by_min(bands: list[Band], value: float) -> float:
    for band in sorted(bands, key=lambda b: b.min or 0, reverse=True):
        if value >= (band.min or 0):
            return band.score
    return 0.0


def _band_by_max(bands: list[Band], value: float) -> float:
    for band in sorted(bands, key=lambda b: b.max if b.max is not None else float("inf")):
        if band.max is None or value <= band.max:
            return band.score
    return 0.0


def score_lead(lead: LeadIn, icp: ICP, today: date | None = None) -> RuleResult:
    text = lead.full_text()
    context = "\n".join(filter(None, [text, lead.company, lead.website]))
    hits: list[RuleHit] = []
    extracted = ExtractedFields(company=lead.company, contact_name=lead.name)

    # Budget: a dedicated form field wins over the free text.
    amount, matched = (None, None)
    if lead.budget:
        amount, matched = parse_budget(lead.budget, fx=icp.budget.fx, assume_money=True)
    if amount is None:
        amount, matched = parse_budget(lead.message, fx=icp.budget.fx)
    extracted.budget, extracted.budget_usd = matched, amount
    if amount is not None:
        fraction = _band_by_min(icp.budget.bands, amount)
        hits.append(
            RuleHit(
                criterion="budget",
                points=fraction * icp.budget.weight,
                detail=f"{matched} ≈ {amount:,.0f} {icp.budget.currency}",
            )
        )
    else:
        hits.append(
            RuleHit(
                criterion="budget", points=icp.budget.unknown_score * icp.budget.weight, detail="not stated"
            )
        )

    # Timeline.
    months, timeline_text, exploring = parse_timeline(" ".join(filter(None, [lead.timeline, text])), today)
    extracted.timeline, extracted.timeline_months = timeline_text, months
    if exploring:
        hits.append(
            RuleHit(
                criterion="timeline",
                points=icp.timeline.exploring_score * icp.timeline.weight,
                detail=f"exploring ('{timeline_text}')",
            )
        )
    elif months is not None:
        fraction = _band_by_max(icp.timeline.bands, months)
        hits.append(
            RuleHit(
                criterion="timeline",
                points=fraction * icp.timeline.weight,
                detail=f"'{timeline_text}' ≈ {months:g} months",
            )
        )
    else:
        hits.append(
            RuleHit(
                criterion="timeline",
                points=icp.timeline.unknown_score * icp.timeline.weight,
                detail="not stated",
            )
        )

    # Company size.
    size, size_text = parse_company_size(" ".join(filter(None, [lead.company_size, text])))
    extracted.company_size = size
    if size is not None:
        fraction = _band_by_min(icp.company_size.bands, size)
        hits.append(
            RuleHit(
                criterion="company_size",
                points=fraction * icp.company_size.weight,
                detail=f"'{size_text}' → {size} people",
            )
        )
    else:
        hits.append(
            RuleHit(
                criterion="company_size",
                points=icp.company_size.unknown_score * icp.company_size.weight,
                detail="not stated",
            )
        )

    # Industry.
    industry, industry_hits = best_category(context, icp.industry.target)
    extracted.industry = industry
    if industry:
        hits.append(
            RuleHit(
                criterion="industry",
                points=icp.industry.weight,
                detail=f"{industry} ({', '.join(industry_hits[:3])})",
            )
        )
    else:
        hits.append(
            RuleHit(
                criterion="industry",
                points=icp.industry.unknown_score * icp.industry.weight,
                detail="no target industry recognized",
            )
        )

    # Need: does the lead ask for something we sell?
    service_hits: dict[str, list[str]] = {
        name: found
        for name, keywords in icp.need.services.items()
        if (found := matched_keywords(text, keywords))
    }
    total_hits = sum(len(v) for v in service_hits.values())
    if service_hits:
        extracted.need = ", ".join(service_hits)
    fraction = min(1.0, total_hits / max(1, icp.need.full_match_count))
    hits.append(
        RuleHit(
            criterion="need",
            points=fraction * icp.need.weight,
            detail=", ".join(f"{k} ({', '.join(v[:2])})" for k, v in service_hits.items())
            or "no matching service",
        )
    )

    raw = sum(h.points for h in hits)
    score = 100 * raw / icp.max_rule_points if icp.max_rule_points else 0.0

    domain = email_domain(lead.email)
    if domain and domain not in {d.lower() for d in icp.free_email_domains}:
        score += icp.corporate_email_bonus
        hits.append(
            RuleHit(
                criterion="contact",
                points=icp.corporate_email_bonus,
                detail=f"corporate email domain {domain}",
            )
        )

    red_flags: list[str] = []
    disqualified = False
    for flag in icp.red_flags:
        found = matched_keywords(context, flag.patterns)
        if found:
            red_flags.append(f"{flag.id}: {flag.description} ('{found[0]}')")
            score -= flag.penalty
            hits.append(
                RuleHit(criterion=f"red_flag:{flag.id}", points=-flag.penalty, detail=f"matched '{found[0]}'")
            )
            disqualified = disqualified or flag.disqualify

    return RuleResult(
        score=round(max(0.0, min(100.0, score)), 1),
        breakdown=hits,
        extracted=extracted,
        red_flags=red_flags,
        disqualified=disqualified,
        exploring=exploring,
    )

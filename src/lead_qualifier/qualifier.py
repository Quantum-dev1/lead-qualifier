"""The qualification pipeline: guard → rules → LLM → decision."""

from __future__ import annotations

import logging
from datetime import date

from .guard import inspect
from .icp import ICP
from .llm import LLMClient, LLMError, assess
from .models import ExtractedFields, LeadIn, LLMAssessment, Qualification, Tier
from .rules import score_lead

log = logging.getLogger(__name__)

TIER_ORDER = [Tier.SPAM, Tier.COLD, Tier.WARM, Tier.HOT]
DISAGREEMENT_THRESHOLD = 40


def _cap(tier: Tier, ceiling: Tier) -> Tier:
    return tier if TIER_ORDER.index(tier) <= TIER_ORDER.index(ceiling) else ceiling


def _merge_extracted(rules: ExtractedFields, llm: ExtractedFields | None) -> ExtractedFields:
    """Deterministic values (and form fields) win; the model only fills gaps."""
    if llm is None:
        return rules
    merged = rules.model_copy()
    for name in ExtractedFields.model_fields:
        if getattr(merged, name) in (None, "") and getattr(llm, name) not in (None, ""):
            setattr(merged, name, getattr(llm, name))
    return merged


def _template_reply(icp: ICP, lead: LeadIn, tier: Tier) -> str:
    template = icp.reply_templates.get(tier, "")
    if not template:
        return ""
    first_name = (lead.name or "there").split()[0]
    return template.format(name=first_name, company=icp.company.name, signature=icp.company.signature).strip()


class Qualifier:
    def __init__(self, icp: ICP, llm: LLMClient | None = None, *, today: date | None = None) -> None:
        self.icp = icp
        self.llm = llm
        self.today = today

    async def qualify(self, lead: LeadIn) -> Qualification:
        icp = self.icp
        guard = inspect(lead, icp)
        rules = score_lead(lead, icp, self.today)
        reasons: list[str] = []
        red_flags = list(rules.red_flags)
        needs_review = False

        # 1. Obvious spam: decide without spending tokens.
        if guard.is_spam:
            reasons.append(f"Spam guard triggered (score {guard.spam_score:.2f}): {'; '.join(guard.signals)}")
            return Qualification(
                score=min(rules.score, 10.0),
                tier=Tier.SPAM,
                rule_score=rules.score,
                extracted=rules.extracted,
                reasons=reasons,
                red_flags=red_flags,
                breakdown=rules.breakdown,
                guard=guard,
                reply_draft="",
            )

        # 2. LLM assessment (optional).
        assessment: LLMAssessment | None = None
        model_saw_injection = False
        if self.llm is not None:
            try:
                assessment, model_saw_injection = await assess(lead, icp, self.llm)
            except LLMError as exc:
                log.warning("LLM assessment failed, falling back to rules: %s", exc)
                reasons.append(f"LLM unavailable, rules-only decision ({exc})")

        injection = guard.injection_detected or model_saw_injection
        if injection:
            needs_review = True
            reasons.append(
                "Possible prompt injection in lead text"
                + (
                    f" ({'; '.join(s for s in guard.signals if 'injection' in s)})"
                    if guard.injection_detected
                    else " (flagged by model)"
                )
                + ": model output ignored for scoring, tier capped at cold, human review required."
            )

        # 3. Combine.
        if assessment is not None and not injection:
            score = icp.weights.rules * rules.score + icp.weights.llm * assessment.fit_score
            reasons.append(
                f"Rules {rules.score:.0f}/100, model {assessment.fit_score:.0f}/100 → {score:.0f}."
            )
            if assessment.reasoning:
                reasons.append(f"Model: {assessment.reasoning}")
            red_flags.extend(f"model: {flag}" for flag in assessment.red_flags)
            if abs(assessment.fit_score - rules.score) >= DISAGREEMENT_THRESHOLD:
                needs_review = True
                reasons.append("Rules and model disagree strongly: human review suggested.")
        else:
            score = rules.score
            reasons.append(f"Rules score {rules.score:.0f}/100.")

        tier = icp.tiers.for_score(score)

        if assessment is not None and not injection and (assessment.is_spam or assessment.tier is Tier.SPAM):
            if score < icp.tiers.warm:
                tier = Tier.SPAM
                reasons.append("Model classified the lead as spam.")
            else:
                needs_review = True
                reasons.append("Model suspects spam but rules score is high: human review suggested.")

        if rules.disqualified:
            tier = _cap(tier, Tier.COLD)
            reasons.append("Disqualifying red flag present: tier capped at cold.")
        if rules.exploring:
            tier = _cap(tier, Tier.WARM)
            reasons.append("Lead is only exploring: tier capped at warm.")
        if injection:
            tier = _cap(tier, Tier.COLD)

        reply = ""
        if tier is not Tier.SPAM:
            if assessment is not None and not injection and assessment.reply_draft.strip():
                reply = assessment.reply_draft.strip()
            else:
                reply = _template_reply(icp, lead, tier)

        return Qualification(
            score=round(score, 1),
            tier=tier,
            rule_score=rules.score,
            llm_score=assessment.fit_score if assessment else None,
            llm_used=assessment is not None,
            needs_review=needs_review,
            extracted=_merge_extracted(
                rules.extracted, assessment.extracted if assessment and not injection else None
            ),
            reasons=reasons,
            red_flags=red_flags,
            breakdown=rules.breakdown,
            guard=guard,
            reply_draft=reply,
            model=self.llm.model if (self.llm is not None and assessment is not None) else None,
        )

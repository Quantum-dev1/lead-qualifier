"""Evaluation harness: run the qualifier over labelled leads and report accuracy."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .icp import ICP
from .llm import LLMClient
from .models import LeadIn, Tier
from .qualifier import Qualifier

LABELS = [t.value for t in Tier]


@dataclass
class EvalCase:
    id: str
    lead: LeadIn
    expected: Tier
    note: str = ""


@dataclass
class EvalResult:
    cases: list[tuple[EvalCase, Tier, float]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def correct(self) -> int:
        return sum(1 for case, predicted, _ in self.cases if case.expected is predicted)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    def confusion(self) -> dict[str, dict[str, int]]:
        matrix = {e: dict.fromkeys(LABELS, 0) for e in LABELS}
        for case, predicted, _ in self.cases:
            matrix[case.expected.value][predicted.value] += 1
        return matrix

    def per_class(self) -> dict[str, dict[str, float]]:
        matrix = self.confusion()
        out: dict[str, dict[str, float]] = {}
        for label in LABELS:
            tp = matrix[label][label]
            predicted = sum(matrix[e][label] for e in LABELS)
            actual = sum(matrix[label].values())
            precision = tp / predicted if predicted else 0.0
            recall = tp / actual if actual else 0.0
            f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
            out[label] = {"precision": precision, "recall": recall, "f1": f1, "support": float(actual)}
        return out

    def mistakes(self) -> list[tuple[EvalCase, Tier, float]]:
        return [(c, p, s) for c, p, s in self.cases if c.expected is not p]


def load_cases(path: str | Path) -> list[EvalCase]:
    cases = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        record = json.loads(line)
        cases.append(
            EvalCase(
                id=str(record.get("id", number)),
                lead=LeadIn.model_validate(record["lead"]),
                expected=Tier(record["expected_tier"]),
                note=record.get("note", ""),
            )
        )
    return cases


async def run_eval(
    cases: list[EvalCase], icp: ICP, llm: LLMClient | None, today: date | None = None
) -> EvalResult:
    qualifier = Qualifier(icp, llm, today=today)
    result = EvalResult()
    for case in cases:
        decision = await qualifier.qualify(case.lead)
        result.cases.append((case, decision.tier, decision.score))
    return result


def format_report(result: EvalResult) -> str:
    lines = [
        f"Accuracy: {result.correct}/{result.total} = {result.accuracy:.1%}",
        "",
        "Confusion matrix (rows = expected, columns = predicted)",
    ]
    width = 8
    lines.append("expected".ljust(10) + "".join(label.rjust(width) for label in LABELS))
    for expected, row in result.confusion().items():
        lines.append(expected.ljust(10) + "".join(str(row[label]).rjust(width) for label in LABELS))
    lines += [
        "",
        "class".ljust(10) + "precision".rjust(11) + "recall".rjust(9) + "f1".rjust(7) + "support".rjust(9),
    ]
    for label, m in result.per_class().items():
        lines.append(
            label.ljust(10)
            + f"{m['precision']:.2f}".rjust(11)
            + f"{m['recall']:.2f}".rjust(9)
            + f"{m['f1']:.2f}".rjust(7)
            + f"{int(m['support'])}".rjust(9)
        )
    mistakes = result.mistakes()
    if mistakes:
        lines += ["", "Misclassified:"]
        for case, predicted, score in mistakes:
            lines.append(
                f"  {case.id}: expected {case.expected.value}, got {predicted.value} (score {score:.0f}){' - ' + case.note if case.note else ''}"
            )
    return "\n".join(lines)

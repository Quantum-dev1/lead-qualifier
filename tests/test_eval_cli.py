import asyncio
import json
from pathlib import Path

import pytest

from lead_qualifier.cli import main
from lead_qualifier.eval import format_report, load_cases, run_eval
from lead_qualifier.icp import ICP
from lead_qualifier.models import Tier
from tests.conftest import ROOT, TODAY, ScriptedLLM, llm_reply

DATASET = ROOT / "evals" / "leads.jsonl"


def test_dataset_is_balanced_and_fictional() -> None:
    cases = load_cases(DATASET)
    assert len(cases) == 20
    assert {t: sum(c.expected is t for c in cases) for t in Tier} == dict.fromkeys(Tier, 5)
    for case in cases:
        if case.lead.email and not case.lead.email.endswith(("gmail.com", "outlook.com")):
            domain = case.lead.email.split("@")[1]
            assert domain.endswith(".example") or domain in {"mailinator.com", "yopmail.com"}, domain


def test_rules_only_accuracy_does_not_regress(icp: ICP) -> None:
    result = asyncio.run(run_eval(load_cases(DATASET), icp, None, TODAY))
    assert result.accuracy >= 0.9
    matrix = result.confusion()
    assert sum(sum(row.values()) for row in matrix.values()) == 20
    assert matrix["spam"]["spam"] == 5  # spam must never leak into the pipeline
    assert matrix["hot"]["hot"] == 5


def test_eval_runs_through_the_llm_path(icp: ICP) -> None:
    llm = ScriptedLLM(llm_reply(fit_score=50, tier="warm"))
    result = asyncio.run(run_eval(load_cases(DATASET), icp, llm, TODAY))
    assert result.total == 20
    assert 0 < len(llm.calls) < 20  # spam caught by the guard never reaches the model
    report = format_report(result)
    assert report.startswith("Accuracy:") and "Confusion matrix" in report


def test_cli_eval(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "--icp",
            str(ROOT / "config/icp.yaml"),
            "eval",
            "--dataset",
            str(DATASET),
            "--today",
            "2026-10-14",
            "--min-accuracy",
            "0.9",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "mode: rules only" in out and "Confusion matrix" in out


def test_cli_qualify(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["--icp", str(ROOT / "config/icp.yaml"), "qualify", str(ROOT / "examples/lead.json"), "--no-llm"]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["tier"] == "hot"


def test_cli_sign(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "abc")
    body = tmp_path / "body.json"
    body.write_text("{}")
    assert main(["sign", str(body)]) == 0
    out = capsys.readouterr().out
    assert "X-Lead-Timestamp:" in out and "X-Lead-Signature: sha256=" in out

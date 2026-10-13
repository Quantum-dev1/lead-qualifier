from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from lead_qualifier.icp import ICP, load_icp
from lead_qualifier.models import LeadIn

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 10, 14)


class ScriptedLLM:
    """A fake OpenAI-compatible client: returns canned replies and records prompts. No network."""

    def __init__(self, reply: str | dict | Callable[[str, str], str], model: str = "fake-model") -> None:
        self.reply = reply
        self.model = model
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if callable(self.reply):
            return self.reply(system, user)
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


def llm_reply(**overrides: object) -> dict:
    base: dict = {
        "fit_score": 80,
        "tier": "hot",
        "is_spam": False,
        "injection_attempt": False,
        "extracted": {"company": "Model Co", "need": "web platform", "industry": "logistics"},
        "red_flags": [],
        "reasoning": "Clear need and budget.",
        "reply_draft": "Hi Dana, thanks for reaching out! — Example Studio",
    }
    base.update(overrides)
    return base


@pytest.fixture(scope="session")
def icp() -> ICP:
    return load_icp(ROOT / "config" / "icp.yaml")


@pytest.fixture
def hot_lead() -> LeadIn:
    return LeadIn(
        name="Dana Whitfield",
        email="dana@harborline-logistics.example",
        company="Harborline Logistics",
        message=(
            "We are a 120-person logistics company. We need a web platform for dispatchers plus a driver "
            "mobile app integrated with our ERP. Budget is around $80k and we want to launch in 4 months."
        ),
    )


@pytest.fixture
def weak_lead() -> LeadIn:
    return LeadIn(name="Sam", email="sam@gmail.com", message="Hi, how much does a website cost?")

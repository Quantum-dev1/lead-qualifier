import asyncio
import json

import httpx
import pytest

from lead_qualifier.llm import LLMError, OpenAICompatibleClient, parse_assessment
from lead_qualifier.models import Tier


def make_client(handler) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        "https://llm.example/v1/", "test-model", "sk-test", transport=httpx.MockTransport(handler)
    )


def test_client_sends_openai_compatible_request() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": '{"ok": true}'}}]}
        )

    reply = asyncio.run(make_client(handler).complete("sys", "usr"))
    assert reply == '{"ok": true}'
    assert seen["url"] == "https://llm.example/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in seen["body"]["messages"]] == ["system", "user"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
    ],
)
def test_client_errors_become_llm_errors(response: httpx.Response) -> None:
    with pytest.raises(LLMError):
        asyncio.run(make_client(lambda _: response).complete("s", "u"))


def test_network_errors_become_llm_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMError, match="ConnectError"):
        asyncio.run(make_client(handler).complete("s", "u"))


def test_parse_assessment_handles_code_fences_and_clamps() -> None:
    raw = '```json\n{"fit_score": 140, "tier": "HOT", "extracted": {"budget_usd": 5000, "bogus": 1}, "red_flags": ["x"]}\n```'
    assessment, injection = parse_assessment(raw)
    assert assessment.fit_score == 100
    assert assessment.tier is Tier.HOT
    assert assessment.extracted.budget_usd == 5000
    assert injection is False


@pytest.mark.parametrize(
    "raw",
    [
        "no json here",
        '{"fit_score": "high", "tier": "hot"}',
        '{"fit_score": 10, "tier": "lukewarm"}',
        "[1, 2]",
    ],
)
def test_parse_assessment_rejects_garbage(raw: str) -> None:
    with pytest.raises(LLMError):
        parse_assessment(raw)

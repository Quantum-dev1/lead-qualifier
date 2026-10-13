import asyncio
import json

import httpx
import pytest

from lead_qualifier.icp import ICP
from lead_qualifier.models import LeadIn, OutputResult, Qualification, Tier
from lead_qualifier.outputs import (
    HubSpotOutput,
    PipedriveOutput,
    SlackOutput,
    WebhookOutput,
    route,
    unconfigured_targets,
)
from lead_qualifier.outputs.slack import build_payload
from lead_qualifier.qualifier import Qualifier
from lead_qualifier.signature import SIGNATURE_HEADER, TIMESTAMP_HEADER, verify
from tests.conftest import TODAY


@pytest.fixture
def result(icp: ICP, hot_lead: LeadIn) -> Qualification:
    return asyncio.run(Qualifier(icp, today=TODAY).qualify(hot_lead))


class Recorder:
    """httpx.MockTransport handler that records requests and replies from a route table."""

    def __init__(self, routes: dict[tuple[str, str], list[httpx.Response] | httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        reply = self.routes.get(key)
        if reply is None:
            return httpx.Response(404, json={"error": f"no route for {key}"})
        if isinstance(reply, list):
            return reply.pop(0)
        return reply

    def bodies(self, method: str, path: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.method == method and r.url.path == path]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr("lead_qualifier.outputs.base.asyncio.sleep", instant)


# --- Slack --------------------------------------------------------------------------------------


def test_slack_payload_escapes_untrusted_text(hot_lead: LeadIn, result: Qualification) -> None:
    lead = hot_lead.model_copy(update={"message": "<!channel> click <https://evil.example|here> & win"})
    payload = build_payload(lead, result)
    dumped = json.dumps(payload)
    assert "<!channel>" not in dumped
    assert "&lt;!channel&gt;" in dumped
    assert payload["text"].startswith(":fire: HOT lead")


def test_slack_output_posts_to_webhook(hot_lead: LeadIn, result: Qualification) -> None:
    recorder = Recorder({("POST", "/services/T/B/X"): httpx.Response(200, text="ok")})
    output = SlackOutput(
        "https://hooks.slack.example/services/T/B/X", transport=httpx.MockTransport(recorder)
    )
    sent = asyncio.run(output.send(hot_lead, result))
    assert sent.ok
    assert "blocks" in recorder.bodies("POST", "/services/T/B/X")[0]


def test_slack_output_reports_failures(hot_lead: LeadIn, result: Qualification) -> None:
    recorder = Recorder({("POST", "/hook"): httpx.Response(404, text="no_service")})
    sent = asyncio.run(
        SlackOutput("https://hooks.slack.example/hook", transport=httpx.MockTransport(recorder)).send(
            hot_lead, result
        )
    )
    assert not sent.ok and "404" in sent.detail


# --- Generic webhook ----------------------------------------------------------------------------


def test_webhook_output_signs_payload(hot_lead: LeadIn, result: Qualification) -> None:
    recorder = Recorder({("POST", "/in"): httpx.Response(204)})
    output = WebhookOutput(
        "https://crm.example/in", secret="out-secret", transport=httpx.MockTransport(recorder)
    )
    assert asyncio.run(output.send(hot_lead, result)).ok
    request = recorder.requests[0]
    verify(
        ["out-secret"], request.content, request.headers[TIMESTAMP_HEADER], request.headers[SIGNATURE_HEADER]
    )
    body = json.loads(request.content)
    assert body["event"] == "lead.qualified" and body["qualification"]["tier"] == "hot"


def test_webhook_retries_transient_errors(hot_lead: LeadIn, result: Qualification) -> None:
    recorder = Recorder(
        {
            ("POST", "/in"): [
                httpx.Response(503),
                httpx.Response(429, headers={"retry-after": "1"}),
                httpx.Response(200),
            ]
        }
    )
    output = WebhookOutput("https://crm.example/in", transport=httpx.MockTransport(recorder))
    assert asyncio.run(output.send(hot_lead, result)).ok
    assert len(recorder.requests) == 3


# --- HubSpot ------------------------------------------------------------------------------------

HUBSPOT_UPSERT = ("POST", "/crm/v3/objects/contacts/batch/upsert")
HUBSPOT_NOTES = ("POST", "/crm/v3/objects/notes")


def hubspot(recorder: Recorder, **kwargs) -> HubSpotOutput:
    return HubSpotOutput("pat-test", transport=httpx.MockTransport(recorder), **kwargs)


def test_hubspot_upserts_contact_and_attaches_note(hot_lead: LeadIn, result: Qualification) -> None:
    recorder = Recorder(
        {
            HUBSPOT_UPSERT: httpx.Response(
                200, json={"status": "COMPLETE", "results": [{"id": "5150", "properties": {}, "new": True}]}
            ),
            HUBSPOT_NOTES: httpx.Response(201, json={"id": "9001", "properties": {}}),
        }
    )
    sent = asyncio.run(
        hubspot(recorder, properties_by_tier={"hot": {"lifecyclestage": "salesqualifiedlead"}}).send(
            hot_lead, result
        )
    )
    assert sent.ok and sent.external_id == "5150" and sent.data["note_id"] == "9001"
    assert all(r.headers["authorization"] == "Bearer pat-test" for r in recorder.requests)
    assert str(recorder.requests[0].url).startswith("https://api.hubapi.com/")

    upsert = recorder.bodies(*HUBSPOT_UPSERT)[0]["inputs"][0]
    assert upsert["id"] == "dana@harborline-logistics.example" and upsert["idProperty"] == "email"
    assert upsert["properties"] == {
        "email": "dana@harborline-logistics.example",
        "firstname": "Dana",
        "lastname": "Whitfield",
        "company": "Harborline Logistics",
        "lifecyclestage": "salesqualifiedlead",
    }

    note = recorder.bodies(*HUBSPOT_NOTES)[0]
    assert set(note["properties"]) == {"hs_timestamp", "hs_note_body"}
    assert note["properties"]["hs_timestamp"].endswith("Z")
    assert note["associations"] == [
        {
            "to": {"id": "5150"},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
        }
    ]


def test_hubspot_note_escapes_html(hot_lead: LeadIn, result: Qualification) -> None:
    lead = hot_lead.model_copy(update={"message": "<script>alert(1)</script>"})
    recorder = Recorder(
        {
            HUBSPOT_UPSERT: httpx.Response(200, json={"results": [{"id": "1"}]}),
            HUBSPOT_NOTES: httpx.Response(201, json={"id": "2"}),
        }
    )
    asyncio.run(hubspot(recorder).send(lead, result))
    body = recorder.bodies(*HUBSPOT_NOTES)[0]["properties"]["hs_note_body"]
    assert "<script>" not in body and "&lt;script&gt;" in body


def test_hubspot_skips_leads_without_email(result: Qualification) -> None:
    recorder = Recorder({})
    sent = asyncio.run(hubspot(recorder).send(LeadIn(message="Call me maybe"), result))
    assert not sent.ok and "no email" in sent.detail and recorder.requests == []


def test_hubspot_api_error_is_reported(hot_lead: LeadIn, result: Qualification) -> None:
    recorder = Recorder(
        {HUBSPOT_UPSERT: httpx.Response(401, json={"message": "Authentication credentials not found"})}
    )
    sent = asyncio.run(hubspot(recorder).send(hot_lead, result))
    assert not sent.ok and "401" in sent.detail


# --- Pipedrive ----------------------------------------------------------------------------------


def test_pipedrive_creates_org_reuses_person_and_creates_lead(
    hot_lead: LeadIn, result: Qualification
) -> None:
    recorder = Recorder(
        {
            ("GET", "/api/v2/organizations/search"): httpx.Response(
                200, json={"success": True, "data": {"items": []}}
            ),
            ("POST", "/api/v2/organizations"): httpx.Response(
                200, json={"success": True, "data": {"id": 77, "name": "Harborline Logistics"}}
            ),
            ("GET", "/api/v2/persons/search"): httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"items": [{"result_score": 1.0, "item": {"id": 42, "name": "Dana Whitfield"}}]},
                },
            ),
            ("POST", "/v1/leads"): httpx.Response(
                201, json={"success": True, "data": {"id": "adf21080-0e10-11eb-879b-05d71fb426ec"}}
            ),
            ("POST", "/v1/notes"): httpx.Response(200, json={"success": True, "data": {"id": 5}}),
        }
    )
    output = PipedriveOutput(
        "pd-token",
        label_ids_by_tier={"hot": ["f08b42a0-4e75-11ea-9643-03698ef1cfd6"]},
        owner_id=3,
        transport=httpx.MockTransport(recorder),
    )
    sent = asyncio.run(output.send(hot_lead, result))
    assert sent.ok, sent.detail
    assert sent.external_id == "adf21080-0e10-11eb-879b-05d71fb426ec"
    assert sent.data == {"person_id": 42, "organization_id": 77}
    assert all(r.headers["x-api-token"] == "pd-token" for r in recorder.requests)
    assert all("api_token" not in str(r.url) for r in recorder.requests)

    org_search = next(r for r in recorder.requests if r.url.path == "/api/v2/organizations/search")
    assert dict(org_search.url.params) == {
        "term": "Harborline Logistics",
        "fields": "name",
        "exact_match": "true",
    }
    person_search = next(r for r in recorder.requests if r.url.path == "/api/v2/persons/search")
    assert dict(person_search.url.params) == {
        "term": "dana@harborline-logistics.example",
        "fields": "email",
        "exact_match": "true",
    }
    assert not recorder.bodies("POST", "/api/v2/persons")  # existing person reused

    lead_body = recorder.bodies("POST", "/v1/leads")[0]
    assert lead_body == {
        "title": "Harborline Logistics [hot]",
        "person_id": 42,
        "organization_id": 77,
        "value": {"amount": 80000.0, "currency": "USD"},
        "label_ids": ["f08b42a0-4e75-11ea-9643-03698ef1cfd6"],
        "owner_id": 3,
    }
    note = recorder.bodies("POST", "/v1/notes")[0]
    assert note["lead_id"] == "adf21080-0e10-11eb-879b-05d71fb426ec" and "HOT" in note["content"]


def test_pipedrive_creates_person_with_v2_email_format(result: Qualification) -> None:
    lead = LeadIn(name="Ana Ruiz", email="ana@example.org", phone="+1 555 0100", message="Need an app")
    recorder = Recorder(
        {
            ("GET", "/api/v2/persons/search"): httpx.Response(200, json={"data": {"items": []}}),
            ("POST", "/api/v2/persons"): httpx.Response(200, json={"data": {"id": 9}}),
            ("POST", "/v1/leads"): httpx.Response(201, json={"data": {"id": "uuid-1"}}),
            ("POST", "/v1/notes"): httpx.Response(200, json={"data": {"id": 1}}),
        }
    )
    result = result.model_copy(
        update={"extracted": result.extracted.model_copy(update={"company": None, "budget_usd": None})}
    )
    sent = asyncio.run(PipedriveOutput("t", transport=httpx.MockTransport(recorder)).send(lead, result))
    assert sent.ok
    assert recorder.bodies("POST", "/api/v2/persons")[0] == {
        "name": "Ana Ruiz",
        "emails": [{"value": "ana@example.org", "primary": True, "label": "work"}],
        "phones": [{"value": "+1 555 0100", "primary": True, "label": "work"}],
    }
    assert recorder.bodies("POST", "/v1/leads")[0] == {"title": "Ana Ruiz [hot]", "person_id": 9}


def test_pipedrive_failure_is_reported(hot_lead: LeadIn, result: Qualification) -> None:
    recorder = Recorder(
        {("GET", "/api/v2/organizations/search"): httpx.Response(401, json={"error": "unauthorized"})}
    )
    sent = asyncio.run(PipedriveOutput("bad", transport=httpx.MockTransport(recorder)).send(hot_lead, result))
    assert not sent.ok and "401" in sent.detail


# --- Routing ------------------------------------------------------------------------------------


class FakeOutput:
    def __init__(self, name: str, crash: bool = False) -> None:
        self.name = name
        self.crash = crash
        self.received: list[Tier] = []

    async def send(self, lead: LeadIn, result: Qualification) -> OutputResult:
        if self.crash:
            raise RuntimeError("adapter bug")
        self.received.append(result.tier)
        return OutputResult(output=self.name, ok=True)


def test_route_follows_tier_rules_and_survives_crashes(
    icp: ICP, hot_lead: LeadIn, result: Qualification
) -> None:
    slack, hub = FakeOutput("slack"), FakeOutput("hubspot", crash=True)
    results = asyncio.run(route(hot_lead, result, icp, {"slack": slack, "hubspot": hub}))
    by_name = {r.output: r for r in results}
    assert slack.received == [Tier.HOT]
    assert by_name["hubspot"].ok is False and "crashed" in by_name["hubspot"].detail
    assert set(by_name) == {"slack", "hubspot"}  # unconfigured targets are skipped
    assert unconfigured_targets(icp, {"slack": slack, "hubspot": hub}) == ["pipedrive", "webhook"]
    spam = result.model_copy(update={"tier": Tier.SPAM})
    assert asyncio.run(route(hot_lead, spam, icp, {"slack": slack})) == []

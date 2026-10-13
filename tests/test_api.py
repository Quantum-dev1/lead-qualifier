import json
import time

import pytest
from fastapi.testclient import TestClient

from lead_qualifier.app import create_app
from lead_qualifier.config import Settings
from lead_qualifier.icp import ICP
from lead_qualifier.models import LeadIn, OutputResult, Qualification
from lead_qualifier.signature import sign
from lead_qualifier.store import DecisionStore
from tests.conftest import ScriptedLLM, llm_reply

SECRET = "test-webhook-secret"
HOT = {
    "source": "form",
    "name": "Dana Whitfield",
    "email": "dana@harborline-logistics.example",
    "company": "Harborline Logistics",
    "message": "We are a 120-person logistics company and need a web platform plus a mobile app. Budget $80k, launch in 4 months.",
}


class FakeOutput:
    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[str] = []

    async def send(self, lead: LeadIn, result: Qualification) -> OutputResult:
        self.sent.append(result.tier.value)
        return OutputResult(output=self.name, ok=True, detail="fake")


def make_client(icp: ICP, tmp_path, **settings_overrides) -> tuple[TestClient, dict[str, FakeOutput]]:
    values = {
        "webhook_secret": SECRET,
        "admin_password": "admin-pw",
        "database_path": str(tmp_path / "db.sqlite3"),
    }
    values.update(settings_overrides)
    settings = Settings(_env_file=None, **values)
    outputs = {name: FakeOutput(name) for name in ("slack", "hubspot", "pipedrive", "webhook")}
    app = create_app(
        settings,
        icp=icp,
        llm=ScriptedLLM(llm_reply()),
        outputs=outputs,
        store=DecisionStore(settings.database_path),
    )
    return TestClient(app), outputs


def signed(payload: dict, secret: str = SECRET, timestamp: int | None = None) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    return body, {"content-type": "application/json", **sign(secret, body, timestamp)}


def test_signed_lead_is_qualified_stored_and_routed(icp: ICP, tmp_path) -> None:
    client, outputs = make_client(icp, tmp_path)
    with client:
        body, headers = signed(HOT)
        response = client.post("/leads", content=body, headers=headers)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["tier"] == "hot" and data["llm_used"] is True and data["lead_id"]
        assert outputs["slack"].sent == ["hot"] and outputs["hubspot"].sent == ["hot"]

        stored = client.get(f"/leads/{data['lead_id']}", auth=("admin", "admin-pw")).json()
        assert stored["tier"] == "hot"
        assert {r["output"] for r in stored["routing"]} == {"slack", "hubspot", "pipedrive", "webhook"}
        assert stored["lead"]["email"] == HOT["email"]

        listed = client.get("/leads", params={"tier": "hot"}, auth=("admin", "admin-pw")).json()
        assert [row["id"] for row in listed] == [data["lead_id"]]


@pytest.mark.parametrize(
    ("mutate", "detail"),
    [
        (lambda body, headers: (body, {"content-type": "application/json"}), "missing"),
        (lambda body, headers: (body + b" ", headers), "mismatch"),
        (lambda body, headers: signed(HOT, secret="other-secret"), "mismatch"),
        (lambda body, headers: signed(HOT, timestamp=int(time.time()) - 3600), "replay"),
    ],
)
def test_bad_signatures_are_rejected(icp: ICP, tmp_path, mutate, detail: str) -> None:
    client, outputs = make_client(icp, tmp_path)
    body, headers = mutate(*signed(HOT))
    response = client.post("/leads", content=body, headers=headers)
    assert response.status_code == 401
    assert detail in response.json()["detail"]
    assert outputs["slack"].sent == []


def test_missing_secret_fails_closed(icp: ICP, tmp_path) -> None:
    client, _ = make_client(icp, tmp_path, webhook_secret=None)
    assert client.post("/leads", json=HOT).status_code == 503


def test_unsigned_mode_must_be_explicit(icp: ICP, tmp_path) -> None:
    client, _ = make_client(icp, tmp_path, webhook_secret=None, allow_unsigned_webhooks=True)
    assert client.post("/leads", json=HOT).status_code == 200


def test_invalid_payload_and_oversized_body(icp: ICP, tmp_path) -> None:
    client, _ = make_client(icp, tmp_path, max_body_bytes=500)
    body, headers = signed({"email": "not-an-email", "message": "hi"})
    response = client.post("/leads", content=body, headers=headers)
    assert response.status_code == 422
    big, headers = signed({**HOT, "message": "x" * 1000})
    assert client.post("/leads", content=big, headers=headers).status_code == 413


def test_dashboard_requires_password_and_renders(icp: ICP, tmp_path) -> None:
    client, _ = make_client(icp, tmp_path)
    body, headers = signed({**HOT, "message": HOT["message"] + " <script>alert(1)</script>"})
    client.post("/leads", content=body, headers=headers)
    assert client.get("/dashboard").status_code == 401
    assert client.get("/dashboard", auth=("admin", "wrong")).status_code == 401
    page = client.get("/dashboard", auth=("admin", "admin-pw"))
    assert page.status_code == 200
    assert "Lead Qualifier" in page.text and "Harborline Logistics" in page.text
    assert "<script>alert(1)</script>" not in page.text
    assert client.get("/dashboard?tier=spam", auth=("admin", "admin-pw")).text.count("No leads yet") == 1


def test_without_admin_password_only_localhost_can_read(icp: ICP, tmp_path) -> None:
    client, _ = make_client(icp, tmp_path, admin_password=None)
    assert client.get("/dashboard").status_code == 403  # TestClient's host is "testclient", not loopback
    local = TestClient(client.app, client=("127.0.0.1", 50000))
    assert local.get("/dashboard").status_code == 200


def test_healthz(icp: ICP, tmp_path) -> None:
    client, _ = make_client(icp, tmp_path)
    assert client.get("/healthz").json() == {"status": "ok"}

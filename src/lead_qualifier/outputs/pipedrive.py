"""Pipedrive CRM adapter (API token auth via the ``x-api-token`` header).

Endpoints and fields follow Pipedrive's official OpenAPI documents:

- ``GET  /api/v2/organizations/search`` (term, fields=name, exact_match) → ``data.items[].item.id``
- ``POST /api/v2/organizations``  {name}                                 → ``data.id``
- ``GET  /api/v2/persons/search`` (term, fields=email, exact_match)      → ``data.items[].item.id``
- ``POST /api/v2/persons``        {name, emails[], phones[], org_id}     → ``data.id``
- ``POST /v1/leads``              {title, person_id, organization_id, value{amount,currency}, label_ids, owner_id}
                                                                          → ``data.id`` (UUID)
- ``POST /v1/notes``              {content (HTML), lead_id}

Leads are only available in API v1 at the time of writing; persons and
organizations use v2.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..models import LeadIn, OutputResult, Qualification
from .base import OutputError, note_html, raise_for_status, request_with_retry


class PipedriveOutput:
    name = "pipedrive"

    def __init__(
        self,
        api_token: str,
        *,
        base_url: str = "https://api.pipedrive.com",
        currency: str = "USD",
        label_ids_by_tier: dict[str, list[str]] | None = None,
        owner_id: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.currency = currency
        self.label_ids_by_tier = label_ids_by_tier or {}
        self.owner_id = owner_id
        self._transport = transport

    @staticmethod
    def _first_search_id(response: httpx.Response) -> int | None:
        items = ((response.json() or {}).get("data") or {}).get("items") or []
        for entry in items:
            item = entry.get("item") or {}
            if "id" in item:
                return int(item["id"])
        return None

    @staticmethod
    def _created_id(response: httpx.Response, what: str) -> Any:
        data = (response.json() or {}).get("data") or {}
        if "id" not in data:
            raise OutputError(f"Pipedrive {what}: response has no data.id")
        return data["id"]

    async def _find_or_create_org(self, client: httpx.AsyncClient, name: str) -> int:
        search = await request_with_retry(
            client,
            "GET",
            "/api/v2/organizations/search",
            params={"term": name, "fields": "name", "exact_match": "true"},
        )
        raise_for_status(search, "Pipedrive organization search")
        if (found := self._first_search_id(search)) is not None:
            return found
        created = await request_with_retry(client, "POST", "/api/v2/organizations", json={"name": name})
        raise_for_status(created, "Pipedrive organization create")
        return int(self._created_id(created, "organization create"))

    async def _find_or_create_person(
        self, client: httpx.AsyncClient, lead: LeadIn, org_id: int | None
    ) -> int:
        if lead.email:
            search = await request_with_retry(
                client,
                "GET",
                "/api/v2/persons/search",
                params={"term": lead.email, "fields": "email", "exact_match": "true"},
            )
            raise_for_status(search, "Pipedrive person search")
            if (found := self._first_search_id(search)) is not None:
                return found
        body: dict[str, Any] = {"name": lead.name or lead.email or "Unknown"}
        if lead.email:
            body["emails"] = [{"value": lead.email, "primary": True, "label": "work"}]
        if lead.phone:
            body["phones"] = [{"value": lead.phone, "primary": True, "label": "work"}]
        if org_id is not None:
            body["org_id"] = org_id
        created = await request_with_retry(client, "POST", "/api/v2/persons", json=body)
        raise_for_status(created, "Pipedrive person create")
        return int(self._created_id(created, "person create"))

    async def send(self, lead: LeadIn, result: Qualification) -> OutputResult:
        headers = {"x-api-token": self.api_token, "content-type": "application/json"}
        company = result.extracted.company or lead.company
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, headers=headers, timeout=15, transport=self._transport
            ) as client:
                org_id = await self._find_or_create_org(client, company) if company else None
                person_id = await self._find_or_create_person(client, lead, org_id)

                lead_body: dict[str, Any] = {
                    "title": f"{company or lead.name or lead.email or 'Inbound lead'} [{result.tier.value}]",
                    "person_id": person_id,
                }
                if org_id is not None:
                    lead_body["organization_id"] = org_id
                if result.extracted.budget_usd:
                    lead_body["value"] = {
                        "amount": round(result.extracted.budget_usd, 2),
                        "currency": self.currency,
                    }
                if labels := self.label_ids_by_tier.get(result.tier.value):
                    lead_body["label_ids"] = labels
                if self.owner_id is not None:
                    lead_body["owner_id"] = self.owner_id
                created = await request_with_retry(client, "POST", "/v1/leads", json=lead_body)
                raise_for_status(created, "Pipedrive lead create")
                lead_id = str(self._created_id(created, "lead create"))

                note = await request_with_retry(
                    client, "POST", "/v1/notes", json={"content": note_html(lead, result), "lead_id": lead_id}
                )
                raise_for_status(note, "Pipedrive note")
        except (OutputError, httpx.HTTPError, ValueError) as exc:
            return OutputResult(output=self.name, ok=False, detail=str(exc))
        return OutputResult(
            output=self.name,
            ok=True,
            detail="lead created with note",
            external_id=lead_id,
            data={"person_id": person_id, "organization_id": org_id},
        )

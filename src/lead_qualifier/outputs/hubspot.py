"""HubSpot CRM adapter (CRM v3 API, private app access token).

Flow, per HubSpot's documentation:

1. Upsert the contact by email:
   ``POST /crm/v3/objects/contacts/batch/upsert`` with ``idProperty: "email"``.
   The response's ``results[0].id`` is the contact id.
2. Attach a note with the qualification summary:
   ``POST /crm/v3/objects/notes`` with ``hs_timestamp`` and ``hs_note_body``,
   associated to the contact with the HubSpot-defined association type ``202``
   (note → contact).

Required private app scopes: ``crm.objects.contacts.write`` (and ``.read``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from ..models import LeadIn, OutputResult, Qualification
from .base import OutputError, note_html, raise_for_status, request_with_retry

NOTE_TO_CONTACT_ASSOCIATION_TYPE_ID = 202


def split_name(name: str | None) -> tuple[str | None, str | None]:
    if not name:
        return None, None
    parts = name.split()
    return parts[0], (" ".join(parts[1:]) or None)


class HubSpotOutput:
    name = "hubspot"

    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = "https://api.hubapi.com",
        properties_by_tier: dict[str, dict[str, str]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.properties_by_tier = properties_by_tier or {}
        self._transport = transport

    def contact_properties(self, lead: LeadIn, result: Qualification) -> dict[str, str]:
        first, last = split_name(lead.name)
        props: dict[str, Any] = {
            "email": lead.email,
            "firstname": first,
            "lastname": last,
            "company": result.extracted.company or lead.company,
            "phone": lead.phone,
            "website": lead.website,
            "lifecyclestage": "lead",
        }
        props.update(self.properties_by_tier.get(result.tier.value, {}))
        # HubSpot property values are strings; drop empty ones so we never blank out existing data.
        return {k: str(v) for k, v in props.items() if v not in (None, "")}

    async def send(self, lead: LeadIn, result: Qualification) -> OutputResult:
        if not lead.email:
            return OutputResult(
                output=self.name,
                ok=False,
                detail="skipped: lead has no email (HubSpot contacts are upserted by email)",
            )
        headers = {"authorization": f"Bearer {self.access_token}", "content-type": "application/json"}
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, headers=headers, timeout=15, transport=self._transport
            ) as client:
                upsert = await request_with_retry(
                    client,
                    "POST",
                    "/crm/v3/objects/contacts/batch/upsert",
                    json={
                        "inputs": [
                            {
                                "id": lead.email,
                                "idProperty": "email",
                                "properties": self.contact_properties(lead, result),
                            }
                        ]
                    },
                )
                raise_for_status(upsert, "HubSpot contact upsert")
                results = upsert.json().get("results") or []
                if not results or "id" not in results[0]:
                    raise OutputError("HubSpot contact upsert returned no results")
                contact_id = str(results[0]["id"])

                note = await request_with_retry(
                    client,
                    "POST",
                    "/crm/v3/objects/notes",
                    json={
                        "properties": {
                            "hs_timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                            "hs_note_body": note_html(lead, result)[:65_000],
                        },
                        "associations": [
                            {
                                "to": {"id": contact_id},
                                "types": [
                                    {
                                        "associationCategory": "HUBSPOT_DEFINED",
                                        "associationTypeId": NOTE_TO_CONTACT_ASSOCIATION_TYPE_ID,
                                    }
                                ],
                            }
                        ],
                    },
                )
                raise_for_status(note, "HubSpot note")
                note_id = str(note.json().get("id", ""))
        except (OutputError, httpx.HTTPError, ValueError) as exc:
            return OutputResult(output=self.name, ok=False, detail=str(exc))
        return OutputResult(
            output=self.name,
            ok=True,
            detail="contact upserted, note attached",
            external_id=contact_id,
            data={"note_id": note_id},
        )

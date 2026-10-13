"""Generic JSON webhook output, optionally HMAC-signed like inbound webhooks."""

from __future__ import annotations

import json

import httpx

from ..models import LeadIn, OutputResult, Qualification
from ..signature import sign
from .base import OutputError, raise_for_status, request_with_retry


class WebhookOutput:
    name = "webhook"

    def __init__(
        self, url: str, *, secret: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.url = url
        self.secret = secret
        self._transport = transport

    async def send(self, lead: LeadIn, result: Qualification) -> OutputResult:
        body = json.dumps(
            {
                "event": "lead.qualified",
                "lead": lead.model_dump(mode="json"),
                "qualification": result.model_dump(mode="json"),
            },
            ensure_ascii=False,
        ).encode()
        headers = {"content-type": "application/json"}
        if self.secret:
            headers.update(sign(self.secret, body))
        try:
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
                response = await request_with_retry(client, "POST", self.url, content=body, headers=headers)
            raise_for_status(response, "Webhook")
        except (OutputError, httpx.HTTPError) as exc:
            return OutputResult(output=self.name, ok=False, detail=str(exc))
        return OutputResult(output=self.name, ok=True, detail=f"HTTP {response.status_code}")

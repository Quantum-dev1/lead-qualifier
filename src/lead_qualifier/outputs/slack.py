"""Slack incoming webhook output (https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks)."""

from __future__ import annotations

import httpx

from ..models import LeadIn, OutputResult, Qualification, Tier
from .base import OutputError, raise_for_status, request_with_retry, summary_lines

EMOJI = {Tier.HOT: ":fire:", Tier.WARM: ":sunny:", Tier.COLD: ":snowflake:", Tier.SPAM: ":no_entry:"}


def escape(text: str) -> str:
    """Slack mrkdwn escaping. Also stops lead text from triggering <!channel> pings or fake links."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_payload(lead: LeadIn, result: Qualification, dashboard_url: str | None = None) -> dict[str, object]:
    lines = summary_lines(lead, result)
    headline = f"{EMOJI[result.tier]} {result.tier.value.upper()} lead: {result.extracted.company or lead.name or lead.email or 'unknown'} ({result.score:.0f}/100)"
    fields = [{"type": "mrkdwn", "text": f"*{escape(k)}*\n{escape(v)}"} for k, v in lines[1:]]
    blocks: list[dict[str, object]] = [
        {"type": "header", "text": {"type": "plain_text", "text": headline[:150], "emoji": True}},
        {"type": "section", "fields": fields[:10]},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Message*\n>" + escape(lead.message[:600]).replace("\n", "\n>"),
            },
        },
    ]
    if result.reasons:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": escape(" ".join(result.reasons))[:1900]}],
            }
        )
    if result.reply_draft:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Suggested reply*\n" + escape(result.reply_draft[:1500])},
            }
        )
    if dashboard_url and result.lead_id:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"<{dashboard_url.rstrip('/')}/leads/{result.lead_id}|Open decision {result.lead_id[:8]}>",
                    }
                ],
            }
        )
    return {"text": escape(headline), "blocks": blocks}


class SlackOutput:
    name = "slack"

    def __init__(
        self,
        webhook_url: str,
        *,
        dashboard_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.dashboard_url = dashboard_url
        self._transport = transport

    async def send(self, lead: LeadIn, result: Qualification) -> OutputResult:
        payload = build_payload(lead, result, self.dashboard_url)
        try:
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
                response = await request_with_retry(client, "POST", self.webhook_url, json=payload)
            raise_for_status(response, "Slack webhook")
        except (OutputError, httpx.HTTPError) as exc:
            return OutputResult(output=self.name, ok=False, detail=str(exc))
        return OutputResult(output=self.name, ok=True, detail=response.text[:100])

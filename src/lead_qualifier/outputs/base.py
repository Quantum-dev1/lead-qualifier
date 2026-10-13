"""Output plumbing: the ``Output`` protocol, a retrying HTTP helper and lead summaries."""

from __future__ import annotations

import asyncio
import html
from typing import Any, Protocol

import httpx

from ..models import LeadIn, OutputResult, Qualification


class Output(Protocol):
    """Anything that can receive a qualified lead: chat, webhook, CRM…"""

    name: str

    async def send(self, lead: LeadIn, result: Qualification) -> OutputResult: ...


class OutputError(RuntimeError):
    pass


RETRY_STATUSES = {429, 500, 502, 503, 504}


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    backoff: float = 0.5,
    **kwargs: Any,
) -> httpx.Response:
    """Retry transient failures (429, 5xx, network errors), honouring ``Retry-After`` up to 10 s."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last_error = exc
        else:
            if response.status_code not in RETRY_STATUSES or attempt == attempts - 1:
                return response
            retry_after = response.headers.get("retry-after", "")
            if retry_after.isdigit():
                await asyncio.sleep(min(float(retry_after), 10.0))
                continue
        if attempt < attempts - 1:
            await asyncio.sleep(backoff * 2**attempt)
    raise OutputError(f"{method} {url} failed after {attempts} attempts: {last_error}")


def raise_for_status(response: httpx.Response, what: str) -> None:
    if response.status_code >= 400:
        raise OutputError(f"{what} failed: HTTP {response.status_code} {response.text[:300]}")


def summary_lines(lead: LeadIn, result: Qualification) -> list[tuple[str, str]]:
    x = result.extracted
    lines = [
        ("Tier", result.tier.value.upper()),
        (
            "Score",
            f"{result.score:.0f}/100 (rules {result.rule_score:.0f}"
            + (f", model {result.llm_score:.0f})" if result.llm_score is not None else ")"),
        ),
        (
            "Contact",
            " ".join(filter(None, [lead.name, f"<{lead.email}>" if lead.email else None])) or "unknown",
        ),
        ("Company", x.company or "unknown"),
        ("Budget", x.budget or "not stated"),
        ("Timeline", x.timeline or "not stated"),
        ("Need", x.need or "unclear"),
    ]
    if result.needs_review:
        lines.append(("Review", "needs human review"))
    return lines


def note_html(lead: LeadIn, result: Qualification) -> str:
    """An HTML note for CRMs. Every value is escaped: lead text is untrusted."""
    esc = html.escape
    parts = [f"<b>Lead qualifier: {esc(result.tier.value.upper())} ({result.score:.0f}/100)</b>"]
    parts += [f"<b>{esc(k)}:</b> {esc(v)}" for k, v in summary_lines(lead, result)[2:]]
    if result.reasons:
        parts.append("<b>Reasoning:</b><br>" + "<br>".join(esc(r) for r in result.reasons))
    if result.red_flags:
        parts.append("<b>Red flags:</b> " + esc("; ".join(result.red_flags)))
    if result.reply_draft:
        parts.append("<b>Suggested reply:</b><br>" + esc(result.reply_draft).replace("\n", "<br>"))
    parts.append("<b>Original message:</b><br>" + esc(lead.message[:4000]).replace("\n", "<br>"))
    return "<br><br>".join(parts)

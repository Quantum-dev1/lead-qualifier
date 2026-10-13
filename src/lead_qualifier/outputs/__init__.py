"""Outputs and CRM adapters.

Built-in: ``slack``, ``webhook``, ``hubspot``, ``pipedrive``. Third-party
packages can add more by exposing a factory under the ``lead_qualifier.outputs``
entry-point group. The factory is called as ``factory(settings, icp)`` and
returns an object with a ``name`` attribute and an async
``send(lead, result) -> OutputResult`` method, or ``None`` when it is not
configured.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from importlib.metadata import entry_points

from ..config import Settings
from ..icp import ICP
from ..models import LeadIn, OutputResult, Qualification
from .base import Output, OutputError
from .hubspot import HubSpotOutput
from .pipedrive import PipedriveOutput
from .slack import SlackOutput
from .webhook import WebhookOutput

log = logging.getLogger(__name__)

OutputFactory = Callable[[Settings, ICP], Output | None]
ENTRY_POINT_GROUP = "lead_qualifier.outputs"

__all__ = [
    "HubSpotOutput",
    "Output",
    "OutputError",
    "PipedriveOutput",
    "SlackOutput",
    "WebhookOutput",
    "build_outputs",
    "route",
    "unconfigured_targets",
]


def _builtin_outputs(settings: Settings, icp: ICP) -> list[Output]:
    outputs: list[Output] = []
    if settings.slack_webhook_url:
        outputs.append(
            SlackOutput(settings.slack_webhook_url.get_secret_value(), dashboard_url=settings.public_url)
        )
    if settings.output_webhook_url:
        secret = settings.output_webhook_secret.get_secret_value() if settings.output_webhook_secret else None
        outputs.append(WebhookOutput(settings.output_webhook_url, secret=secret))
    if settings.hubspot_access_token:
        hubspot_cfg = icp.crm.get("hubspot", {}) or {}
        outputs.append(
            HubSpotOutput(
                settings.hubspot_access_token.get_secret_value(),
                base_url=settings.hubspot_base_url,
                properties_by_tier=hubspot_cfg.get("properties_by_tier") or {},
            )
        )
    if settings.pipedrive_api_token:
        pipedrive_cfg = icp.crm.get("pipedrive", {}) or {}
        outputs.append(
            PipedriveOutput(
                settings.pipedrive_api_token.get_secret_value(),
                base_url=settings.pipedrive_base_url,
                currency=icp.budget.currency,
                label_ids_by_tier=pipedrive_cfg.get("label_ids_by_tier") or {},
                owner_id=pipedrive_cfg.get("owner_id"),
            )
        )
    return outputs


def build_outputs(settings: Settings, icp: ICP) -> dict[str, Output]:
    outputs = {o.name: o for o in _builtin_outputs(settings, icp)}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            factory: OutputFactory = ep.load()
            output = factory(settings, icp)
        except Exception:
            log.exception("Failed to load output plugin %s", ep.name)
            continue
        if output is not None:
            outputs[output.name] = output
    return outputs


async def route(
    lead: LeadIn, result: Qualification, icp: ICP, outputs: dict[str, Output]
) -> list[OutputResult]:
    """Send the lead to every output configured for its tier, concurrently. Never raises."""
    # Targets without configuration are skipped silently; `unconfigured_targets` reports them once at startup.
    tasks = [outputs[name] for name in icp.routing.get(result.tier, []) if name in outputs]

    async def _safe_send(output: Output) -> OutputResult:
        try:
            return await output.send(lead, result)
        except Exception as exc:  # an adapter bug must not lose the lead
            log.exception("Output %s crashed", output.name)
            return OutputResult(output=output.name, ok=False, detail=f"crashed: {exc}")

    return list(await asyncio.gather(*(_safe_send(o) for o in tasks)))


def unconfigured_targets(icp: ICP, outputs: dict[str, Output]) -> list[str]:
    """Output names used in the ICP routing table that have no configuration."""
    return sorted({name for names in icp.routing.values() for name in names if name not in outputs})

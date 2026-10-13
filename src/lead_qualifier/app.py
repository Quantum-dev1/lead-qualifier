"""FastAPI application: signed webhook intake, decision log and dashboard."""

from __future__ import annotations

import ipaddress
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import ValidationError

from . import __version__
from .config import Settings
from .dashboard import TIERS, render_dashboard
from .icp import ICP, load_icp
from .llm import LLMClient, OpenAICompatibleClient
from .models import LeadIn, Qualification
from .outputs import Output, build_outputs, route, unconfigured_targets
from .qualifier import Qualifier
from .signature import SIGNATURE_HEADER, TIMESTAMP_HEADER, SignatureError, verify
from .store import DecisionStore

log = logging.getLogger("lead_qualifier")
basic = HTTPBasic(auto_error=False)


def build_llm(settings: Settings) -> LLMClient | None:
    if not settings.llm_model:
        return None
    return OpenAICompatibleClient(
        settings.llm_base_url,
        settings.llm_model,
        settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
        timeout=settings.llm_timeout,
        json_mode=settings.llm_json_mode,
    )


def create_app(
    settings: Settings | None = None,
    *,
    icp: ICP | None = None,
    llm: LLMClient | None = None,
    outputs: dict[str, Output] | None = None,
    store: DecisionStore | None = None,
) -> FastAPI:
    settings = settings or Settings()
    icp = icp or load_icp(settings.icp_path)
    llm = llm if llm is not None else build_llm(settings)
    outputs = outputs if outputs is not None else build_outputs(settings, icp)
    store = store or DecisionStore(settings.database_path)
    qualifier = Qualifier(icp, llm)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if not settings.webhook_secrets() and not settings.allow_unsigned_webhooks:
            log.warning("WEBHOOK_SECRET is not set: POST /leads will reject every request.")
        if settings.allow_unsigned_webhooks:
            log.warning("ALLOW_UNSIGNED_WEBHOOKS=true: anyone who can reach /leads can submit leads.")
        log.info(
            "LLM: %s | outputs: %s",
            llm.model if llm else "disabled (rules only)",
            ", ".join(outputs) or "none",
        )
        if missing := unconfigured_targets(icp, outputs):
            log.info(
                "Routing mentions outputs that are not configured and will be skipped: %s", ", ".join(missing)
            )
        yield
        store.close()

    app = FastAPI(
        title="Lead Qualifier",
        version=__version__,
        description="Qualifies inbound leads against your ICP and routes them to Slack, webhooks and CRMs.",
        lifespan=lifespan,
    )
    app.state.settings, app.state.store, app.state.qualifier = settings, store, qualifier

    def require_admin(
        request: Request, credentials: Annotated[HTTPBasicCredentials | None, Depends(basic)]
    ) -> None:
        password = settings.admin_password.get_secret_value() if settings.admin_password else None
        if password:
            if credentials and secrets.compare_digest(credentials.password.encode(), password.encode()):
                return
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Authentication required", headers={"WWW-Authenticate": "Basic"}
            )
        host = request.client.host if request.client else ""
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Set ADMIN_PASSWORD to access this page from another machine"
        )

    async def deliver(decision_id: str, lead: LeadIn, result: Qualification) -> None:
        results = await route(lead, result, icp, outputs)
        store.save_routing(decision_id, results)
        for r in results:
            if not r.ok:
                log.warning("Output %s failed for %s: %s", r.output, decision_id, r.detail)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/leads", response_model=Qualification, summary="Qualify an inbound lead (HMAC-signed webhook)")
    async def submit_lead(request: Request, background: BackgroundTasks) -> Qualification:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
            raise HTTPException(413, "Body too large")
        body = await request.body()
        if len(body) > settings.max_body_bytes:
            raise HTTPException(413, "Body too large")

        if not settings.allow_unsigned_webhooks:
            webhook_secrets = settings.webhook_secrets()
            if not webhook_secrets:
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "WEBHOOK_SECRET is not configured")
            try:
                verify(
                    webhook_secrets,
                    body,
                    request.headers.get(TIMESTAMP_HEADER),
                    request.headers.get(SIGNATURE_HEADER),
                    tolerance=settings.webhook_tolerance_seconds,
                )
            except SignatureError as exc:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid signature: {exc}") from exc

        try:
            lead = LeadIn.model_validate_json(body)
        except ValidationError as exc:
            raise HTTPException(
                422, exc.errors(include_url=False, include_context=False, include_input=False)
            ) from exc

        result = await qualifier.qualify(lead)
        decision_id = store.save(lead, result)
        result.lead_id = decision_id
        background.add_task(deliver, decision_id, lead, result)
        return result

    @app.get("/leads", dependencies=[Depends(require_admin)], summary="Recent decisions")
    async def list_leads(
        tier: Annotated[str | None, Query(pattern="^(hot|warm|cold|spam)$")] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> list[dict[str, Any]]:
        return store.recent(limit=limit, tier=tier)

    @app.get("/leads/{decision_id}", dependencies=[Depends(require_admin)], summary="One decision")
    async def get_lead(decision_id: str) -> dict[str, Any]:
        row = store.get(decision_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Decision not found")
        return row

    @app.get(
        "/dashboard",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin)],
        include_in_schema=False,
    )
    async def dashboard(tier: str | None = None) -> HTMLResponse:
        active = tier if tier in TIERS else None
        return HTMLResponse(render_dashboard(store.stats(), store.recent(limit=100, tier=active), active))

    return app

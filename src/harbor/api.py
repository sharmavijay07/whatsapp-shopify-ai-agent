"""HTTP surface: sandbox chat, WhatsApp webhooks, and n8n callbacks."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from harbor.config import Settings, get_settings
from harbor.phone import PhoneError
from harbor.service import AgentService, build_service
from harbor.whatsapp import extract_messages, verify_signature

logger = logging.getLogger("harbor.api")


class ChatIn(BaseModel):
    phone: str
    text: str = Field(min_length=1, max_length=2000)
    name: str = ""


class AuditIn(BaseModel):
    phone: str | None = None
    action: str = Field(min_length=1, max_length=80)
    outcome: str = Field(min_length=1, max_length=40)
    detail: dict = Field(default_factory=dict)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = FastAPI(title="Harbor & Co. commerce agent", version="0.1.0")
    origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.settings = settings
    app.state.service = build_service(settings)

    @app.get("/health")
    def health() -> dict:
        service: AgentService = app.state.service
        status = service.settings.llm_status()
        return {
            "status": "ok",
            "mode": "mock" if service.settings.shopify_is_mock else "live",
            "shopify": "mock" if service.settings.shopify_is_mock else "live",
            "whatsapp": "live" if service.settings.whatsapp_is_live else "mock",
            "llm": status,
            "vector_store": service.settings.vector_store,
            "vector_points": service.rag.vector_count(),
        }

    @app.get("/api/meta")
    def meta() -> dict:
        return health()

    @app.get("/api/sandbox")
    def sandbox(phone: str) -> dict:
        service: AgentService = app.state.service
        try:
            return service.snapshot(phone).as_dict()
        except PhoneError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/chat")
    def chat(body: ChatIn) -> dict:
        service: AgentService = app.state.service
        try:
            return service.handle(body.phone, body.text, body.name, channel="sandbox").as_dict()
        except PhoneError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/sandbox/reset")
    def reset() -> dict:
        service: AgentService = app.state.service
        if not service.settings.shopify_is_mock:
            raise HTTPException(409, "Reset is only available in mock mode.")
        service.reset()
        return {"ok": True}

    @app.post("/api/escalations/{escalation_id}/resolve")
    def resolve(escalation_id: str) -> dict:
        service: AgentService = app.state.service
        row = service.resolve_escalation(escalation_id)
        if row is None:
            raise HTTPException(404, "No escalation with that id.")
        return row

    @app.post("/api/internal/audit")
    def external_audit(body: AuditIn, request: Request) -> dict:
        service: AgentService = app.state.service
        secret = service.settings.n8n_shared_secret
        if secret and request.headers.get("x-harbor-secret") != secret:
            raise HTTPException(401, "Bad shared secret.")
        service.db.audit(body.phone, f"n8n.{body.action}", body.outcome, body.detail)
        return {"ok": True}

    @app.get("/webhooks/whatsapp")
    def whatsapp_verify(request: Request):
        service: AgentService = app.state.service
        params = request.query_params
        token = params.get("hub.verify_token")
        if params.get("hub.mode") == "subscribe" and token == service.settings.whatsapp_verify_token:
            return PlainTextResponse(params.get("hub.challenge") or "")
        raise HTTPException(403, "Verification failed.")

    @app.post("/webhooks/whatsapp")
    async def whatsapp_inbound(request: Request) -> dict:
        service: AgentService = app.state.service
        body = await request.body()
        secret = service.settings.whatsapp_app_secret
        if secret and not verify_signature(body, request.headers.get("x-hub-signature-256"), secret):
            raise HTTPException(403, "Bad signature.")
        if service.settings.app_mode == "live" and not secret:
            logger.warning("WHATSAPP_APP_SECRET is empty; webhook signatures are not being checked.")
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, "Expected a JSON body.") from exc
        handled = 0
        for message in extract_messages(payload):
            if not message["from"]:
                continue
            text = message["text"]
            if message.get("unsupported"):
                text = "I can only read text messages right now."
                # Still run it so the customer gets that explanation once.
            if not text:
                continue
            result = service.handle(
                message["from"],
                text if not message.get("unsupported") else "Please send that as a text message.",
                message.get("name") or "",
                channel="whatsapp",
                external_id=message.get("id") or None,
            )
            if message.get("unsupported") and not result.duplicate_inbound:
                # The line above already answered. Nothing else.
                pass
            handled += 0 if result.duplicate_inbound else 1
        return {"ok": True, "handled": handled}

    return app


_app: FastAPI | None = None


def __getattr__(name: str):
    """Build the ASGI app on first use so imports and tests do not lock Qdrant."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

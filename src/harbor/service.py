"""One turn of the agent, shared by the sandbox UI and the WhatsApp webhook."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from langchain_core.messages import AIMessage, HumanMessage

from harbor.agent import HarborAgent, ToolContext, ctx_var
from harbor.backend import MockShopify
from harbor.catalog import product_documents
from harbor.config import Settings
from harbor.db import Database
from harbor.mailer import Mailer
from harbor.money import format_money
from harbor.n8n_client import N8NClient
from harbor.parsing import is_explicit_confirmation
from harbor.phone import PhoneError, normalize_phone
from harbor.rag import KnowledgeBase
from harbor.safeguards import scrub_false_order_claim
from harbor.service_ids import uid
from harbor.shopify_live import LiveShopify
from harbor.shopify_webhooks import normalize_shopify_webhook
from harbor.whatsapp import WhatsAppClient

logger = logging.getLogger("harbor.service")


@dataclass
class TurnResult:
    reply: str
    phone: str
    turn_id: str
    sources: list = field(default_factory=list)
    draft: dict | None = None
    order: dict | None = None
    escalation: dict | None = None
    safeguard: dict | None = None
    customer: dict | None = None
    orders: list = field(default_factory=list)
    audit: list = field(default_factory=list)
    escalations: list = field(default_factory=list)
    products: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    llm: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)
    duplicate_inbound: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


class AgentService:
    def __init__(self, settings: Settings, db: Database, backend, rag: KnowledgeBase, agent: HarborAgent, whatsapp: WhatsAppClient, n8n: N8NClient, mailer: Mailer):
        self.settings = settings
        self.db = db
        self.backend = backend
        self.rag = rag
        self.agent = agent
        self.whatsapp = whatsapp
        self.n8n = n8n
        self.mailer = mailer

    def close(self) -> None:
        for resource in (self.rag, self.whatsapp, self.n8n, self.mailer, self.backend, self.db):
            closer = getattr(resource, "close", None)
            if closer:
                closer()

    def handle(
        self,
        phone_raw: str,
        text: str,
        profile_name: str = "",
        channel: str = "sandbox",
        external_id: str | None = None,
    ) -> TurnResult:
        phone = normalize_phone(phone_raw)
        if external_id and self.db.seen_inbound(external_id):
            self.db.audit(
                phone,
                "whatsapp.duplicate_inbound",
                "blocked",
                {"id": external_id},
            )
            snapshot = self.snapshot(phone)
            snapshot.reply = ""
            snapshot.duplicate_inbound = True
            return snapshot

        turn_id = uid("turn")
        if external_id:
            self.db.remember_inbound(external_id, phone)
        if profile_name.strip():
            self.db.set_conversation(phone, customer_name=profile_name.strip())
        conversation = self.db.get_conversation(phone)
        name = profile_name.strip() or conversation.get("customer_name") or ""
        self.db.add_message(phone, "user", text, turn_id)
        history = []
        for item in self.db.history(phone, limit=12):
            if item["role"] == "assistant":
                history.append(AIMessage(content=item["content"]))
            else:
                history.append(HumanMessage(content=item["content"]))
        draft = self.db.pending_draft(phone)
        explicit = is_explicit_confirmation(text, draft["token"] if draft else None)
        ctx = ToolContext(
            phone=phone,
            profile_name=name,
            turn_id=turn_id,
            user_text=text,
            explicit_confirmation=explicit,
            escalated=bool(conversation.get("escalated")),
            db=self.db,
            backend=self.backend,
            settings=self.settings,
        )
        token = ctx_var.set(ctx)
        retrieved: list = []
        try:
            state = self.agent.graph.invoke(
                {
                    "messages": history,
                    "phone": phone,
                    "turn_id": turn_id,
                    "user_text": text,
                    "profile_name": name,
                    "explicit_confirmation": explicit,
                    "retrieved": [],
                    "tool_rounds": 0,
                    "tool_events": [],
                }
            )
            retrieved = state.get("retrieved") or []
            reply = (state.get("reply") or "").strip()
        except Exception as exc:  # noqa: BLE001 — never leave the customer unsure about an order
            logger.exception("agent turn failed")
            self.db.audit(phone, "agent.error", "error", {"error": str(exc)[:500]})
            self.n8n.error(
                {
                    "phone": phone,
                    "verified_phone": phone,
                    "action": "agent.error",
                    "message": str(exc)[:500],
                }
            )
            reply = "Something went wrong on our side. I have not placed or changed an order."
        finally:
            ctx_var.reset(token)

        created_new = bool(
            ctx.order_result
            and ctx.order_result.get("code") == "created"
            and ctx.created_order
            and not ctx.created_order.get("duplicate")
        )
        reply = scrub_false_order_claim(reply or "I didn't catch that.", created_new)
        self.db.add_message(phone, "assistant", reply, turn_id)
        self.db.audit(
            phone,
            "conversation.turn",
            "success",
            {
                "preview": text[:140],
                "channel": channel,
                "safeguard": None if not ctx.safeguard else ctx.safeguard.get("code"),
            },
        )
        if created_new and ctx.created_order is not None:
            self.n8n.order_created(
                {
                    "verified_phone": phone,
                    "confirmation": {
                        "explicit": True,
                        "token": ctx.created_order.get("confirmation_token"),
                        "same_turn": False,
                    },
                    "idempotency_key": ctx.created_order.get("idempotency_key"),
                    "customer": {
                        "id": (ctx.customer or {}).get("id"),
                        "name": (ctx.customer or {}).get("name"),
                        "phone": phone,
                    },
                    "shopify_draft_id": (ctx.order_result.get("draft") or {}).get("shopify_draft_id") or "",
                    "order": {
                        "id": ctx.created_order["id"],
                        "number": ctx.created_order["number"],
                        "total_cents": ctx.created_order["total_cents"],
                        "lines": ctx.created_order.get("lines") or [],
                        "shopify_draft_id": (ctx.order_result.get("draft") or {}).get("shopify_draft_id") or "",
                    },
                    "total_cents": ctx.created_order["total_cents"],
                    "limits": {"max_order_value_cents": self.settings.max_order_value_cents},
                    "already_created": True,
                    "notify_whatsapp": False,
                }
            )
        elif ctx.safeguard and ctx.safeguard.get("code") in {"over_limit", "shopify_error"}:
            self.n8n.error(
                {
                    "phone": phone,
                    "verified_phone": phone,
                    "action": ctx.safeguard["code"],
                    "message": ctx.safeguard.get("message"),
                }
            )
        if created_new and ctx.created_order is not None:
            customer = ctx.customer or {}
            self.mailer.order_confirmation(
                phone=phone,
                number=str(ctx.created_order.get("number") or ""),
                total=format_money(
                    ctx.created_order.get("total_cents") or 0,
                    ctx.created_order.get("currency") or self.settings.currency,
                ),
                customer_email=customer.get("email") or "",
                customer_name=customer.get("name") or "",
                invoice_sent=ctx.created_order.get("invoice_sent"),
            )
        if ctx.extra.get("escalation_opened") and ctx.escalation:
            self.mailer.handoff_alert(
                phone=phone,
                reason=ctx.extra.get("escalation_reason") or text,
                escalation_id=ctx.escalation["id"],
            )
        if channel == "whatsapp" and reply:
            try:
                rows = ctx.extra.get("wa_list") or []
                if ctx.draft and not created_new:
                    self.whatsapp.send_reply_buttons(phone, reply)
                elif len(rows) >= 2:
                    self.whatsapp.send_product_list(phone, reply, rows)
                else:
                    self.whatsapp.send_text(phone, reply)
            except Exception as exc:  # noqa: BLE001
                logger.exception("whatsapp send failed")
                self.db.audit(phone, "whatsapp.send", "error", {"error": str(exc)[:300]})

        result = self.snapshot(phone)
        result.reply = reply
        result.turn_id = turn_id
        result.sources = [
            {"title": hit["title"], "excerpt": hit["text"][:280], "kind": hit["kind"], "source": hit["source"]}
            for hit in retrieved[:3]
        ]
        result.safeguard = ctx.safeguard
        result.order = ctx.created_order
        result.escalation = ctx.escalation or self.db.open_escalation(phone)
        return result

    def snapshot(self, phone_raw: str) -> TurnResult:
        phone = normalize_phone(phone_raw)
        return TurnResult(
            reply="",
            phone=phone,
            turn_id="",
            draft=self.db.pending_draft(phone),
            customer=self.backend.find_customer_by_phone(phone),
            orders=self.backend.orders_for_phone(phone),
            audit=self.db.list_audit(phone, limit=12),
            escalations=self.db.list_escalations(phone),
            products=self.backend.list_products(),
            messages=self.db.history(phone, limit=40),
            llm=self.settings.llm_status(),
            limits={
                "max_order_value_cents": self.settings.max_order_value_cents,
                "currency": self.settings.currency,
            },
        )

    def reset(self) -> None:
        if not isinstance(self.backend, MockShopify):
            raise RuntimeError("Sandbox reset is only available while Shopify is mocked.")
        self.backend.seed(force=True)

    def resolve_escalation(self, escalation_id: str) -> dict | None:
        return self.db.resolve_escalation(escalation_id)

    def notify_order_change(self, topic: str, payload: dict) -> dict:
        """Shopify orders/updated and fulfillment webhooks. Template + email, never a session message."""
        event = normalize_shopify_webhook(topic, payload)
        order = self.db.find_order_for_shopify(
            number=event["number"],
            shopify_id=event["shopify_id"],
        )
        phone = event["phone"] or (order or {}).get("phone") or ""
        email = event["email"] or (order or {}).get("email") or ""
        if order and not email:
            customer = self.db.get_customer(order["customer_id"]) if order.get("customer_id") else None
            email = (customer or {}).get("email") or ""
        number = event["number"] or (order or {}).get("number") or ""
        if order:
            financial = event["financial_status"] or None
            fulfillment = event["fulfillment_status"] or None
            self.db.update_order_progress(
                order["id"],
                status=fulfillment,
                financial_status=financial,
            )
        normalized = ""
        if phone:
            try:
                normalized = normalize_phone(phone)
            except PhoneError:
                normalized = ""
        if normalized:
            self.whatsapp.send_template(
                normalized,
                self.settings.whatsapp_status_template,
                [number or "your order", event["status_label"], event["detail"]],
            )
        self.mailer.status_update(
            phone=normalized or phone,
            number=number or "your order",
            status_label=event["status_label"],
            detail=event["detail"],
            customer_email=email,
        )
        self.db.audit(
            normalized or phone or None,
            "order.status_notified",
            "success",
            {
                "topic": topic,
                "number": number,
                "status": event["status_label"],
                "email": email,
            },
        )
        return {
            "phone": normalized or phone,
            "number": number,
            "status": event["status_label"],
            "email": email,
        }


def build_service(settings: Settings) -> AgentService:
    db = Database(settings.database_path)
    if settings.shopify_is_mock:
        backend: MockShopify | LiveShopify = MockShopify(db)
        backend.seed()
    else:
        backend = LiveShopify(settings, db)
    rag = KnowledgeBase(settings, db)
    documents = rag.load_markdown(settings.knowledge_path())
    try:
        documents = documents + product_documents(backend.list_products())
    except Exception:
        logger.exception("product documents skipped")
    rag.ensure(documents)
    agent = HarborAgent(settings, db, backend, rag)
    whatsapp = WhatsAppClient(settings, db)
    n8n = N8NClient(settings, db)
    mailer = Mailer(settings, db)
    return AgentService(settings, db, backend, rag, agent, whatsapp, n8n, mailer)

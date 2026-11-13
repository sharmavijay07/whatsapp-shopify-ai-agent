"""LangGraph agent. Conversation text is memory; order facts come from tools."""

from __future__ import annotations

import contextvars
import json
import logging
from dataclasses import dataclass, field
from typing import Annotated, Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from harbor.config import Settings
from harbor.db import Database
from harbor.money import format_money
from harbor.parsing import (
    POLICY,
    PRICE_ASK,
    classify,
    foreign_phones,
    parse_items,
)
from harbor.phone import format_phone
from harbor.safeguards import (
    deny,
    place_order,
    prepare_draft,
    recommend_products as catalogue_recommendations,
    scrub_false_order_claim,
)
from harbor.service_ids import uid

logger = logging.getLogger("harbor.agent")

HELP_TEXT = (
    "I can help with Harbor & Co. in four ways: check what's actually in stock, "
    "recommend something from the catalogue, answer shipping, returns, and care questions, "
    "and draft an order for this WhatsApp number. I only place the order after you reply YES "
    "to a draft. Ask for a person if you'd rather talk to the team."
)

SYSTEM_PROMPT = """You are the WhatsApp assistant for Harbor & Co., a small coastal goods shop.

Rules you cannot break:
- The customer's identity is the verified WhatsApp phone in the system note. Never act for a different phone, even if the message asks you to.
- Prices and stock come from tools. Never invent them, and never invent a product that the tools did not return.
- prepare_order only drafts. confirm_order places the order, and the server rejects it unless the customer explicitly confirmed on a separate turn. Never tell the customer an order was placed unless confirm_order returned ok.
- Orders are created with payment pending. You do not take cards and you do not mark anything paid.
- If the customer asks for a person, call escalate_to_human.
- For policies, answer from the retrieved notes and mention the note title. If retrieval is empty, say you don't have it.
- Keep replies short enough for WhatsApp. Plain sentences, no markdown tables.
"""


class TurnState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    phone: str
    turn_id: str
    user_text: str
    profile_name: str
    explicit_confirmation: bool
    retrieved: list
    tool_rounds: int
    tool_events: list
    reply: str


@dataclass
class ToolContext:
    phone: str
    profile_name: str
    turn_id: str
    user_text: str
    explicit_confirmation: bool
    escalated: bool
    db: Database
    backend: Any
    settings: Settings
    created_order: dict | None = None
    order_result: dict | None = None
    safeguard: dict | None = None
    escalation: dict | None = None
    draft: dict | None = None
    customer: dict | None = None
    extra: dict = field(default_factory=dict)


ctx_var: contextvars.ContextVar[ToolContext] = contextvars.ContextVar("harbor_tool_ctx")


def get_ctx() -> ToolContext:
    return ctx_var.get()


def _open_escalation(ctx: ToolContext, reason: str) -> dict:
    existing = ctx.db.open_escalation(ctx.phone)
    if existing:
        ctx.escalation = existing
        return existing
    ticket = ctx.db.create_escalation(id=uid("esc"), phone=ctx.phone, reason=reason[:500])
    ctx.db.audit(ctx.phone, "escalation.opened", "success", {"id": ticket["id"]})
    ctx.escalation = ticket
    return ticket


def _remember(result: dict) -> dict:
    ctx = get_ctx()
    if not result.get("ok"):
        ctx.safeguard = {"code": result.get("code"), "message": result.get("message", "")}
    if result.get("escalate"):
        ticket = _open_escalation(ctx, result.get("message") or "Needs a person")
        result["escalation"] = ticket
    if result.get("escalation"):
        ctx.escalation = result["escalation"]
    if result.get("order"):
        ctx.created_order = result["order"]
        ctx.order_result = result
    if result.get("ok") and result.get("draft"):
        ctx.draft = result["draft"]
    if result.get("customer"):
        ctx.customer = result["customer"]
    return result


def tool_check_availability(_args: dict) -> dict:
    ctx = get_ctx()
    products = ctx.backend.list_products()
    matched = [item.product for item in parse_items(ctx.user_text, products)]
    if not matched:
        query_tokens = set(ctx.user_text.lower().split())
        scored = []
        for product in products:
            title = product["title"].lower()
            score = sum(1 for token in query_tokens if token in title)
            if score:
                scored.append((score, product))
        scored.sort(key=lambda item: -item[0])
        matched = [product for _, product in scored[:1]]
    if not matched:
        return deny("not_found", "I couldn't find that in the Shopify catalogue.")
    payload = []
    for product in matched:
        payload.append(
            {
                "title": product["title"],
                "description": product["description"],
                "variants": [
                    {
                        "title": variant["title"],
                        "sku": variant["sku"],
                        "price_cents": variant["price_cents"],
                        "inventory_qty": variant["inventory_qty"],
                    }
                    for variant in product["variants"]
                ],
            }
        )
    return {"ok": True, "code": "availability", "products": payload}


def tool_search_products(_args: dict) -> dict:
    ctx = get_ctx()
    products = ctx.backend.list_products()
    matched = [item.product for item in parse_items(ctx.user_text, products)]
    if not matched:
        return tool_check_availability(_args)
    payload = []
    for product in matched:
        payload.append(
            {
                "title": product["title"],
                "description": product["description"],
                "tags": product.get("tags") or [],
                "variants": [
                    {
                        "title": variant["title"],
                        "sku": variant["sku"],
                        "price_cents": variant["price_cents"],
                        "inventory_qty": variant["inventory_qty"],
                    }
                    for variant in product["variants"]
                ],
            }
        )
    return {"ok": True, "code": "products", "products": payload}


def tool_recommend(_args: dict) -> dict:
    return catalogue_recommendations(get_ctx().backend, get_ctx().user_text)


def tool_lookup(_args: dict) -> dict:
    ctx = get_ctx()
    if foreign_phones(ctx.user_text, ctx.phone):
        ctx.db.audit(ctx.phone, "order.lookup", "denied", {"code": "unauthorized"})
        return deny(
            "unauthorized",
            f"I can only look up orders for {format_phone(ctx.phone)}, the number this chat is on.",
        )
    from harbor.parsing import order_number_in

    number = order_number_in(ctx.user_text)
    if number:
        order = ctx.backend.get_order_by_number(number)
        if order is None:
            return {
                "ok": True,
                "code": "not_found",
                "orders": [],
                "message": f"I can't find order #{number} for {format_phone(ctx.phone)}.",
            }
        if order.get("phone") and order["phone"] != ctx.phone:
            ctx.db.audit(
                ctx.phone,
                "order.lookup",
                "denied",
                {"code": "unauthorized", "number": number},
            )
            return deny(
                "unauthorized",
                f"I can only look up orders for {format_phone(ctx.phone)}. I can't open an order tied to a different customer.",
            )
        return {"ok": True, "code": "orders", "orders": [order]}
    orders = ctx.backend.orders_for_phone(ctx.phone)
    return {"ok": True, "code": "orders", "orders": orders}


def tool_prepare(_args: dict) -> dict:
    ctx = get_ctx()
    if foreign_phones(ctx.user_text, ctx.phone):
        ctx.db.audit(ctx.phone, "order.denied", "denied", {"code": "unauthorized"})
        return deny(
            "unauthorized",
            f"I only draft orders for {format_phone(ctx.phone)}. I can't place one for a different number.",
        )
    return prepare_draft(
        db=ctx.db,
        backend=ctx.backend,
        phone=ctx.phone,
        text=ctx.user_text,
        profile_name=ctx.profile_name,
        turn_id=ctx.turn_id,
        escalated=ctx.escalated,
        max_order_value_cents=ctx.settings.max_order_value_cents,
        currency=ctx.settings.currency,
        ttl_minutes=ctx.settings.draft_ttl_minutes,
    )


def tool_confirm(_args: dict) -> dict:
    ctx = get_ctx()
    return place_order(
        db=ctx.db,
        backend=ctx.backend,
        phone=ctx.phone,
        text=ctx.user_text,
        turn_id=ctx.turn_id,
        explicit_confirmation=ctx.explicit_confirmation,
        escalated=ctx.escalated,
        max_order_value_cents=ctx.settings.max_order_value_cents,
        duplicate_window_minutes=ctx.settings.duplicate_window_minutes,
        currency=ctx.settings.currency,
    )


def tool_cancel(_args: dict) -> dict:
    ctx = get_ctx()
    draft = ctx.db.pending_draft(ctx.phone)
    if draft is None:
        return deny("no_draft", "There's no draft to cancel.")
    ctx.db.set_draft_status(draft["id"], "cancelled")
    ctx.db.audit(ctx.phone, "order.draft", "cancelled", {"draft_id": draft["id"]})
    return {"ok": True, "code": "cancelled", "message": "I cancelled that draft. Nothing was placed."}


def tool_escalate(_args: dict) -> dict:
    ctx = get_ctx()
    reason = ctx.user_text.strip()[:500] or "Customer asked for a person"
    ticket = _open_escalation(ctx, reason)
    return {
        "ok": True,
        "code": "escalated",
        "escalation": ticket,
        "message": "Handed to a specialist.",
    }


IMPLEMENTATIONS = {
    "check_availability": tool_check_availability,
    "search_products": tool_search_products,
    "recommend_products": tool_recommend,
    "lookup_orders": tool_lookup,
    "prepare_order": tool_prepare,
    "confirm_order": tool_confirm,
    "cancel_draft": tool_cancel,
    "escalate_to_human": tool_escalate,
}


def dispatch(name: str, args: dict | None) -> dict:
    fn = IMPLEMENTATIONS.get(name)
    if fn is None:
        return deny("unknown_tool", f"I don't have a tool named {name}.")
    result = fn(args or {})
    return _remember(result)


@tool
def check_availability(query: str) -> str:
    """Check live Shopify inventory for a product the customer asked about. Do not guess stock."""
    return json.dumps(dispatch("check_availability", {"query": query}))


@tool
def search_products(query: str) -> str:
    """Look up a catalogue product's description and price. Prices come from Shopify."""
    return json.dumps(dispatch("search_products", {"query": query}))


@tool
def recommend_products(interest: str) -> str:
    """Recommend in-stock products. Budget is read from the customer's message, not from you."""
    return json.dumps(dispatch("recommend_products", {"interest": interest}))


@tool
def lookup_orders(note: str = "") -> str:
    """Look up orders for the verified WhatsApp customer only."""
    return json.dumps(dispatch("lookup_orders", {"note": note}))


@tool
def prepare_order(request: str) -> str:
    """Draft an order from the customer's message. Does not place it."""
    return json.dumps(dispatch("prepare_order", {"request": request}))


@tool
def confirm_order(note: str = "") -> str:
    """Place the pending draft. Succeeds only after an explicit customer confirmation on a later turn."""
    return json.dumps(dispatch("confirm_order", {"note": note}))


@tool
def cancel_draft(note: str = "") -> str:
    """Cancel the pending draft without placing an order."""
    return json.dumps(dispatch("cancel_draft", {"note": note}))


@tool
def escalate_to_human(reason: str) -> str:
    """Hand the conversation to a human and pause automated orders."""
    return json.dumps(dispatch("escalate_to_human", {"reason": reason}))


LANGCHAIN_TOOLS = [
    check_availability,
    search_products,
    recommend_products,
    lookup_orders,
    prepare_order,
    confirm_order,
    cancel_draft,
    escalate_to_human,
]


def _variant_bits(product: dict) -> str:
    bits = []
    for variant in product["variants"]:
        label = ""
        if variant["title"] not in {"Default", "Default Title"}:
            label = f"size {variant['title']}: "
        bits.append(f"{label}{variant['inventory_qty']} in stock")
    price = format_money(product["variants"][0]["price_cents"])
    return f"{product['title']} is {price}. " + "; ".join(bits) + "."


def render_tool(name: str, result: dict, phone: str) -> str:
    if not result.get("ok"):
        return result.get("message") or "I couldn't do that."
    code = result.get("code")
    if code == "availability":
        lines = []
        for product in result["products"]:
            total = sum(variant["inventory_qty"] for variant in product["variants"])
            if total <= 0:
                price = format_money(product["variants"][0]["price_cents"])
                lines.append(f"{product['title']} is out of stock right now (0 available), listed at {price}.")
            else:
                lines.append(_variant_bits(product))
        return " ".join(lines)
    if code == "products":
        chunks = []
        for product in result["products"]:
            chunks.append(product["description"].strip() + " " + _variant_bits(product))
        return "\n\n".join(chunks)
    if code == "recommendations":
        lines = ["From what's in stock right now:"]
        for product in result["products"]:
            lines.append(
                f"• {product['title']} — {format_money(product['price_cents'])}. {product['reason']}"
            )
        if result.get("budget_cents"):
            lines.append(f"All of these are within {format_money(result['budget_cents'])}.")
        lines.append("Tell me which one you want if you'd like a draft.")
        return "\n".join(lines)
    if code == "orders":
        orders = result.get("orders") or []
        if not orders:
            return f"I don't have any orders for {format_phone(phone)} yet."
        lines = []
        for order in orders:
            item_text = ", ".join(
                f"{line['quantity']} × {line['title']}" for line in order.get("lines") or []
            )
            lines.append(
                f"Order #{order['number']} is {order.get('financial_status') or order.get('status')} "
                f"({format_money(order['total_cents'], order.get('currency') or 'USD')}"
                + (f": {item_text}" if item_text else "")
                + ")."
            )
        return " ".join(lines)
    if code == "not_found":
        return result.get("message") or "I can't find that order on this number."
    if code == "draft":
        draft = result["draft"]
        customer = result["customer"]
        lines = "\n".join(
            f"• {line['quantity']} × {line['title']} — {format_money(line['unit_price_cents'])} each"
            for line in draft["lines"]
        )
        return (
            f"Here's a draft for {customer['name']} on {format_phone(customer['phone'])}. "
            f"Nothing has been placed yet.\n\n{lines}\n\n"
            f"Total {format_money(draft['total_cents'], draft['currency'])}.\n\n"
            f"Reply YES to place this order. Confirmation token {draft['token']}."
        )
    if code in {"created", "duplicate", "duplicate_recent"}:
        if code != "created":
            return result.get("message") or "I did not create a second order."
        order = result["order"]
        lines = "\n".join(
            f"• {line['quantity']} × {line['title']}" for line in order.get("lines") or []
        )
        return (
            f"Order #{order['number']} is in the shop for {format_phone(order['phone'])}. "
            f"Payment status: {order.get('financial_status', 'pending')}. "
            f"This chat does not capture payment.\n\n{lines}\n\n"
            f"Total {format_money(order['total_cents'], order.get('currency') or 'USD')}. "
            f"A repeat of this confirmation will not create a second order."
        )
    if code == "cancelled":
        return result.get("message") or "Cancelled."
    if code == "escalated":
        ticket = result["escalation"]
        return (
            f"I've handed this to a Harbor & Co. specialist. Reference {ticket['id']}. "
            "Someone picks up messages between 9am and 6pm Pacific. "
            "I won't place orders on this number until they close the handoff, "
            "but I can still answer product and policy questions."
        )
    return result.get("message") or "Done."


def render_knowledge(user_text: str, hits: list[dict]) -> str:
    from harbor.parsing import GREETING, HELP

    stripped = user_text.strip()
    if HELP.search(stripped) or (GREETING.match(stripped) and len(stripped) < 48):
        return HELP_TEXT
    usable = [hit for hit in hits if hit.get("lexical", 0) >= 0.15 or hit.get("vector", 0) >= 0.32]
    if not usable:
        return (
            "I don't have that in the Harbor & Co. catalogue or the shop policies. "
            "I can check a product, draft an order for this number, or bring in a person."
        )
    top = usable[0]
    return f"{top['text'].strip()}\n\nSource: {top['title']}."


class HarborAgent:
    def __init__(self, settings: Settings, db: Database, backend, rag):
        self.settings = settings
        self.db = db
        self.backend = backend
        self.rag = rag
        self._chat = None
        graph = StateGraph(TurnState)
        graph.add_node("retrieve", self.retrieve)
        graph.add_node("plan", self.plan)
        graph.add_node("tools", self.run_tools)
        graph.add_edge(START, "retrieve")
        graph.add_edge("retrieve", "plan")
        graph.add_conditional_edges("plan", self.route, {"tools": "tools", "end": END})
        graph.add_edge("tools", "plan")
        self.graph = graph.compile()

    def retrieve(self, state: TurnState) -> dict:
        hits = self.rag.search(state.get("user_text") or "", k=4)
        return {"retrieved": hits}

    def route(self, state: TurnState) -> str:
        messages = state.get("messages") or []
        if not messages:
            return "end"
        last = messages[-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        return "end"

    def mock_calls(self, state: TurnState) -> list[dict]:
        phone = state.get("phone") or ""
        text = state.get("user_text") or ""
        draft = self.db.pending_draft(phone)
        intent = classify(
            text,
            has_pending=draft is not None,
            token=draft["token"] if draft else None,
        )
        if (
            intent == "faq"
            and parse_items(text, self.backend.list_products())
            and PRICE_ASK.search(text)
            and not POLICY.search(text)
        ):
            intent = "product"
        names = {
            "confirm": "confirm_order",
            "cancel": "cancel_draft",
            "escalate": "escalate_to_human",
            "status": "lookup_orders",
            "recommend": "recommend_products",
            "availability": "check_availability",
            "order": "prepare_order",
            "product": "search_products",
        }
        name = names.get(intent)
        if not name:
            return []
        return [{"name": name, "args": {}, "id": f"call_{name}", "type": "tool_call"}]

    def compose(self, state: TurnState) -> str:
        events = state.get("tool_events") or []
        if events:
            last = events[-1]
            return render_tool(last["name"], last["result"], state.get("phone") or "")
        return render_knowledge(state.get("user_text") or "", state.get("retrieved") or [])

    def _llm(self):
        if self._chat is None:
            from langchain_openai import ChatOpenAI

            base_url = self.settings.resolved_base_url() or None
            self._chat = ChatOpenAI(
                model=self.settings.resolved_model(),
                api_key=self.settings.resolved_api_key(),
                base_url=base_url,
                temperature=self.settings.llm_temperature,
                max_completion_tokens=self.settings.llm_max_tokens,
            ).bind_tools(LANGCHAIN_TOOLS)
        return self._chat

    def _prompt(self, state: TurnState) -> list:
        phone = state.get("phone") or ""
        draft = self.db.pending_draft(phone)
        retrieved = state.get("retrieved") or []
        notes = "\n\n".join(
            f"[{hit['title']}] {hit['text'][:500]}" for hit in retrieved[:3]
        ) or "(nothing retrieved)"
        draft_note = "none"
        if draft:
            draft_note = (
                f"id {draft['id']} token {draft['token']} total "
                f"{format_money(draft['total_cents'], draft['currency'])}"
            )
        system = (
            SYSTEM_PROMPT
            + f"\nVerified WhatsApp phone: {phone}\n"
            + f"Customer name on the channel: {state.get('profile_name') or '(unknown)'}\n"
            + f"Pending draft: {draft_note}\n"
            + f"Explicit confirmation detected on this message: {bool(state.get('explicit_confirmation'))}\n"
            + f"Retrieved notes:\n{notes}"
        )
        return [SystemMessage(content=system), *state.get("messages", [])]

    def plan(self, state: TurnState) -> dict:
        rounds = int(state.get("tool_rounds") or 0)
        if rounds >= 3 or (self.settings.use_mock_llm and rounds >= 1):
            reply = self.compose(state)
            return {"messages": [AIMessage(content=reply)], "reply": reply}
        if self.settings.use_mock_llm:
            calls = self.mock_calls(state)
            if not calls:
                reply = self.compose(state)
                return {"messages": [AIMessage(content=reply)], "reply": reply}
            return {"messages": [AIMessage(content="", tool_calls=calls)]}
        try:
            response = self._llm().invoke(self._prompt(state))
        except Exception as exc:  # noqa: BLE001 — surface a safe customer message
            logger.exception("llm call failed")
            self.db.audit(
                state.get("phone"),
                "llm.error",
                "error",
                {"error": str(exc)[:500], "provider": self.settings.llm_provider},
            )
            if "429" in str(exc):
                reply = (
                    "The language model is rate-limited right now, so I left every order untouched. "
                    "Groq's free tier caps requests and tokens. Try again in a minute, or ask for a person."
                )
            else:
                reply = (
                    "The language model didn't answer, so I left every order untouched. "
                    "Ask again in a moment, or ask for a person."
                )
            return {"messages": [AIMessage(content=reply)], "reply": reply}
        if getattr(response, "tool_calls", None):
            return {"messages": [response]}
        content = response.content
        if not isinstance(content, str):
            content = json.dumps(content)
        ctx = ctx_var.get()
        created = bool(ctx.created_order and not ctx.created_order.get("duplicate"))
        reply = scrub_false_order_claim(content.strip() or self.compose(state), created)
        return {"messages": [AIMessage(content=reply)], "reply": reply}

    def run_tools(self, state: TurnState) -> dict:
        last = state["messages"][-1]
        events = []
        messages = []
        for call in last.tool_calls:
            name = call["name"]
            result = dispatch(name, call.get("args") or {})
            events.append({"name": name, "args": call.get("args") or {}, "result": result})
            messages.append(
                ToolMessage(content=json.dumps(result, default=str), tool_call_id=call["id"])
            )
        return {
            "messages": messages,
            "tool_rounds": int(state.get("tool_rounds") or 0) + 1,
            "tool_events": list(state.get("tool_events") or []) + events,
        }

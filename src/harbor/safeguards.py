"""Order gates. The language model cannot skip these.

A draft is not an order. Placement requires an explicit customer confirmation
on a later turn, a phone that matches the WhatsApp sender, a fresh stock and
price check, a value cap, and an idempotency key.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from harbor.db import Database, StoreError, canonical_lines
from harbor.money import format_money
from harbor.parsing import (
    allows_duplicate_override,
    budget_cents,
    email_in,
    parse_items,
    wrong_token,
)
from harbor.phone import format_phone
from harbor.service_ids import confirmation_token, uid

MAX_QTY = 99


def deny(code: str, message: str, **extra) -> dict:
    return {"ok": False, "code": code, "message": message, **extra}


def idempotency_key(phone: str, draft_id: str) -> str:
    digest = hashlib.sha256(f"{phone}|{draft_id}".encode()).hexdigest()[:24]
    return f"ord_{digest}"


def scrub_false_order_claim(reply: str, order_created: bool) -> str:
    """If the model says it placed an order and the tools did not, correct it."""
    if order_created or not reply:
        return reply
    if re.search(
        r"\b(i(?:'ve| have) placed|order (?:is|has been) placed|successfully placed|"
        r"i placed your order|your order is confirmed)\b",
        reply,
        re.I,
    ):
        return (
            "I have not placed an order. An order is created only after you confirm "
            "a draft, and that did not happen just now.\n\n" + reply
        )
    return reply


def _line_title(product: dict, variant: dict) -> str:
    if variant["title"] in {"Default", "Default Title"}:
        return product["title"]
    return f"{product['title']} ({variant['title']})"


def prepare_draft(
    *,
    db: Database,
    backend,
    phone: str,
    text: str,
    profile_name: str,
    turn_id: str,
    escalated: bool,
    max_order_value_cents: int,
    currency: str,
    ttl_minutes: int,
) -> dict:
    if escalated:
        return deny(
            "order_paused",
            "A specialist has this chat. I won't place or draft an order until they close the handoff.",
        )
    products = backend.list_products()
    requests = parse_items(text, products)
    if not requests:
        return deny(
            "no_items",
            "Tell me which piece you want and how many. I can draft it, and I won't place it until you confirm.",
        )
    if any(item.needs_size for item in requests):
        missing = next(item for item in requests if item.needs_size)
        options = ", ".join(missing.options)
        return deny(
            "needs_size",
            f"{missing.product['title']} comes in {options}. Tell me which size and I'll draft it. I won't guess.",
            product=missing.product["title"],
            options=missing.options,
        )
    if any(item.quantity > MAX_QTY for item in requests):
        return deny("quantity_limit", f"I can draft at most {MAX_QTY} of any one item.")
    if any(item.quantity < 1 for item in requests):
        return deny("quantity_limit", "Quantity has to be at least 1.")

    lines = []
    for item in requests:
        variant = item.variant
        assert variant is not None
        if variant["inventory_qty"] < item.quantity:
            available = variant["inventory_qty"]
            db.audit(
                phone,
                "order.denied",
                "denied",
                {"code": "insufficient_stock", "variant_id": variant["id"], "available": available},
            )
            return deny(
                "insufficient_stock",
                (
                    f"I can't draft {item.quantity} × {_line_title(item.product, variant)}. "
                    f"Only {available} are available."
                ),
                available=available,
            )
        lines.append(
            {
                "variant_id": variant["id"],
                "product_id": item.product["id"],
                "title": _line_title(item.product, variant),
                "quantity": item.quantity,
                "unit_price_cents": int(variant["price_cents"]),
            }
        )
    total = sum(line["quantity"] * line["unit_price_cents"] for line in lines)
    if total > max_order_value_cents:
        db.audit(phone, "order.denied", "denied", {"code": "over_limit", "total_cents": total})
        return deny(
            "over_limit",
            (
                f"That draft would be {format_money(total, currency)}, above the "
                f"{format_money(max_order_value_cents, currency)} limit I can place automatically. "
                "I have not created it. A person needs to take this one."
            ),
            total_cents=total,
            escalate=True,
        )

    customer = backend.find_customer_by_phone(phone)
    created_customer = False
    if customer is None:
        name = (profile_name or "").strip()
        if not name:
            return deny(
                "needs_name",
                "I can draft this for the WhatsApp number you're messaging from, but I need the name to put on the customer record.",
            )
        customer = backend.create_customer(name=name, phone=phone, email=email_in(text))
        created_customer = True
    draft = db.create_draft(
        id=uid("draft"),
        phone=phone,
        token=confirmation_token(),
        customer_id=customer["id"],
        lines=lines,
        total_cents=total,
        currency=currency,
        created_turn_id=turn_id,
        ttl_minutes=ttl_minutes,
    )
    db.audit(
        phone,
        "order.draft",
        "success",
        {"draft_id": draft["id"], "total_cents": total, "token": draft["token"]},
    )
    if created_customer:
        db.audit(
            phone,
            "customer.created",
            "success",
            {"customer_id": customer["id"], "phone": phone},
        )
    return {"ok": True, "code": "draft", "draft": draft, "customer": customer}


def place_order(
    *,
    db: Database,
    backend,
    phone: str,
    text: str,
    turn_id: str,
    explicit_confirmation: bool,
    escalated: bool,
    max_order_value_cents: int,
    duplicate_window_minutes: int,
    currency: str,
) -> dict:
    if not explicit_confirmation:
        return deny(
            "confirmation_required",
            "I have not placed an order. Reply YES once you have seen the draft.",
        )
    if escalated:
        return deny(
            "order_paused",
            "A specialist has this chat, so I will not place an order.",
        )
    draft = db.pending_draft(phone)
    if draft is None:
        recent = _recent_order(db, phone, duplicate_window_minutes)
        if recent is not None:
            recent["duplicate"] = True
            db.audit(phone, "order.duplicate", "blocked", {"order_id": recent["id"]})
            return {
                "ok": True,
                "code": "duplicate",
                "order": recent,
                "message": f"Order #{recent['number']} is already in the shop. I did not create another.",
            }
        return deny(
            "no_draft",
            "There's no draft waiting. Tell me what you'd like and I'll prepare one before anything is placed.",
        )
    if draft["created_turn_id"] == turn_id:
        return deny(
            "same_turn",
            "I drafted that just now. I need a separate reply of YES before I place it.",
        )
    if wrong_token(text, draft["token"]):
        db.audit(phone, "order.denied", "denied", {"code": "wrong_token", "draft_id": draft["id"]})
        return deny(
            "wrong_token",
            f"That confirmation token doesn't match the draft. The token is {draft['token']}.",
        )
    customer = backend.find_customer_by_phone(phone)
    if customer is None or customer["id"] != draft["customer_id"] or customer["phone"] != phone:
        db.audit(phone, "order.denied", "denied", {"code": "unauthorized", "draft_id": draft["id"]})
        return deny(
            "unauthorized",
            f"I can only place an order for {format_phone(phone)}, the number this chat is on.",
        )

    resolved_lines = []
    for line in draft["lines"]:
        variant = backend.get_variant(line["variant_id"])
        if variant is None:
            return deny("unavailable", "One of those items is no longer in the catalogue. I didn't place the order.")
        if int(variant["price_cents"]) != int(line["unit_price_cents"]):
            db.set_draft_status(draft["id"], "rejected")
            return deny(
                "price_changed",
                "The price changed after the draft. I didn't place the order. Ask me to draft it again.",
            )
        if int(variant["inventory_qty"]) < int(line["quantity"]):
            return deny(
                "insufficient_stock",
                f"Only {variant['inventory_qty']} of {line['title']} are left, so I didn't place the order.",
            )
        resolved_lines.append(line)
    total = sum(int(line["quantity"]) * int(line["unit_price_cents"]) for line in resolved_lines)
    if total != int(draft["total_cents"]) or total > max_order_value_cents:
        db.set_draft_status(draft["id"], "rejected")
        db.audit(phone, "order.denied", "denied", {"code": "over_limit", "total_cents": total})
        return deny(
            "over_limit",
            (
                f"That order would be {format_money(total, currency)}, above the "
                f"{format_money(max_order_value_cents, currency)} automatic limit. Nothing was placed."
            ),
            escalate=True,
            total_cents=total,
        )

    if not allows_duplicate_override(text):
        prior = db.recent_matching_order(
            phone, canonical_lines(resolved_lines), duplicate_window_minutes
        )
        if prior is not None:
            db.audit(
                phone,
                "order.duplicate",
                "blocked",
                {"order_id": prior["id"], "draft_id": draft["id"]},
            )
            return {
                "ok": True,
                "code": "duplicate_recent",
                "order": prior,
                "message": (
                    f"This matches order #{prior['number']}, placed a few minutes ago. "
                    "I did not create a second one. Say 'place another order anyway' if you really want a separate order."
                ),
            }

    key = idempotency_key(phone, draft["id"])
    existing = db.get_idempotency(key)
    if existing:
        existing["duplicate"] = True
        db.set_draft_status(draft["id"], "confirmed")
        db.audit(phone, "order.duplicate", "blocked", {"order_id": existing["id"], "key": key})
        return {
            "ok": True,
            "code": "duplicate",
            "order": existing,
            "message": f"Order #{existing['number']} was already placed for this confirmation. I did not create another.",
        }
    try:
        order = backend.create_order(
            id=uid("ord"),
            customer_id=customer["id"],
            phone=phone,
            lines=resolved_lines,
            currency=currency,
            idempotency_key=key,
            confirmation_token=draft["token"],
        )
    except StoreError as exc:
        db.audit(phone, "order.denied", "error", {"code": exc.code, "message": exc.message})
        return deny(exc.code, exc.message + " Nothing else was changed.")
    db.set_draft_status(draft["id"], "confirmed")
    db.audit(
        phone,
        "order.created",
        "success",
        {
            "order_id": order["id"],
            "number": order["number"],
            "idempotency_key": key,
            "total_cents": order["total_cents"],
            "phone": phone,
        },
    )
    order["duplicate"] = bool(order.get("duplicate"))
    return {"ok": True, "code": "created", "order": order, "customer": customer, "draft": draft}


def _recent_order(db: Database, phone: str, window_minutes: int) -> dict | None:
    orders = db.orders_for_phone(phone)
    if not orders:
        return None
    created = datetime.fromisoformat(orders[0]["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - created <= timedelta(minutes=window_minutes):
        return orders[0]
    return None


def recommend_products(backend, text: str) -> dict:
    budget = budget_cents(text)
    from harbor.rag import tokenize

    interest = set(tokenize(text))
    scored = []
    for product in backend.list_products():
        available = [variant for variant in product["variants"] if variant["inventory_qty"] > 0]
        if not available:
            continue
        cheapest = min(available, key=lambda variant: variant["price_cents"])
        if budget is not None and cheapest["price_cents"] > budget:
            continue
        blob = set(
            tokenize(
                product["title"]
                + " "
                + product["description"]
                + " "
                + " ".join(product.get("tags") or [])
            )
        )
        overlap = len(interest & blob)
        scored.append((overlap, cheapest["price_cents"], product, cheapest))
    if not scored:
        if budget is not None:
            return deny(
                "no_match",
                f"Nothing in stock is {format_money(budget)} or under right now.",
            )
        return deny("no_match", "Nothing is in stock to recommend right now.")
    if all(item[0] == 0 for item in scored):
        scored.sort(key=lambda item: item[1])
    else:
        scored.sort(key=lambda item: (-item[0], item[1]))
    picks = []
    for _overlap, _price, product, variant in scored[:3]:
        picks.append(
            {
                "title": product["title"],
                "variant_id": variant["id"],
                "price_cents": variant["price_cents"],
                "inventory_qty": variant["inventory_qty"],
                "description": product["description"],
                "reason": product["description"].split(".")[0] + ".",
            }
        )
    return {"ok": True, "code": "recommendations", "products": picks, "budget_cents": budget}

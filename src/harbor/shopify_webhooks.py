"""Shopify webhook signatures and the order/fulfillment payloads we notify on."""

from __future__ import annotations

import base64
import hashlib
import hmac


def verify_shopify_hmac(body: bytes, header: str | None, secret: str) -> bool:
    """Shopify signs the raw body with the app API secret. The header is base64."""
    if not header or not secret:
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, header.strip())


def normalize_shopify_webhook(topic: str, payload: dict | None) -> dict:
    """Pull the fields a status notification needs out of a REST webhook body."""
    topic = (topic or "").strip().lower()
    payload = payload or {}
    if topic.startswith("fulfillment"):
        return _fulfillment(payload)
    return _order(payload)


def _order(payload: dict) -> dict:
    customer = payload.get("customer") or {}
    financial = str(payload.get("financial_status") or "pending").replace("_", " ")
    fulfillment = payload.get("fulfillment_status") or "unfulfilled"
    fulfillment_label = str(fulfillment).replace("_", " ")
    tracking_numbers: list[str] = []
    for row in payload.get("fulfillments") or []:
        number = (row or {}).get("tracking_number") or ""
        if number and number not in tracking_numbers:
            tracking_numbers.append(str(number))
    if tracking_numbers:
        detail = "Tracking: " + ", ".join(tracking_numbers) + "."
    else:
        detail = f"Payment is {financial}."
    number = str(payload.get("name") or payload.get("order_number") or "").lstrip("#")
    return {
        "number": number,
        "shopify_id": str(payload.get("id") or ""),
        "phone": payload.get("phone") or customer.get("phone") or "",
        "email": payload.get("email") or customer.get("email") or "",
        "status_label": fulfillment_label,
        "financial_status": financial,
        "fulfillment_status": fulfillment_label,
        "detail": detail,
    }


def _fulfillment(payload: dict) -> dict:
    tracking = str(payload.get("tracking_number") or "").strip()
    company = str(payload.get("tracking_company") or "").strip()
    url = str(payload.get("tracking_url") or "").strip()
    bits = [part for part in (company, tracking) if part]
    if url:
        bits.append(url)
    if bits:
        detail = "Tracking: " + " ".join(bits) + "."
    else:
        detail = "A fulfillment update was recorded."
    destination = payload.get("destination") or {}
    status = str(payload.get("shipment_status") or payload.get("status") or "updated").replace("_", " ")
    return {
        "number": str(payload.get("name") or "").lstrip("#"),
        "shopify_id": str(payload.get("order_id") or payload.get("id") or ""),
        "phone": destination.get("phone") or payload.get("phone") or "",
        "email": payload.get("email") or "",
        "status_label": status,
        "financial_status": "",
        "fulfillment_status": status,
        "detail": detail,
    }

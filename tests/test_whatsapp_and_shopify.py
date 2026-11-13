import hashlib
import hmac
import json

import httpx
from fastapi.testclient import TestClient

from harbor.api import create_app
from harbor.config import Settings
from harbor.db import Database
from harbor.shopify_live import LiveShopify
from harbor.whatsapp import extract_messages, verify_signature


def _payload(message_id: str, phone: str, text: str, name: str = "Avery Chen") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": phone, "profile": {"name": name}}],
                            "messages": [
                                {
                                    "from": phone,
                                    "id": message_id,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


def test_signature_and_parser():
    body = b'{"ok":true}'
    secret = "app-secret"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, f"sha256={digest}", secret)
    assert not verify_signature(body, "sha256=nope", secret)
    messages = extract_messages(_payload("wamid.1", "15035550142", "Hello"))
    assert messages[0]["text"] == "Hello"
    assert messages[0]["name"] == "Avery Chen"


def test_webhook_dedupes_and_verifies(tmp_path):
    settings = Settings(
        app_mode="mock",
        llm_provider="mock",
        database_path=str(tmp_path / "harbor.db"),
        qdrant_path=str(tmp_path / "qdrant"),
        whatsapp_verify_token="harbor-verify",
        whatsapp_app_secret="app-secret",
    )
    app = create_app(settings)
    client = TestClient(app)
    challenge = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "harbor-verify", "hub.challenge": "99"},
    )
    assert challenge.status_code == 200
    assert challenge.text == "99"
    refused = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "99"},
    )
    assert refused.status_code == 403

    body = _payload("wamid.draft", "15035550142", "I'd like a sea salt candle")
    raw = json.dumps(body).encode()
    signature = hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    first = client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={"content-type": "application/json", "x-hub-signature-256": f"sha256={signature}"},
    )
    assert first.status_code == 200
    assert first.json()["handled"] == 1
    bad = client.post("/webhooks/whatsapp", content=raw, headers={"content-type": "application/json"})
    assert bad.status_code == 403
    again = client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={"content-type": "application/json", "x-hub-signature-256": f"sha256={signature}"},
    )
    assert again.json()["handled"] == 0
    assert len(app.state.service.db.orders_for_phone("+15035550142")) == 0
    app.state.service.close()


def test_live_shopify_order_uses_idempotency_and_inventory_behaviour(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(
            {
                "url": str(request.url),
                "body": body,
                "key": request.headers.get("Idempotency-Key"),
            }
        )
        query = body["query"]
        if "customers(" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "customers": {
                            "nodes": [
                                {
                                    "id": "gid://shopify/Customer/9",
                                    "displayName": "Avery Chen",
                                    "phone": "+15035550142",
                                    "email": "",
                                }
                            ]
                        }
                    }
                },
            )
        assert body["variables"]["options"]["inventoryBehaviour"] == "DECREMENT_OBEYING_POLICY"
        assert body["variables"]["order"]["financialStatus"] == "PENDING"
        assert body["variables"]["options"]["sendReceipt"] is False
        return httpx.Response(
            200,
            json={
                "data": {
                    "orderCreate": {
                        "userErrors": [],
                        "order": {
                            "id": "gid://shopify/Order/55",
                            "name": "#2001",
                            "displayFinancialStatus": "PENDING",
                            "totalPriceSet": {"shopMoney": {"amount": "24.00", "currencyCode": "USD"}},
                        },
                    }
                }
            },
        )

    settings = Settings(
        app_mode="live",
        shopify_store_domain="harbor-dev.myshopify.com",
        shopify_admin_token="shpat_test",
        shopify_api_version="2026-10",
        database_path=str(tmp_path / "harbor.db"),
    )
    db = Database(settings.database_path)
    db.create_customer(id="cus_test", name="Avery Chen", phone="+15035550142", email="")
    live = LiveShopify(settings, db, client=httpx.Client(transport=httpx.MockTransport(handler)))
    order = live.create_order(
        id="ord_test",
        customer_id="cus_test",
        phone="+15035550142",
        lines=[
            {
                "variant_id": "gid://shopify/ProductVariant/1",
                "title": "Driftwood Ceramic Mug",
                "quantity": 1,
                "unit_price_cents": 2400,
            }
        ],
        currency="USD",
        idempotency_key="ord_abc123",
        confirmation_token="K7MQ",
    )
    assert order["number"] == "2001"
    assert order["shopify_id"] == "gid://shopify/Order/55"
    assert order["financial_status"] == "pending"
    mutation = next(item for item in calls if "orderCreate" in item["body"]["query"])
    assert mutation["key"] == "ord_abc123"
    assert calls[0]["url"].endswith("/admin/api/2026-10/graphql.json")
    assert "harbor-dev.myshopify.com" in calls[0]["url"]
    again = live.create_order(
        id="ord_test_2",
        customer_id="cus_test",
        phone="+15035550142",
        lines=order["lines"],
        currency="USD",
        idempotency_key="ord_abc123",
        confirmation_token="K7MQ",
    )
    assert again["duplicate"] is True
    assert again["number"] == "2001"
    assert sum(1 for item in calls if "orderCreate" in item["body"]["query"]) == 1
    live.close()
    db.close()

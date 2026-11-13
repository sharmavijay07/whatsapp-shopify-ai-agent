"""Live Shopify, WhatsApp, Gmail, webhooks, and hosted embeddings. Network is mocked."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from email.message import EmailMessage
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from harbor.api import create_app
from harbor.catalog import load_catalog
from harbor.config import Settings
from harbor.db import Database
from harbor.mailer import Mailer
from harbor.rag import KnowledgeBase, qdrant_client_kwargs
from harbor.shopify_live import LiveShopify, shopify_search_query
from harbor.shopify_seed import (
    build_product_set_input,
    public_webhook_base,
    register_webhooks,
    run as seed_run,
    seed_products,
)
from harbor.shopify_webhooks import normalize_shopify_webhook, verify_shopify_hmac
from harbor.whatsapp import WhatsAppClient, extract_messages

ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "app_mode": "mock",
        "llm_provider": "mock",
        "database_path": str(tmp_path / "harbor.db"),
        "vector_store": "memory",
        "embedding_provider": "bm25",
        "qdrant_path": str(tmp_path / "qdrant"),
        "knowledge_dir": str(ROOT / "src" / "harbor" / "knowledge"),
    }
    values.update(overrides)
    return Settings(**values)


def _shopify_sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class _FakeSMTP:
    sent: list[EmailMessage] = []
    login_password = ""

    def __init__(self, host, port, context=None, timeout=None):
        assert host == "smtp.gmail.com"
        assert port == 465

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, user, password):
        assert user == "don@example.com"
        _FakeSMTP.login_password = password

    def send_message(self, message):
        _FakeSMTP.sent.append(message)


def test_qdrant_cloud_client_sends_the_api_key():
    settings = Settings(
        qdrant_url="https://xyz.cloud.qdrant.io:6333",
        qdrant_api_key="qd_test",
    )
    kwargs = qdrant_client_kwargs(settings)
    assert kwargs["url"] == "https://xyz.cloud.qdrant.io:6333"
    assert kwargs["api_key"] == "qd_test"
    assert kwargs["prefer_grpc"] is False


def test_search_query_keeps_product_words():
    assert "linen" in shopify_search_query("Is the coastal linen shirt in stock?")
    assert shopify_search_query("hi").startswith("status:active")


def test_whatsapp_buttons_list_template_and_typed_yes(tmp_path):
    settings = _settings(
        tmp_path,
        app_mode="live",
        whatsapp_token="wa-token",
        whatsapp_phone_number_id="555",
        whatsapp_api_version="v21.0",
    )
    db = Database(settings.database_path)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    client = WhatsAppClient(settings, db, client=httpx.Client(transport=httpx.MockTransport(handler)))
    buttons = client.send_reply_buttons("+15035550142", "Reply YES to place this order.")
    listing = client.send_product_list(
        "+15035550142",
        "From what's in stock right now:",
        [
            {"id": "pick:Driftwood Ceramic Mug", "title": "Driftwood Ceramic Mug", "description": "$24.00"},
            {"id": "pick:Sea Salt Candle", "title": "Sea Salt Candle", "description": "$32.00"},
        ],
    )
    template = client.send_template(
        "+15035550142",
        "harbor_order_status",
        ["10421", "fulfilled", "Tracking: 1Z999."],
    )
    client.close()
    db.close()

    assert buttons["messages"][0]["id"] == "wamid.out"
    button_ids = [
        button["reply"]["id"] for button in calls[0]["interactive"]["action"]["buttons"]
    ]
    assert button_ids == ["confirm_yes", "confirm_no"]
    assert calls[0]["interactive"]["action"]["buttons"][0]["reply"]["title"] == "YES"
    rows = listing["messages"] and calls[1]["interactive"]["action"]["sections"][0]["rows"]
    assert rows[0]["id"] == "pick:Driftwood Ceramic Mug"
    assert calls[2]["type"] == "template"
    assert calls[2]["template"]["name"] == "harbor_order_status"
    assert calls[2]["template"]["components"][0]["parameters"][0]["text"] == "10421"
    assert template["messages"][0]["id"] == "wamid.out"

    button_yes = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "15035550142", "profile": {"name": "Avery Chen"}}],
                            "messages": [
                                {
                                    "from": "15035550142",
                                    "id": "wamid.btn",
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {"id": "confirm_yes", "title": "YES"},
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    parsed = extract_messages(button_yes)
    assert parsed[0]["text"] == "YES"
    assert parsed[0]["unsupported"] is False
    listed = extract_messages(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "15035550142",
                                        "id": "wamid.list",
                                        "type": "interactive",
                                        "interactive": {
                                            "type": "list_reply",
                                            "list_reply": {
                                                "id": "pick:Sea Salt Candle",
                                                "title": "Sea Salt Candle",
                                            },
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    )
    assert listed[0]["text"] == "I'd like Sea Salt Candle"
    typed = extract_messages(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "15035550142",
                                        "id": "wamid.typed",
                                        "type": "text",
                                        "text": {"body": "YES"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    )
    assert typed[0]["text"] == "YES"


def test_webhook_buttons_confirm_and_list_offers_products(tmp_path):
    settings = _settings(tmp_path, whatsapp_app_secret="app-secret", whatsapp_verify_token="harbor-verify")
    app = create_app(settings)
    client = TestClient(app)

    def post(message_id: str, message: dict):
        body = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "contacts": [{"wa_id": "15035550142", "profile": {"name": "Avery Chen"}}],
                                "messages": [message | {"from": "15035550142", "id": message_id}],
                            }
                        }
                    ]
                }
            ]
        }
        raw = json.dumps(body).encode()
        signature = hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
        response = client.post(
            "/webhooks/whatsapp",
            content=raw,
            headers={"content-type": "application/json", "x-hub-signature-256": f"sha256={signature}"},
        )
        assert response.status_code == 200
        return response

    post("wamid.draft", {"type": "text", "text": {"body": "I'd like a sea salt candle"}})
    audits = app.state.service.db.list_audit("+15035550142", 20)
    button_audit = next(row for row in audits if row["detail"].get("type") == "interactive")
    assert button_audit["detail"]["payload"]["interactive"]["type"] == "button"
    assert app.state.service.db.orders_for_phone("+15035550142") == []

    post(
        "wamid.yes",
        {
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": "confirm_yes", "title": "YES"},
            },
        },
    )
    orders = app.state.service.db.orders_for_phone("+15035550142")
    assert len(orders) == 1
    assert orders[0]["financial_status"] == "pending"
    assert any(row["action"] == "n8n.skipped" for row in app.state.service.db.list_audit("+15035550142", 30))

    post("wamid.gift", {"type": "text", "text": {"body": "Recommend a gift under $50"}})
    gift_audits = app.state.service.db.list_audit("+15035550142", 10)
    listed = next(
        row
        for row in gift_audits
        if (row["detail"].get("payload") or {}).get("interactive", {}).get("type") == "list"
    )
    titles = [
        row["title"]
        for row in listed["detail"]["payload"]["interactive"]["action"]["sections"][0]["rows"]
    ]
    assert "Driftwood Ceramic Mug" in titles
    assert "Harbor Canvas Tote" not in titles
    app.state.service.close()


def test_live_draft_is_completed_and_invoiced(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body)
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
                                    "email": "avery@example.com",
                                }
                            ]
                        }
                    }
                },
            )
        if "draftOrderCreate" in query:
            draft_input = body["variables"]["input"]
            assert draft_input["purchasingEntity"]["customerId"] == "gid://shopify/Customer/9"
            assert draft_input["lineItems"][0]["variantId"] == "gid://shopify/ProductVariant/1"
            assert draft_input["email"] == "avery@example.com"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "draftOrderCreate": {
                            "userErrors": [],
                            "draftOrder": {"id": "gid://shopify/DraftOrder/7", "name": "#D7", "invoiceUrl": ""},
                        }
                    }
                },
            )
        if "draftOrderComplete" in query:
            assert "paymentPending: true" in query
            assert body["variables"]["id"] == "gid://shopify/DraftOrder/7"
            assert request.headers["Idempotency-Key"] == "ord_abc"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "draftOrderComplete": {
                            "userErrors": [],
                            "draftOrder": {
                                "id": "gid://shopify/DraftOrder/7",
                                "order": {
                                    "id": "gid://shopify/Order/55",
                                    "name": "#2001",
                                    "displayFinancialStatus": "PENDING",
                                    "totalPriceSet": {"shopMoney": {"amount": "24.00", "currencyCode": "USD"}},
                                },
                            },
                        }
                    }
                },
            )
        if "orderInvoiceSend" in query:
            assert body["variables"]["email"]["to"] == "avery@example.com"
            return httpx.Response(
                200,
                json={"data": {"orderInvoiceSend": {"userErrors": [], "order": {"id": "gid://shopify/Order/55"}}}},
            )
        if "productVariant(" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "productVariant": {
                            "id": "gid://shopify/ProductVariant/1",
                            "title": "Default",
                            "sku": "HOM-014",
                            "price": "24.00",
                            "inventoryQuantity": 12,
                            "product": {"id": "gid://shopify/Product/1", "title": "Driftwood Ceramic Mug"},
                        }
                    }
                },
            )
        if "products(" in query:
            assert "linen" in body["variables"]["query"]
            return httpx.Response(200, json={"data": {"products": {"nodes": []}}})
        raise AssertionError(query)

    settings = _settings(
        tmp_path,
        app_mode="live",
        shopify_store_domain="harbor-dev.myshopify.com",
        shopify_admin_token="shpat_test",
        shopify_api_version="2026-10",
    )
    db = Database(settings.database_path)
    db.create_customer(id="cus_test", name="Avery Chen", phone="+15035550142", email="avery@example.com")
    live = LiveShopify(settings, db, client=httpx.Client(transport=httpx.MockTransport(handler)))
    opened = live.open_draft_order(
        customer={"id": "cus_test", "phone": "+15035550142", "email": "avery@example.com", "name": "Avery Chen"},
        lines=[{"variant_id": "gid://shopify/ProductVariant/1", "quantity": 1, "title": "Mug", "unit_price_cents": 2400}],
        email="avery@example.com",
    )
    assert opened["id"] == "gid://shopify/DraftOrder/7"
    order = live.complete_draft_order(
        shopify_draft_id=opened["id"],
        email="avery@example.com",
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
        idempotency_key="ord_abc",
        confirmation_token="K7MQ",
    )
    assert order["number"] == "2001"
    assert order["financial_status"] == "pending"
    assert order["invoice_sent"] is True
    assert order["shopify_id"] == "gid://shopify/Order/55"
    again = live.complete_draft_order(
        shopify_draft_id=opened["id"],
        email="avery@example.com",
        id="ord_test_2",
        customer_id="cus_test",
        phone="+15035550142",
        lines=order["lines"],
        currency="USD",
        idempotency_key="ord_abc",
        confirmation_token="K7MQ",
    )
    assert again["duplicate"] is True
    assert sum(1 for call in calls if "draftOrderComplete" in call["query"]) == 1
    variant = live.get_variant("gid://shopify/ProductVariant/1")
    assert variant["inventory_qty"] == 12
    assert variant["product_title"] == "Driftwood Ceramic Mug"
    assert live.search_products("linen shirt") == []
    live.close()
    db.close()


def test_seed_dev_store_creates_products_and_webhooks(tmp_path):
    catalog = {"products": [load_catalog()["products"][0]]}
    location = build_product_set_input(catalog["products"][0], "gid://shopify/Location/1")
    assert location["productOptions"][0]["name"] == "Size"
    assert location["variants"][0]["sku"] == "HLS-001-S"
    assert location["variants"][0]["price"] == "68.00"
    assert location["variants"][0]["inventoryQuantities"][0]["quantity"] == 4

    calls = []
    seen_skus: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body)
        query = body["query"]
        if "locations(" in query:
            return httpx.Response(
                200,
                json={"data": {"locations": {"nodes": [{"id": "gid://shopify/Location/1", "name": "Shop", "isActive": True}]}}},
            )
        if "productVariants(" in query:
            sku = body["variables"]["query"].split("sku:", 1)[1]
            nodes = [{"id": "gid://shopify/ProductVariant/9", "sku": sku}] if sku in seen_skus else []
            return httpx.Response(200, json={"data": {"productVariants": {"nodes": nodes}}})
        if "productSet" in query:
            for variant in body["variables"]["input"]["variants"]:
                seen_skus.add(variant["sku"])
            return httpx.Response(
                200,
                json={"data": {"productSet": {"userErrors": [], "product": {"id": "gid://shopify/Product/3", "title": "Coastal Linen Shirt"}}}},
            )
        if "webhookSubscriptionCreate" in query:
            assert body["variables"]["webhook"]["callbackUrl"] == "https://shop.example/webhooks/shopify"
            assert body["variables"]["topic"] in {"ORDERS_UPDATED", "FULFILLMENTS_CREATE", "FULFILLMENTS_UPDATE"}
            return httpx.Response(
                200,
                json={
                    "data": {
                        "webhookSubscriptionCreate": {
                            "userErrors": [],
                            "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/1", "topic": body["variables"]["topic"]},
                        }
                    }
                },
            )
        raise AssertionError(query)

    settings = _settings(
        tmp_path,
        shopify_store_domain="harbor-dev.myshopify.com",
        shopify_admin_token="shpat_test",
        public_base_url="https://shop.example",
    )
    assert public_webhook_base(settings) == "https://shop.example"
    assert public_webhook_base(_settings(tmp_path, public_base_url="http://127.0.0.1:8091")) == ""
    db = Database(settings.database_path)
    live = LiveShopify(settings, db, client=httpx.Client(transport=httpx.MockTransport(handler)))
    first = seed_products(live, catalog)
    assert first["created"] == ["Coastal Linen Shirt"]
    second = seed_products(live, catalog)
    assert second["skipped"] == ["Coastal Linen Shirt"]
    assert sum(1 for call in calls if "productSet" in call["query"]) == 1
    hooks = register_webhooks(live, "https://shop.example")
    assert hooks["registered"] == ["ORDERS_UPDATED", "FULFILLMENTS_CREATE", "FULFILLMENTS_UPDATE"]
    live.close()
    db.close()
    assert seed_run(_settings(tmp_path / "empty", shopify_store_domain="", shopify_admin_token="")) == 2


def test_shopify_webhook_notifies_by_template_and_email(tmp_path):
    secret = "shpss_test"
    settings = _settings(tmp_path, shopify_webhook_secret=secret, owner_email="don@example.com")
    app = create_app(settings)
    client = TestClient(app)
    payload = {
        "id": 55,
        "name": "#2001",
        "email": "avery@example.com",
        "financial_status": "pending",
        "fulfillment_status": "fulfilled",
        "customer": {"phone": "+15035550142", "email": "avery@example.com"},
        "fulfillments": [{"tracking_number": "1Z999"}],
    }
    raw = json.dumps(payload).encode()
    ok = client.post(
        "/webhooks/shopify",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-shopify-hmac-sha256": _shopify_sign(raw, secret),
            "x-shopify-topic": "orders/updated",
            "x-shopify-webhook-id": "wh_1",
        },
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "fulfilled"
    audits = app.state.service.db.list_audit("+15035550142", 10)
    template = next(row for row in audits if row["detail"].get("type") == "template")
    assert template["detail"]["payload"]["template"]["name"] == "harbor_order_status"
    assert template["detail"]["payload"]["template"]["components"][0]["parameters"][2]["text"].startswith("Tracking:")
    assert any(row["action"] == "email.demo" for row in audits)
    refused = client.post(
        "/webhooks/shopify",
        content=raw,
        headers={"content-type": "application/json", "x-shopify-topic": "orders/updated"},
    )
    assert refused.status_code == 401
    again = client.post(
        "/webhooks/shopify",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-shopify-hmac-sha256": _shopify_sign(raw, secret),
            "x-shopify-topic": "orders/updated",
            "x-shopify-webhook-id": "wh_1",
        },
    )
    assert again.json()["duplicate"] is True
    normalized = normalize_shopify_webhook(
        "fulfillments/create",
        {"order_id": 55, "status": "success", "tracking_number": "1Z999", "destination": {"phone": "+15035550142"}},
    )
    assert normalized["shopify_id"] == "55"
    assert "1Z999" in normalized["detail"]
    assert verify_shopify_hmac(raw, _shopify_sign(raw, secret), secret)
    assert not verify_shopify_hmac(raw, "nope", secret)
    app.state.service.close()


def test_gmail_demo_mode_and_smtp(tmp_path):
    demo_settings = _settings(tmp_path, owner_email="don@example.com")
    demo_db = Database(demo_settings.database_path)
    demo = Mailer(demo_settings, demo_db)
    skipped = demo.order_confirmation(
        phone="+15035550142",
        number="2001",
        total="$24.00",
        customer_email="avery@example.com",
        customer_name="Avery Chen",
        invoice_sent=True,
    )
    assert skipped["demo"] is True
    assert any(row["action"] == "email.demo" for row in demo_db.list_audit("+15035550142", 5))
    demo_db.close()

    _FakeSMTP.sent = []
    live_settings = _settings(
        tmp_path / "live",
        gmail_user="don@example.com",
        gmail_app_password="ab cd",
        owner_email="don@example.com",
    )
    live_db = Database(live_settings.database_path)
    mailer = Mailer(live_settings, live_db, smtp_factory=_FakeSMTP)
    sent = mailer.handoff_alert(phone="+15035550142", reason="I want a person", escalation_id="esc_1")
    assert sent["ok"] is True
    assert _FakeSMTP.login_password == "abcd"
    assert _FakeSMTP.sent[0]["To"] == "don@example.com"
    assert "esc_1" in _FakeSMTP.sent[0]["Subject"]
    assert any(row["action"] == "email.sent" for row in live_db.list_audit("+15035550142", 5))
    live_db.close()


def test_hosted_embeddings_and_bm25_fallback(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        vectors = []
        for text in body["input"]:
            lowered = text.lower()
            if "return" in lowered or "30 days" in lowered or "back" in lowered:
                vectors.append([1.0, 0.0, 0.0, 0.0])
            else:
                vectors.append([0.0, 1.0, 0.0, 0.0])
        return httpx.Response(
            200,
            json={"data": [{"index": index, "embedding": vector} for index, vector in enumerate(vectors)]},
        )

    settings = _settings(
        tmp_path,
        vector_store="qdrant",
        embedding_provider="hosted",
        embedding_model="jina-embeddings-v3",
        embedding_api_url="https://api.jina.ai/v1",
        embedding_api_key="jina_test",
        qdrant_url="",
    )
    db = Database(settings.database_path)
    transport = httpx.MockTransport(handler)
    kb = KnowledgeBase(settings, db, http_client=httpx.Client(transport=transport))
    try:
        assert kb.backend == "qdrant"
        assert kb.dim == 4
        documents = kb.load_markdown(settings.knowledge_path())
        kb.ensure(documents)
        hits = kb.search("can I send something back if it doesn't fit?", k=3)
        assert any("30 days" in hit["text"] for hit in hits)
    finally:
        kb.close()
        db.close()

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    broken_settings = _settings(
        tmp_path / "fallback",
        vector_store="qdrant",
        embedding_provider="hosted",
        embedding_api_url="https://api.jina.ai/v1",
        embedding_api_key="jina_test",
    )
    broken_db = Database(broken_settings.database_path)
    fallback = KnowledgeBase(
        broken_settings,
        broken_db,
        http_client=httpx.Client(transport=httpx.MockTransport(broken)),
    )
    try:
        assert fallback.backend == "memory"
        fallback.ensure(fallback.load_markdown(broken_settings.knowledge_path()))
        hits = fallback.search("can I send something back if it doesn't fit?", k=3)
        assert any("30 days" in hit["text"] for hit in hits)
    finally:
        fallback.close()
        broken_db.close()

    real_import = __import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "fastembed" or name.startswith("fastembed."):
            raise ImportError("fastembed is not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", guarded)
    fast_settings = _settings(
        tmp_path / "fast",
        vector_store="qdrant",
        embedding_provider="fastembed",
    )
    fast_db = Database(fast_settings.database_path)
    fast = KnowledgeBase(fast_settings, fast_db)
    try:
        assert fast.backend == "memory"
        assert fast_settings.resolved_embedding_model() == "BAAI/bge-small-en-v1.5"
    finally:
        fast.close()
        fast_db.close()

"""Create the Harbor & Co. demo catalogue in a Shopify Partner development store.

Uses the Admin API token (SHOPIFY_STORE_DOMAIN + SHOPIFY_ADMIN_TOKEN). Safe to
re-run: a product is skipped when one of its SKUs already exists. When
PUBLIC_BASE_URL is a public https origin, the same run registers the order
and fulfillment webhooks.
"""

from __future__ import annotations

import html
import sys

import httpx

from harbor.catalog import load_catalog
from harbor.config import Settings, get_settings
from harbor.db import Database, StoreError
from harbor.shopify_live import LiveShopify

LOCATION_QUERY = """
query Locations {
  locations(first: 5) {
    nodes { id name isActive }
  }
}
"""

SKU_QUERY = """
query Sku($query: String!) {
  productVariants(first: 1, query: $query) {
    nodes { id sku }
  }
}
"""

PRODUCT_SET = """
mutation SeedProduct($input: ProductSetInput!) {
  productSet(synchronous: true, input: $input) {
    product { id title }
    userErrors { field message }
  }
}
"""

WEBHOOK_CREATE = """
mutation Webhook($topic: WebhookSubscriptionTopic!, $webhook: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $webhook) {
    webhookSubscription { id topic }
    userErrors { field message }
  }
}
"""

WEBHOOK_TOPICS = ("ORDERS_UPDATED", "FULFILLMENTS_CREATE", "FULFILLMENTS_UPDATE")


def public_webhook_base(settings: Settings) -> str:
    base = (settings.public_base_url or "").strip().rstrip("/")
    if not base.startswith("https://"):
        return ""
    host = base.removeprefix("https://").split("/")[0]
    if host.startswith("127.0.0.1") or host.startswith("localhost"):
        return ""
    return base


def build_product_set_input(product: dict, location_id: str) -> dict:
    variants = product["variants"]
    sized = len(variants) > 1 or variants[0]["title"] not in {"Default", "Default Title"}
    if sized:
        option_name = "Size"
        option_values = [{"name": variant["title"]} for variant in variants]
    else:
        option_name = "Title"
        option_values = [{"name": "Default"}]
    variant_inputs = []
    for variant in variants:
        value_name = variant["title"] if sized else "Default"
        price = f"{int(variant['price_cents']) / 100:.2f}"
        variant_inputs.append(
            {
                "optionValues": [{"optionName": option_name, "name": value_name}],
                "sku": variant["sku"],
                "price": price,
                "inventoryQuantities": [
                    {
                        "locationId": location_id,
                        "name": "available",
                        "quantity": int(variant["inventory_qty"]),
                    }
                ],
            }
        )
    description = html.escape(product.get("description") or "")
    return {
        "title": product["title"],
        "descriptionHtml": f"<p>{description}</p>",
        "tags": list(product.get("tags") or []),
        "status": "ACTIVE",
        "productOptions": [{"name": option_name, "values": option_values}],
        "variants": variant_inputs,
    }


def _first_location(live: LiveShopify) -> str:
    data = live.graphql(LOCATION_QUERY, {})
    nodes = data.get("locations", {}).get("nodes") or []
    active = [node for node in nodes if node.get("isActive", True)]
    chosen = (active or nodes or [None])[0]
    if not chosen or not chosen.get("id"):
        raise StoreError("shopify_error", "The dev store has no location to attach inventory to.")
    return chosen["id"]


def _sku_exists(live: LiveShopify, sku: str) -> bool:
    data = live.graphql(SKU_QUERY, {"query": f"sku:{sku}"})
    return bool((data.get("productVariants") or {}).get("nodes"))


def seed_products(live: LiveShopify, catalog: dict | None = None) -> dict:
    catalog = catalog or load_catalog()
    location_id = _first_location(live)
    created: list[str] = []
    skipped: list[str] = []
    for product in catalog.get("products") or []:
        skus = [variant["sku"] for variant in product["variants"] if variant.get("sku")]
        if any(_sku_exists(live, sku) for sku in skus):
            skipped.append(product["title"])
            continue
        payload_input = build_product_set_input(product, location_id)
        data = live.graphql(PRODUCT_SET, {"input": payload_input})
        payload = data.get("productSet") or {}
        errors = payload.get("userErrors") or []
        if errors or not (payload.get("product") or {}).get("id"):
            message = errors[0]["message"] if errors else "Shopify did not create the product."
            raise StoreError("shopify_error", f"{product['title']}: {message}")
        created.append(product["title"])
    return {"created": created, "skipped": skipped, "location_id": location_id}


def register_webhooks(live: LiveShopify, public_base: str) -> dict:
    if not public_base:
        return {
            "registered": [],
            "problems": ["skipped: set PUBLIC_BASE_URL to the public https origin"],
        }
    callback = public_base.rstrip("/") + "/webhooks/shopify"
    registered: list[str] = []
    problems: list[str] = []
    for topic in WEBHOOK_TOPICS:
        data = live.graphql(
            WEBHOOK_CREATE,
            {
                "topic": topic,
                "webhook": {"callbackUrl": callback, "format": "JSON"},
            },
        )
        payload = data.get("webhookSubscriptionCreate") or {}
        errors = payload.get("userErrors") or []
        if errors or not (payload.get("webhookSubscription") or {}).get("id"):
            message = errors[0]["message"] if errors else "Shopify did not register the webhook."
            # "already been taken" means a previous run registered it.
            if "already" in message.lower():
                registered.append(topic)
                continue
            problems.append(f"{topic}: {message}")
            continue
        registered.append(topic)
    return {"registered": registered, "problems": problems, "callback": callback}


def run(settings: Settings, client: httpx.Client | None = None) -> int:
    if not settings.shopify_store_domain.strip() or not settings.shopify_admin_token.strip():
        print(
            "Set SHOPIFY_STORE_DOMAIN and SHOPIFY_ADMIN_TOKEN. "
            "APP_MODE can stay mock while you seed.",
            file=sys.stderr,
        )
        return 2
    db = Database(settings.database_path)
    live = LiveShopify(settings, db, client=client)
    try:
        seeded = seed_products(live)
        hooks = register_webhooks(live, public_webhook_base(settings))
    except StoreError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    finally:
        live.close()
        db.close()
    print(f"Created: {', '.join(seeded['created']) or '(none)'}")
    print(f"Skipped (SKU already in the store): {', '.join(seeded['skipped']) or '(none)'}")
    if hooks["registered"]:
        print(f"Webhooks: {', '.join(hooks['registered'])} -> {hooks.get('callback', '')}")
    for problem in hooks["problems"]:
        print(f"Webhook note: {problem}", file=sys.stderr)
    return 0


def main() -> int:
    return run(get_settings())

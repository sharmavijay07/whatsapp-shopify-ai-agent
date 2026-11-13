"""Shopify Admin GraphQL client for a development or live store.

Orders are created as pending. Payment is never captured here.
Inventory uses OrderCreateOptionsInput.inventoryBehaviour (British spelling),
which is the field the Admin API actually accepts.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from harbor.config import Settings
from harbor.db import Database, StoreError
from harbor.service_ids import uid

PRODUCT_QUERY = """
query Products($query: String!) {
  products(first: 40, query: $query) {
    nodes {
      id
      title
      description
      tags
      variants(first: 20) {
        nodes { id title sku price inventoryQuantity }
      }
    }
  }
}
"""

CUSTOMER_SEARCH = """
query Customers($query: String!) {
  customers(first: 1, query: $query) {
    nodes { id displayName phone email }
  }
}
"""

CUSTOMER_CREATE = """
mutation CustomerCreate($input: CustomerInput!) {
  customerCreate(input: $input) {
    customer { id displayName phone email }
    userErrors { field message }
  }
}
"""

ORDER_CREATE = """
mutation OrderCreate($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {
  orderCreate(order: $order, options: $options) {
    userErrors { field message }
    order {
      id
      name
      displayFinancialStatus
      totalPriceSet { shopMoney { amount currencyCode } }
    }
  }
}
"""

ORDER_SEARCH = """
query Orders($query: String!) {
  orders(first: 10, query: $query) {
    nodes {
      id
      name
      displayFinancialStatus
      displayFulfillmentStatus
      createdAt
      customer { id phone displayName }
      totalPriceSet { shopMoney { amount currencyCode } }
      lineItems(first: 20) {
        nodes {
          title
          quantity
          variant { id }
          originalUnitPriceSet { shopMoney { amount } }
        }
      }
    }
  }
}
"""


def _money_to_cents(amount: str | None) -> int:
    if not amount:
        return 0
    return int(round(float(amount) * 100))


def split_name(name: str) -> tuple[str, str]:
    parts = (name or "").strip().split()
    if not parts:
        return "WhatsApp", "Customer"
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


class LiveShopify:
    def __init__(self, settings: Settings, db: Database, client: httpx.Client | None = None):
        self.settings = settings
        self.db = db
        self._client = client or httpx.Client(timeout=20.0)
        self._cache: list[dict] | None = None
        self._cached_at = 0.0

    def close(self) -> None:
        self._client.close()

    def _endpoint(self) -> str:
        host = (
            self.settings.shopify_store_domain.strip()
            .removeprefix("https://")
            .removeprefix("http://")
            .strip("/")
        )
        version = self.settings.shopify_api_version
        return f"https://{host}/admin/api/{version}/graphql.json"

    def graphql(
        self,
        query: str,
        variables: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "X-Shopify-Access-Token": self.settings.shopify_admin_token,
            "Content-Type": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = self._client.post(
            self._endpoint(),
            headers=headers,
            json={"query": query, "variables": variables or {}},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            message = payload["errors"][0].get("message", "Shopify rejected the request.")
            raise StoreError("shopify_error", message)
        return payload.get("data") or {}

    def list_products(self) -> list[dict]:
        now = time.time()
        if self._cache is not None and now - self._cached_at < 30:
            return self._cache
        data = self.graphql(PRODUCT_QUERY, {"query": "status:active"})
        products = []
        for node in data.get("products", {}).get("nodes", []):
            variants = []
            for variant in node.get("variants", {}).get("nodes", []):
                variants.append(
                    {
                        "id": variant["id"],
                        "product_id": node["id"],
                        "title": variant.get("title") or "Default",
                        "sku": variant.get("sku") or "",
                        "price_cents": _money_to_cents(variant.get("price")),
                        "inventory_qty": int(variant.get("inventoryQuantity") or 0),
                    }
                )
            title = node["title"]
            products.append(
                {
                    "id": node["id"],
                    "title": title,
                    "description": node.get("description") or "",
                    "tags": node.get("tags") or [],
                    "aliases": [title, title.lower()],
                    "variants": variants,
                }
            )
        self._cache = products
        self._cached_at = now
        return products

    def get_variant(self, variant_id: str) -> dict | None:
        for product in self.list_products():
            for variant in product["variants"]:
                if variant["id"] == variant_id:
                    return {**variant, "product_title": product["title"]}
        return None

    def find_customer_by_phone(self, phone: str) -> dict | None:
        data = self.graphql(CUSTOMER_SEARCH, {"query": f"phone:{phone}"})
        nodes = data.get("customers", {}).get("nodes", [])
        if not nodes:
            return self.db.get_customer_by_phone(phone)
        node = nodes[0]
        return self._mirror_customer(node, fallback_phone=phone)

    def create_customer(self, *, name: str, phone: str, email: str = "") -> dict:
        existing = self.find_customer_by_phone(phone)
        if existing:
            return existing
        first, last = split_name(name)
        customer_input: dict[str, Any] = {"firstName": first, "lastName": last, "phone": phone}
        if email:
            customer_input["email"] = email
        data = self.graphql(CUSTOMER_CREATE, {"input": customer_input})
        payload = data.get("customerCreate") or {}
        errors = payload.get("userErrors") or []
        if errors:
            raise StoreError("shopify_error", errors[0]["message"])
        return self._mirror_customer(payload["customer"], fallback_phone=phone, fallback_name=name)

    def _mirror_customer(self, node: dict, *, fallback_phone: str, fallback_name: str = "") -> dict:
        phone = node.get("phone") or fallback_phone
        name = node.get("displayName") or fallback_name or "WhatsApp customer"
        existing = self.db.get_customer_by_phone(phone)
        if existing:
            return existing
        return self.db.create_customer(
            id=uid("cus"),
            name=name,
            phone=phone,
            email=node.get("email") or "",
        )

    def create_order(self, **kwargs) -> dict:
        idempotency_key = kwargs["idempotency_key"]
        existing = self.db.get_idempotency(idempotency_key)
        if existing:
            return existing
        customer = self.db.get_customer(kwargs["customer_id"])
        if customer is None:
            raise StoreError("unauthorized", "No verified customer for this phone.")
        shopify_customer = self.find_customer_by_phone(customer["phone"])
        # find_customer mirrors and may return the local row. We need the Shopify gid.
        gid = self._shopify_customer_gid(customer["phone"])
        order_input = {
            "lineItems": [
                {"variantId": line["variant_id"], "quantity": int(line["quantity"])}
                for line in kwargs["lines"]
            ],
            "customer": {"toAssociate": {"id": gid}},
            "financialStatus": "PENDING",
            "note": "Created by the Harbor agent after an explicit WhatsApp confirmation.",
            "tags": ["harbor-agent", "whatsapp"],
        }
        data = self.graphql(
            ORDER_CREATE,
            {
                "order": order_input,
                "options": {
                    "inventoryBehaviour": "DECREMENT_OBEYING_POLICY",
                    "sendReceipt": False,
                    "sendFulfillmentReceipt": False,
                },
            },
            idempotency_key=idempotency_key,
        )
        payload = data.get("orderCreate") or {}
        errors = payload.get("userErrors") or []
        if errors or not payload.get("order"):
            message = errors[0]["message"] if errors else "Shopify did not return an order."
            raise StoreError("shopify_error", message)
        remote = payload["order"]
        number = str(remote.get("name") or "").lstrip("#") or None
        return self.db.create_order(
            decrement_inventory=False,
            shopify_id=remote.get("id"),
            number=number,
            status="pending",
            financial_status=(remote.get("displayFinancialStatus") or "pending").lower(),
            **kwargs,
        )

    def _shopify_customer_gid(self, phone: str) -> str:
        data = self.graphql(CUSTOMER_SEARCH, {"query": f"phone:{phone}"})
        nodes = data.get("customers", {}).get("nodes", [])
        if not nodes:
            raise StoreError("shopify_error", "Shopify has no customer for this WhatsApp number.")
        return nodes[0]["id"]

    def orders_for_phone(self, phone: str) -> list[dict]:
        data = self.graphql(ORDER_SEARCH, {"query": f"phone:{phone}"})
        return [self._order_node(node, phone) for node in data.get("orders", {}).get("nodes", [])]

    def get_order_by_number(self, number: str) -> dict | None:
        cleaned = number.lstrip("#")
        data = self.graphql(ORDER_SEARCH, {"query": f"name:#{cleaned}"})
        nodes = data.get("orders", {}).get("nodes", [])
        if not nodes:
            return self.db.get_order_by_number(cleaned)
        node = nodes[0]
        phone = ((node.get("customer") or {}).get("phone")) or ""
        return self._order_node(node, phone)

    def _order_node(self, node: dict, phone: str) -> dict:
        lines = []
        for item in node.get("lineItems", {}).get("nodes", []):
            price = ((item.get("originalUnitPriceSet") or {}).get("shopMoney") or {}).get("amount")
            lines.append(
                {
                    "variant_id": (item.get("variant") or {}).get("id") or "",
                    "title": item.get("title") or "Item",
                    "quantity": int(item.get("quantity") or 0),
                    "unit_price_cents": _money_to_cents(price),
                }
            )
        total = ((node.get("totalPriceSet") or {}).get("shopMoney") or {})
        return {
            "id": node.get("id"),
            "number": str(node.get("name") or "").lstrip("#"),
            "customer_id": ((node.get("customer") or {}).get("id")) or "",
            "phone": phone,
            "status": (node.get("displayFulfillmentStatus") or "open").lower(),
            "financial_status": (node.get("displayFinancialStatus") or "pending").lower(),
            "total_cents": _money_to_cents(total.get("amount")),
            "currency": total.get("currencyCode") or self.settings.currency,
            "idempotency_key": "",
            "created_at": node.get("createdAt") or "",
            "lines": lines,
            "duplicate": False,
            "shopify_id": node.get("id"),
        }

"""Shopify Admin GraphQL client for a development or live store.

A live order starts as a Shopify draft (`draftOrderCreate`). Confirmation
completes that draft with payment still pending and asks Shopify to email
the invoice. Payment is never captured here.

`create_order` remains the direct `orderCreate` path (pending, with
`inventoryBehaviour: DECREMENT_OBEYING_POLICY`) for callers that do not
have a draft id. The agent uses the draft path.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from harbor.catalog import load_catalog
from harbor.config import Settings
from harbor.db import Database, StoreError
from harbor.service_ids import uid

_SEARCH_STOP = {
    "a", "an", "the", "is", "are", "of", "for", "to", "and", "or", "my", "your",
    "you", "we", "i", "do", "does", "what", "how", "can", "me", "it", "this",
    "that", "with", "on", "in", "if", "please", "like", "want", "would", "much",
    "have", "stock", "available", "about", "tell", "show",
}

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
      customer { id phone displayName email }
      totalPriceSet { shopMoney { amount currencyCode } }
      lineItems(first: 20) {
        nodes {
          title
          quantity
          variant { id }
          originalUnitPriceSet { shopMoney { amount } }
        }
      }
      fulfillments(first: 3) {
        nodes {
          status
          trackingInfo { number url company }
        }
      }
    }
  }
}
"""

VARIANT_QUERY = """
query Variant($id: ID!) {
  productVariant(id: $id) {
    id
    title
    sku
    price
    inventoryQuantity
    product { id title }
  }
}
"""

DRAFT_CREATE = """
mutation DraftCreate($input: DraftOrderInput!) {
  draftOrderCreate(input: $input) {
    draftOrder { id name invoiceUrl status }
    userErrors { field message }
  }
}
"""

DRAFT_COMPLETE = """
mutation DraftComplete($id: ID!) {
  draftOrderComplete(id: $id, paymentPending: true) {
    draftOrder {
      id
      order {
        id
        name
        displayFinancialStatus
        totalPriceSet { shopMoney { amount currencyCode } }
      }
    }
    userErrors { field message }
  }
}
"""

ORDER_INVOICE = """
mutation Invoice($id: ID!, $email: EmailInput) {
  orderInvoiceSend(id: $id, email: $email) {
    order { id }
    userErrors { field message }
  }
}
"""

DRAFT_DELETE = """
mutation DraftDelete($input: DraftOrderDeleteInput!) {
  draftOrderDelete(input: $input) {
    deletedId
    userErrors { field message }
  }
}
"""


def shopify_search_query(text: str) -> str:
    """Shopify product search string. Empty chatter falls back to active products."""
    tokens: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9']+", text or ""):
        word = raw.lower().strip("'")
        if len(word) < 3 or word in _SEARCH_STOP:
            continue
        if word not in tokens:
            tokens.append(word)
        if len(tokens) == 6:
            break
    if not tokens:
        return "status:active"
    return "status:active " + " ".join(tokens)


def catalogue_aliases(title: str, variants: list[dict]) -> list[str]:
    """Reuse the demo catalogue's aliases when a seeded dev store uses the same SKUs."""
    skus = {variant.get("sku") for variant in variants if variant.get("sku")}
    try:
        products = load_catalog().get("products") or []
    except Exception:  # noqa: BLE001 — alias enrichment is optional
        products = []
    for product in products:
        if product["title"].strip().lower() == (title or "").strip().lower():
            return list(product.get("aliases") or [title])
        catalog_skus = {variant.get("sku") for variant in product.get("variants") or []}
        if skus and skus & catalog_skus:
            return list(product.get("aliases") or [title])
    lowered = (title or "").lower()
    if lowered and lowered != title:
        return [title, lowered]
    return [title or "Product"]


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
        products = self._map_products(data)
        self._cache = products
        self._cached_at = now
        return products

    def list_products_fresh(self) -> list[dict]:
        self._cache = None
        return self.list_products()

    def search_products(self, text: str) -> list[dict]:
        data = self.graphql(PRODUCT_QUERY, {"query": shopify_search_query(text)})
        return self._map_products(data)

    def _map_products(self, data: dict) -> list[dict]:
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
                    "aliases": catalogue_aliases(title, variants),
                    "variants": variants,
                }
            )
        return products

    def _cached_variant(self, variant_id: str) -> dict | None:
        for product in self._cache or []:
            for variant in product["variants"]:
                if variant["id"] == variant_id:
                    return {**variant, "product_title": product["title"]}
        return None

    def get_variant(self, variant_id: str) -> dict | None:
        """Live inventory and price for one variant. The 30s catalogue cache is not enough here."""
        try:
            data = self.graphql(VARIANT_QUERY, {"id": variant_id})
        except StoreError:
            return self._cached_variant(variant_id)
        node = data.get("productVariant")
        if not node:
            return self._cached_variant(variant_id)
        product = node.get("product") or {}
        return {
            "id": node["id"],
            "product_id": product.get("id") or "",
            "title": node.get("title") or "Default",
            "sku": node.get("sku") or "",
            "price_cents": _money_to_cents(node.get("price")),
            "inventory_qty": int(node.get("inventoryQuantity") or 0),
            "product_title": product.get("title") or "",
        }

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
        # Keep the verified WhatsApp number as the local key. Shopify sometimes
        # echoes a differently formatted phone, which would split the customer.
        phone = fallback_phone or node.get("phone") or ""
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

    def open_draft_order(
        self,
        *,
        customer: dict,
        lines: list[dict],
        email: str = "",
        note: str = "",
    ) -> dict:
        """Open a real Shopify draft. This is not an order and does not take payment."""
        gid = self._shopify_customer_gid(customer["phone"])
        draft_input: dict[str, Any] = {
            "lineItems": [
                {"variantId": line["variant_id"], "quantity": int(line["quantity"])}
                for line in lines
            ],
            "purchasingEntity": {"customerId": gid},
            "note": note or "Harbor agent draft. Not an order until the customer replies YES.",
            "tags": ["harbor-agent", "whatsapp"],
        }
        recipient = (email or customer.get("email") or "").strip()
        if recipient:
            draft_input["email"] = recipient
        data = self.graphql(DRAFT_CREATE, {"input": draft_input})
        payload = data.get("draftOrderCreate") or {}
        errors = payload.get("userErrors") or []
        remote = payload.get("draftOrder") or {}
        if errors or not remote.get("id"):
            message = errors[0]["message"] if errors else "Shopify did not open a draft order."
            raise StoreError("shopify_error", message)
        return {
            "id": remote["id"],
            "name": remote.get("name") or "",
            "invoice_url": remote.get("invoiceUrl") or "",
        }

    def delete_draft_order(self, draft_id: str) -> None:
        if not draft_id:
            return
        data = self.graphql(DRAFT_DELETE, {"input": {"id": draft_id}})
        payload = data.get("draftOrderDelete") or {}
        errors = payload.get("userErrors") or []
        if errors:
            raise StoreError("shopify_error", errors[0]["message"])

    def complete_draft_order(
        self,
        *,
        shopify_draft_id: str,
        email: str = "",
        **kwargs,
    ) -> dict:
        """Complete the Shopify draft as an unpaid order and email the invoice when we have an address."""
        idempotency_key = kwargs["idempotency_key"]
        existing = self.db.get_idempotency(idempotency_key)
        if existing:
            return existing
        data = self.graphql(
            DRAFT_COMPLETE,
            {"id": shopify_draft_id},
            idempotency_key=idempotency_key,
        )
        payload = data.get("draftOrderComplete") or {}
        errors = payload.get("userErrors") or []
        remote_order = ((payload.get("draftOrder") or {}).get("order")) or {}
        if errors or not remote_order.get("id"):
            message = errors[0]["message"] if errors else "Shopify did not complete the draft."
            raise StoreError("shopify_error", message)
        invoice_sent = False
        recipient = (email or "").strip()
        if recipient:
            try:
                invoice = self.graphql(
                    ORDER_INVOICE,
                    {
                        "id": remote_order["id"],
                        "email": {
                            "to": recipient,
                            "subject": f"Harbor & Co. invoice for order {remote_order.get('name') or ''}".strip(),
                            "customMessage": (
                                "Your WhatsApp confirmation opened this Shopify order. "
                                "Payment is still pending. This message is the invoice, not a card charge."
                            ),
                        },
                    },
                )
                invoice_payload = invoice.get("orderInvoiceSend") or {}
                invoice_errors = invoice_payload.get("userErrors") or []
            except Exception as exc:  # noqa: BLE001 — the order already exists; don't hide it
                invoice_errors = [{"message": str(exc)[:300]}]
                invoice_payload = {}
            if invoice_errors:
                self.db.audit(
                    kwargs.get("phone"),
                    "shopify.invoice",
                    "error",
                    {"message": invoice_errors[0]["message"], "order_id": remote_order["id"]},
                )
            else:
                invoice_sent = True
                self.db.audit(
                    kwargs.get("phone"),
                    "shopify.invoice",
                    "success",
                    {"to": recipient, "order_id": remote_order["id"]},
                )
        else:
            self.db.audit(
                kwargs.get("phone"),
                "shopify.invoice",
                "skipped",
                {"reason": "no customer email", "order_id": remote_order["id"]},
            )
        number = str(remote_order.get("name") or "").lstrip("#") or None
        self._cache = None
        order = self.db.create_order(
            decrement_inventory=False,
            shopify_id=remote_order.get("id"),
            number=number,
            status="pending",
            financial_status=(remote_order.get("displayFinancialStatus") or "pending").lower(),
            **kwargs,
        )
        order["invoice_sent"] = invoice_sent
        order["shopify_draft_id"] = shopify_draft_id
        return order

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
        tracking = _tracking(node.get("fulfillments"))
        customer = node.get("customer") or {}
        return {
            "id": node.get("id"),
            "number": str(node.get("name") or "").lstrip("#"),
            "customer_id": customer.get("id") or "",
            "phone": phone or customer.get("phone") or "",
            "email": customer.get("email") or "",
            "status": (node.get("displayFulfillmentStatus") or "unfulfilled").lower(),
            "financial_status": (node.get("displayFinancialStatus") or "pending").lower(),
            "fulfillment_status": (node.get("displayFulfillmentStatus") or "unfulfilled").lower(),
            "tracking": tracking,
            "total_cents": _money_to_cents(total.get("amount")),
            "currency": total.get("currencyCode") or self.settings.currency,
            "idempotency_key": "",
            "created_at": node.get("createdAt") or "",
            "lines": lines,
            "duplicate": False,
            "shopify_id": node.get("id"),
        }


def _tracking(fulfillments: dict | None) -> str:
    numbers: list[str] = []
    for node in (fulfillments or {}).get("nodes") or []:
        for info in node.get("trackingInfo") or []:
            number = (info or {}).get("number") or ""
            if number and number not in numbers:
                numbers.append(number)
    return ", ".join(numbers)

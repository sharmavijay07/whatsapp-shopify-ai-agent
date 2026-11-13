"""Load the seeded Harbour catalogue shipped with the package."""

from __future__ import annotations

import json
from functools import lru_cache

from harbor.config import PACKAGE_DIR


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    path = PACKAGE_DIR / "seed" / "catalog.json"
    return json.loads(path.read_text(encoding="utf-8"))


def product_documents(products: list[dict]) -> list[dict]:
    from harbor.money import format_money

    documents = []
    for product in products:
        prices = sorted({variant["price_cents"] for variant in product["variants"]})
        price = (
            format_money(prices[0])
            if len(prices) == 1
            else " or ".join(format_money(amount) for amount in prices)
        )
        options = ", ".join(variant["title"] for variant in product["variants"])
        stock_note = (
            "Live quantity changes. Always check the inventory tool before promising stock."
        )
        text = (
            f"{product['title']} costs {price}. Options: {options}. "
            f"{product['description']} Tags: {', '.join(product.get('tags') or [])}. {stock_note}"
        )
        documents.append(
            {
                "id": f"product:{product['id']}",
                "title": product["title"],
                "text": text,
                "source": "shopify-catalog",
                "kind": "product",
            }
        )
    return documents

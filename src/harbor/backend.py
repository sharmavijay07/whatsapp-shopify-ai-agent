"""Shopify catalogue and orders. Mock mode is a durable stand-in for a dev store."""

from __future__ import annotations

from harbor.catalog import load_catalog
from harbor.db import Database


class CommerceBackend:
    def list_products(self) -> list[dict]:
        raise NotImplementedError

    def get_variant(self, variant_id: str) -> dict | None:
        raise NotImplementedError

    def find_customer_by_phone(self, phone: str) -> dict | None:
        raise NotImplementedError

    def create_customer(self, *, name: str, phone: str, email: str = "") -> dict:
        raise NotImplementedError

    def create_order(self, **kwargs) -> dict:
        raise NotImplementedError

    def orders_for_phone(self, phone: str) -> list[dict]:
        raise NotImplementedError

    def get_order_by_number(self, number: str) -> dict | None:
        raise NotImplementedError


class MockShopify(CommerceBackend):
    def __init__(self, db: Database):
        self.db = db
        self.catalog = load_catalog()

    def seed(self, force: bool = False) -> None:
        if force:
            self.db.clear_commerce()
        elif self.db.list_products():
            return
        self.db.replace_products(self.catalog["products"])
        for customer in self.catalog["customers"]:
            self.db.create_customer(
                id=customer["id"],
                name=customer["name"],
                phone=customer["phone"],
                email=customer.get("email") or "",
            )
        for order in self.catalog["orders"]:
            self.db.insert_seed_order(order, self.catalog.get("currency", "USD"))

    def list_products(self) -> list[dict]:
        return self.db.list_products()

    def get_variant(self, variant_id: str) -> dict | None:
        return self.db.get_variant(variant_id)

    def find_customer_by_phone(self, phone: str) -> dict | None:
        return self.db.get_customer_by_phone(phone)

    def create_customer(self, *, name: str, phone: str, email: str = "") -> dict:
        from harbor.service_ids import uid

        return self.db.create_customer(id=uid("cus"), name=name, phone=phone, email=email)

    def create_order(self, **kwargs) -> dict:
        return self.db.create_order(decrement_inventory=True, **kwargs)

    def orders_for_phone(self, phone: str) -> list[dict]:
        return self.db.orders_for_phone(phone)

    def get_order_by_number(self, number: str) -> dict | None:
        return self.db.get_order_by_number(number)

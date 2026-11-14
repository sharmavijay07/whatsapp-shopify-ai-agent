"""SQLite memory: catalogue cache, drafts, orders, conversations, audit.

The audit table is append-only. Callers do not get an update or delete for it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class StoreError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    return (moment or utcnow()).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  aliases_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS variants (
  id TEXT PRIMARY KEY,
  product_id TEXT NOT NULL,
  title TEXT NOT NULL,
  sku TEXT NOT NULL,
  price_cents INTEGER NOT NULL,
  inventory_qty INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  phone TEXT NOT NULL UNIQUE,
  email TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  number TEXT NOT NULL UNIQUE,
  customer_id TEXT NOT NULL,
  phone TEXT NOT NULL,
  status TEXT NOT NULL,
  financial_status TEXT NOT NULL,
  total_cents INTEGER NOT NULL,
  currency TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  confirmation_token TEXT,
  shopify_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT NOT NULL,
  variant_id TEXT NOT NULL,
  title TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  unit_price_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY,
  order_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
  id TEXT PRIMARY KEY,
  phone TEXT NOT NULL,
  token TEXT NOT NULL,
  customer_id TEXT,
  lines_json TEXT NOT NULL,
  total_cents INTEGER NOT NULL,
  currency TEXT NOT NULL,
  status TEXT NOT NULL,
  created_turn_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  shopify_draft_id TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  turn_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
  phone TEXT PRIMARY KEY,
  escalated INTEGER NOT NULL DEFAULT 0,
  escalation_id TEXT,
  customer_name TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS escalations (
  id TEXT PRIMARY KEY,
  phone TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  phone TEXT,
  action TEXT NOT NULL,
  outcome TEXT NOT NULL,
  detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_messages (
  message_id TEXT PRIMARY KEY,
  phone TEXT,
  received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_chunks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  text TEXT NOT NULL,
  source TEXT NOT NULL,
  kind TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(phone, id);
CREATE INDEX IF NOT EXISTS idx_orders_phone ON orders(phone, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(id);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(drafts)").fetchall()
        }
        if "shopify_draft_id" not in columns:
            self._conn.execute("ALTER TABLE drafts ADD COLUMN shopify_draft_id TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def get_meta(self, key: str) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def clear_commerce(self) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for table in (
                    "order_lines",
                    "orders",
                    "idempotency",
                    "drafts",
                    "messages",
                    "conversations",
                    "escalations",
                    "audit_log",
                    "inbound_messages",
                    "customers",
                    "variants",
                    "products",
                ):
                    self._conn.execute(f"DELETE FROM {table}")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def replace_products(self, products: list[dict]) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM variants")
                self._conn.execute("DELETE FROM products")
                for product in products:
                    self._conn.execute(
                        "INSERT INTO products(id, title, description, tags_json, aliases_json) "
                        "VALUES(?, ?, ?, ?, ?)",
                        (
                            product["id"],
                            product["title"],
                            product["description"],
                            json.dumps(product.get("tags") or []),
                            json.dumps(product.get("aliases") or [product["title"]]),
                        ),
                    )
                    for variant in product["variants"]:
                        self._conn.execute(
                            "INSERT INTO variants(id, product_id, title, sku, price_cents, inventory_qty) "
                            "VALUES(?, ?, ?, ?, ?, ?)",
                            (
                                variant["id"],
                                product["id"],
                                variant["title"],
                                variant["sku"],
                                int(variant["price_cents"]),
                                int(variant["inventory_qty"]),
                            ),
                        )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def list_products(self) -> list[dict]:
        products = self._all("SELECT * FROM products ORDER BY title")
        variants = self._all("SELECT * FROM variants ORDER BY id")
        by_id = {}
        for product in products:
            product["tags"] = json.loads(product.pop("tags_json"))
            product["aliases"] = json.loads(product.pop("aliases_json"))
            product["variants"] = []
            by_id[product["id"]] = product
        for variant in variants:
            parent = by_id.get(variant["product_id"])
            if parent is not None:
                parent["variants"].append(
                    {
                        "id": variant["id"],
                        "product_id": variant["product_id"],
                        "title": variant["title"],
                        "sku": variant["sku"],
                        "price_cents": variant["price_cents"],
                        "inventory_qty": variant["inventory_qty"],
                    }
                )
        return list(by_id.values())

    def get_variant(self, variant_id: str) -> dict | None:
        for product in self.list_products():
            for variant in product["variants"]:
                if variant["id"] == variant_id:
                    return {**variant, "product_title": product["title"], "product_id": product["id"]}
        return None

    def get_customer_by_phone(self, phone: str) -> dict | None:
        return self._one("SELECT * FROM customers WHERE phone = ?", (phone,))

    def get_customer(self, customer_id: str) -> dict | None:
        return self._one("SELECT * FROM customers WHERE id = ?", (customer_id,))

    def create_customer(self, *, id: str, name: str, phone: str, email: str = "") -> dict:
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM customers WHERE phone = ?", (phone,)
            ).fetchone()
            if existing:
                if email and not existing["email"]:
                    self._conn.execute(
                        "UPDATE customers SET email = ? WHERE id = ?",
                        (email, existing["id"]),
                    )
                    existing = self._conn.execute(
                        "SELECT * FROM customers WHERE id = ?", (existing["id"],)
                    ).fetchone()
                return dict(existing)
            self._conn.execute(
                "INSERT INTO customers(id, name, phone, email, created_at) VALUES(?, ?, ?, ?, ?)",
                (id, name, phone, email, iso()),
            )
        created = self.get_customer(id)
        assert created is not None
        return created

    def insert_seed_order(self, order: dict, currency: str) -> None:
        lines = order["lines"]
        total = sum(line["quantity"] * line["unit_price_cents"] for line in lines)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO orders("
                    "id, number, customer_id, phone, status, financial_status, total_cents, "
                    "currency, idempotency_key, confirmation_token, shopify_id, created_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        order["id"],
                        order["number"],
                        order["customer_id"],
                        order["phone"],
                        order.get("status", "open"),
                        order.get("financial_status", "paid"),
                        total,
                        currency,
                        order["idempotency_key"],
                        None,
                        order.get("shopify_id"),
                        order.get("created_at") or iso(utcnow() - timedelta(days=1)),
                    ),
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO idempotency(key, order_id, created_at) VALUES(?, ?, ?)",
                    (order["idempotency_key"], order["id"], iso()),
                )
                existing_lines = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM order_lines WHERE order_id = ?",
                    (order["id"],),
                ).fetchone()["n"]
                if existing_lines == 0:
                    for line in lines:
                        self._conn.execute(
                            "INSERT INTO order_lines(order_id, variant_id, title, quantity, unit_price_cents) "
                            "VALUES(?, ?, ?, ?, ?)",
                            (
                                order["id"],
                                line["variant_id"],
                                line["title"],
                                line["quantity"],
                                line["unit_price_cents"],
                            ),
                        )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _order_from_row(self, row: dict, lines: list[dict] | None = None) -> dict:
        if lines is None:
            lines = self._all(
                "SELECT variant_id, title, quantity, unit_price_cents FROM order_lines WHERE order_id = ?",
                (row["id"],),
            )
        return {
            "id": row["id"],
            "number": row["number"],
            "customer_id": row["customer_id"],
            "phone": row["phone"],
            "status": row["status"],
            "financial_status": row["financial_status"],
            "total_cents": row["total_cents"],
            "currency": row["currency"],
            "idempotency_key": row["idempotency_key"],
            "confirmation_token": row["confirmation_token"],
            "shopify_id": row["shopify_id"],
            "created_at": row["created_at"],
            "lines": lines,
            "duplicate": False,
        }

    def get_order_by_id(self, order_id: str) -> dict | None:
        row = self._one("SELECT * FROM orders WHERE id = ?", (order_id,))
        return self._order_from_row(row) if row else None

    def get_order_by_number(self, number: str) -> dict | None:
        cleaned = number.lstrip("#")
        row = self._one("SELECT * FROM orders WHERE number = ?", (cleaned,))
        return self._order_from_row(row) if row else None

    def find_order_for_shopify(self, *, number: str = "", shopify_id: str = "") -> dict | None:
        if number:
            found = self.get_order_by_number(number)
            if found:
                return found
        token = (shopify_id or "").strip()
        if not token:
            return None
        suffix = token.rsplit("/", 1)[-1]
        row = self._one(
            "SELECT * FROM orders WHERE shopify_id = ? OR shopify_id = ? "
            "OR shopify_id LIKE ? ORDER BY created_at DESC LIMIT 1",
            (token, suffix, f"%/{suffix}"),
        )
        return self._order_from_row(row) if row else None

    def update_order_progress(
        self,
        order_id: str,
        *,
        status: str | None = None,
        financial_status: str | None = None,
    ) -> dict | None:
        current = self.get_order_by_id(order_id)
        if current is None:
            return None
        next_status = status or current["status"]
        next_financial = financial_status or current["financial_status"]
        with self._lock:
            self._conn.execute(
                "UPDATE orders SET status = ?, financial_status = ? WHERE id = ?",
                (next_status, next_financial, order_id),
            )
        return self.get_order_by_id(order_id)

    def orders_for_phone(self, phone: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM orders WHERE phone = ? ORDER BY created_at DESC, number DESC",
            (phone,),
        )
        return [self._order_from_row(row) for row in rows]

    def get_idempotency(self, key: str) -> dict | None:
        row = self._one("SELECT * FROM idempotency WHERE key = ?", (key,))
        if not row:
            return None
        order = self.get_order_by_id(row["order_id"])
        if order:
            order["duplicate"] = True
        return order

    def recent_matching_order(self, phone: str, canonical: str, within_minutes: int) -> dict | None:
        cutoff = iso(utcnow() - timedelta(minutes=within_minutes))
        orders = self._all(
            "SELECT * FROM orders WHERE phone = ? AND created_at >= ? ORDER BY created_at DESC",
            (phone, cutoff),
        )
        for row in orders:
            order = self._order_from_row(row)
            if canonical_lines(order["lines"]) == canonical:
                order["duplicate"] = True
                return order
        return None

    def create_order(
        self,
        *,
        id: str,
        customer_id: str,
        phone: str,
        lines: list[dict],
        currency: str,
        idempotency_key: str,
        confirmation_token: str | None,
        decrement_inventory: bool,
        shopify_id: str | None = None,
        number: str | None = None,
        status: str = "pending",
        financial_status: str = "pending",
    ) -> dict:
        total = sum(int(line["quantity"]) * int(line["unit_price_cents"]) for line in lines)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT order_id FROM idempotency WHERE key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    self._conn.execute("COMMIT")
                    order = self.get_order_by_id(existing["order_id"])
                    assert order is not None
                    order["duplicate"] = True
                    return order
                if decrement_inventory:
                    for line in lines:
                        row = self._conn.execute(
                            "SELECT inventory_qty FROM variants WHERE id = ?",
                            (line["variant_id"],),
                        ).fetchone()
                        if row is None or row["inventory_qty"] < int(line["quantity"]):
                            raise StoreError(
                                "insufficient_stock",
                                "Stock changed before the order was placed.",
                            )
                    for line in lines:
                        self._conn.execute(
                            "UPDATE variants SET inventory_qty = inventory_qty - ? WHERE id = ?",
                            (int(line["quantity"]), line["variant_id"]),
                        )
                if number is None:
                    current = self._conn.execute(
                        "SELECT MAX(CAST(number AS INTEGER)) AS n FROM orders"
                    ).fetchone()["n"]
                    number = str((current or 10420) + 1)
                self._conn.execute(
                    "INSERT INTO orders("
                    "id, number, customer_id, phone, status, financial_status, total_cents, "
                    "currency, idempotency_key, confirmation_token, shopify_id, created_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        id,
                        number,
                        customer_id,
                        phone,
                        status,
                        financial_status,
                        total,
                        currency,
                        idempotency_key,
                        confirmation_token,
                        shopify_id,
                        iso(),
                    ),
                )
                for line in lines:
                    self._conn.execute(
                        "INSERT INTO order_lines(order_id, variant_id, title, quantity, unit_price_cents) "
                        "VALUES(?, ?, ?, ?, ?)",
                        (
                            id,
                            line["variant_id"],
                            line["title"],
                            int(line["quantity"]),
                            int(line["unit_price_cents"]),
                        ),
                    )
                self._conn.execute(
                    "INSERT INTO idempotency(key, order_id, created_at) VALUES(?, ?, ?)",
                    (idempotency_key, id, iso()),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        order = self.get_order_by_id(id)
        assert order is not None
        return order

    def create_draft(
        self,
        *,
        id: str,
        phone: str,
        token: str,
        customer_id: str | None,
        lines: list[dict],
        total_cents: int,
        currency: str,
        created_turn_id: str,
        ttl_minutes: int,
        shopify_draft_id: str = "",
    ) -> dict:
        with self._lock:
            self._conn.execute(
                "UPDATE drafts SET status = 'cancelled' WHERE phone = ? AND status = 'pending'",
                (phone,),
            )
            self._conn.execute(
                "INSERT INTO drafts("
                "id, phone, token, customer_id, lines_json, total_cents, currency, status, "
                "created_turn_id, created_at, expires_at, shopify_draft_id"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    id,
                    phone,
                    token,
                    customer_id,
                    json.dumps(lines),
                    total_cents,
                    currency,
                    created_turn_id,
                    iso(),
                    iso(utcnow() + timedelta(minutes=ttl_minutes)),
                    shopify_draft_id or "",
                ),
            )
        draft = self.pending_draft(phone)
        assert draft is not None
        return draft

    def pending_draft(self, phone: str) -> dict | None:
        row = self._one(
            "SELECT * FROM drafts WHERE phone = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            (phone,),
        )
        if not row:
            return None
        if row["expires_at"] < iso():
            with self._lock:
                self._conn.execute(
                    "UPDATE drafts SET status = 'expired' WHERE id = ?",
                    (row["id"],),
                )
            return None
        return self._draft(row)

    def get_draft(self, draft_id: str) -> dict | None:
        row = self._one("SELECT * FROM drafts WHERE id = ?", (draft_id,))
        return self._draft(row) if row else None

    def set_draft_status(self, draft_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE drafts SET status = ? WHERE id = ?", (status, draft_id))

    def _draft(self, row: dict) -> dict:
        return {
            "id": row["id"],
            "phone": row["phone"],
            "token": row["token"],
            "customer_id": row["customer_id"],
            "lines": json.loads(row["lines_json"]),
            "total_cents": row["total_cents"],
            "currency": row["currency"],
            "status": row["status"],
            "created_turn_id": row["created_turn_id"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "shopify_draft_id": row["shopify_draft_id"] or "",
        }

    def add_message(self, phone: str, role: str, content: str, turn_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages(phone, role, content, turn_id, created_at) VALUES(?, ?, ?, ?, ?)",
                (phone, role, content, turn_id, iso()),
            )

    def history(self, phone: str, limit: int = 12) -> list[dict]:
        rows = self._all(
            "SELECT role, content, turn_id, created_at FROM messages "
            "WHERE phone = ? ORDER BY id DESC LIMIT ?",
            (phone, limit),
        )
        rows.reverse()
        return rows

    def get_conversation(self, phone: str) -> dict:
        row = self._one("SELECT * FROM conversations WHERE phone = ?", (phone,))
        if row:
            return row
        return {"phone": phone, "escalated": 0, "escalation_id": None, "customer_name": ""}

    def set_conversation(
        self,
        phone: str,
        *,
        escalated: int | None = None,
        escalation_id: str | None = None,
        customer_name: str | None = None,
    ) -> dict:
        current = self.get_conversation(phone)
        name = current["customer_name"] if customer_name is None else customer_name
        flag = current["escalated"] if escalated is None else escalated
        ticket = current["escalation_id"] if escalation_id is None else escalation_id
        with self._lock:
            self._conn.execute(
                "INSERT INTO conversations(phone, escalated, escalation_id, customer_name) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(phone) DO UPDATE SET "
                "escalated = excluded.escalated, escalation_id = excluded.escalation_id, "
                "customer_name = excluded.customer_name",
                (phone, flag, ticket, name),
            )
        return self.get_conversation(phone)

    def create_escalation(self, *, id: str, phone: str, reason: str) -> dict:
        with self._lock:
            self._conn.execute(
                "INSERT INTO escalations(id, phone, reason, status, created_at) VALUES(?, ?, ?, 'open', ?)",
                (id, phone, reason, iso()),
            )
        self.set_conversation(phone, escalated=1, escalation_id=id)
        created = self._one("SELECT * FROM escalations WHERE id = ?", (id,))
        assert created is not None
        return created

    def open_escalation(self, phone: str) -> dict | None:
        return self._one(
            "SELECT * FROM escalations WHERE phone = ? AND status = 'open' ORDER BY created_at DESC LIMIT 1",
            (phone,),
        )

    def list_escalations(self, phone: str | None = None) -> list[dict]:
        if phone:
            return self._all(
                "SELECT * FROM escalations WHERE phone = ? ORDER BY created_at DESC",
                (phone,),
            )
        return self._all("SELECT * FROM escalations ORDER BY created_at DESC")

    def resolve_escalation(self, escalation_id: str) -> dict | None:
        row = self._one("SELECT * FROM escalations WHERE id = ?", (escalation_id,))
        if not row:
            return None
        with self._lock:
            self._conn.execute(
                "UPDATE escalations SET status = 'resolved', resolved_at = ? WHERE id = ?",
                (iso(), escalation_id),
            )
        self.set_conversation(row["phone"], escalated=0, escalation_id=None)
        return self._one("SELECT * FROM escalations WHERE id = ?", (escalation_id,))

    def audit(self, phone: str | None, action: str, outcome: str, detail: dict | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log(ts, phone, action, outcome, detail_json) VALUES(?, ?, ?, ?, ?)",
                (iso(), phone, action, outcome, json.dumps(detail or {}, default=str)),
            )

    def list_audit(self, phone: str | None = None, limit: int = 20) -> list[dict]:
        if phone:
            rows = self._all(
                "SELECT * FROM audit_log WHERE phone = ? ORDER BY id DESC LIMIT ?",
                (phone, limit),
            )
        else:
            rows = self._all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["detail"] = json.loads(row.pop("detail_json"))
        return rows

    def seen_inbound(self, message_id: str) -> bool:
        return self._one(
            "SELECT message_id FROM inbound_messages WHERE message_id = ?",
            (message_id,),
        ) is not None

    def remember_inbound(self, message_id: str, phone: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO inbound_messages(message_id, phone, received_at) VALUES(?, ?, ?)",
                (message_id, phone, iso()),
            )

    def replace_chunks(self, chunks: list[dict]) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM kb_chunks")
                for chunk in chunks:
                    self._conn.execute(
                        "INSERT INTO kb_chunks(id, title, text, source, kind) VALUES(?, ?, ?, ?, ?)",
                        (chunk["id"], chunk["title"], chunk["text"], chunk["source"], chunk["kind"]),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def list_chunks(self) -> list[dict]:
        return self._all("SELECT * FROM kb_chunks ORDER BY id")


def canonical_lines(lines: list[dict]) -> str:
    rows = sorted((line["variant_id"], int(line["quantity"])) for line in lines)
    return json.dumps(rows)

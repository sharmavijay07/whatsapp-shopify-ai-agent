"""Best-effort events for the n8n workflows shipped in n8n/workflows.

A missed webhook never rolls back an order. The audit log records the skip.
"""

from __future__ import annotations

import httpx

from harbor.config import Settings
from harbor.db import Database


class N8NClient:
    def __init__(self, settings: Settings, db: Database, client: httpx.Client | None = None):
        self.settings = settings
        self.db = db
        self._client = client or httpx.Client(timeout=5.0)

    def close(self) -> None:
        self._client.close()

    def order_created(self, payload: dict) -> None:
        self._post("order.created", self.settings.n8n_order_webhook_url, payload)

    def notify(self, payload: dict) -> None:
        self._post("notification", self.settings.n8n_notify_webhook_url, payload)

    def error(self, payload: dict) -> None:
        self._post("error", self.settings.n8n_error_webhook_url, payload)

    def _post(self, event: str, url: str, payload: dict) -> None:
        phone = payload.get("verified_phone") or payload.get("phone")
        body = {"event": event, **payload}
        if not url.strip():
            self.db.audit(phone, "n8n.skipped", "skipped", {"event": event})
            return
        headers = {"Content-Type": "application/json"}
        if self.settings.n8n_shared_secret:
            headers["X-Harbor-Secret"] = self.settings.n8n_shared_secret
        try:
            response = self._client.post(url, json=body, headers=headers)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — delivery must not fail the order
            self.db.audit(
                phone,
                "n8n.failed",
                "error",
                {"event": event, "error": str(exc)},
            )
            return
        self.db.audit(phone, "n8n.delivered", "success", {"event": event})

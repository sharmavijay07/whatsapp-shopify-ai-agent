"""WhatsApp Business Cloud API, with a mock outbox for the sandbox.

Live sends use the Cloud API on Meta's test number: reply buttons for YES/NO,
a list for product choices, and a utility template when Shopify reports an
order change outside the 24-hour session window. Typed YES still confirms,
because the webhook turns a YES button into the same text the parser already
accepts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx

from harbor.config import Settings
from harbor.db import Database

CONFIRM_YES = "confirm_yes"
CONFIRM_NO = "confirm_no"


def verify_signature(body: bytes, header: str | None, secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    provided = header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


def _clip(text: str, limit: int) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _template_param(value: str) -> str:
    cleaned = " ".join((value or "").replace("\n", " ").split())
    return (cleaned[:200] or "-")


def message_text(message: dict) -> tuple[str, bool]:
    """Return (text, unsupported). Buttons and list replies become plain text."""
    kind = message.get("type")
    if kind == "text":
        return ((message.get("text") or {}).get("body") or "").strip(), False
    if kind == "button":
        return ((message.get("button") or {}).get("text") or "").strip(), False
    if kind == "interactive":
        interactive = message.get("interactive") or {}
        if interactive.get("type") == "button_reply":
            reply = interactive.get("button_reply") or {}
            button_id = (reply.get("id") or "").strip()
            title = (reply.get("title") or "").strip()
            if button_id == CONFIRM_YES or title.upper() == "YES":
                return "YES", False
            if button_id == CONFIRM_NO or title.upper() == "NO":
                return "NO", False
            return title, False
        if interactive.get("type") == "list_reply":
            reply = interactive.get("list_reply") or {}
            chosen = (reply.get("id") or reply.get("title") or "").strip()
            if chosen.startswith("pick:"):
                chosen = chosen[5:]
            if not chosen:
                return "", True
            return f"I'd like {chosen}", False
    return "", True


def extract_messages(payload: dict) -> list[dict]:
    """Pull customer messages out of a Cloud API webhook body."""
    found = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {
                contact.get("wa_id"): (contact.get("profile") or {}).get("name") or ""
                for contact in value.get("contacts") or []
            }
            for message in value.get("messages") or []:
                sender = message.get("from") or ""
                text, unsupported = message_text(message)
                found.append(
                    {
                        "id": message.get("id") or "",
                        "from": sender,
                        "name": names.get(sender, ""),
                        "text": text,
                        "unsupported": unsupported,
                        "type": message.get("type") or "",
                    }
                )
    return found


class WhatsAppClient:
    def __init__(self, settings: Settings, db: Database, client: httpx.Client | None = None):
        self.settings = settings
        self.db = db
        self._client = client or httpx.Client(timeout=20.0)

    def close(self) -> None:
        self._client.close()

    def send_text(self, to: str, body: str) -> dict[str, Any]:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to.lstrip("+"),
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }
        return self._send(to, payload, body=body)

    def send_reply_buttons(self, to: str, body: str) -> dict[str, Any]:
        """YES / NO reply buttons. A typed YES is still accepted by the agent."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to.lstrip("+"),
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": _clip(body, 1024)},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": CONFIRM_YES, "title": "YES"}},
                        {"type": "reply", "reply": {"id": CONFIRM_NO, "title": "NO"}},
                    ]
                },
            },
        }
        return self._send(to, payload, body=body)

    def send_product_list(self, to: str, body: str, rows: list[dict]) -> dict[str, Any]:
        """List message. Each row needs id, title, and an optional description."""
        usable = []
        for row in rows[:10]:
            title = _clip(str(row.get("title") or ""), 24)
            row_id = _clip(str(row.get("id") or title), 200)
            if not title or not row_id:
                continue
            item = {"id": row_id, "title": title}
            description = _clip(str(row.get("description") or ""), 72)
            if description:
                item["description"] = description
            usable.append(item)
        if len(usable) < 2:
            return self.send_text(to, body)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to.lstrip("+"),
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": _clip(body, 1024)},
                "action": {
                    "button": "Choose",
                    "sections": [{"title": "Products", "rows": usable}],
                },
            },
        }
        return self._send(to, payload, body=body)

    def send_template(
        self,
        to: str,
        template_name: str,
        parameters: list[str],
        language: str | None = None,
    ) -> dict[str, Any]:
        """Utility template. Use this outside the 24-hour customer-care window."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to.lstrip("+"),
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language or self.settings.whatsapp_template_language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": _template_param(value)} for value in parameters
                        ],
                    }
                ],
            },
        }
        return self._send(to, payload, body=" ".join(parameters))

    def _send(self, to: str, payload: dict[str, Any], *, body: str) -> dict[str, Any]:
        kind = payload.get("type") or "text"
        if not self.settings.whatsapp_is_live:
            self.db.audit(
                to,
                "whatsapp.send",
                "mock",
                {"to": to, "body": body, "mock": True, "type": kind, "payload": payload},
            )
            return {"mock": True, "to": to, "body": body, "type": kind, "payload": payload}
        url = (
            f"https://graph.facebook.com/{self.settings.whatsapp_api_version}/"
            f"{self.settings.whatsapp_phone_number_id}/messages"
        )
        response = self._client.post(
            url,
            headers={
                "Authorization": f"Bearer {self.settings.whatsapp_token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        self.db.audit(
            to,
            "whatsapp.send",
            "success",
            {"to": to, "type": kind, "response": data},
        )
        return data


def dumps_payload(payload: dict) -> bytes:
    return json.dumps(payload).encode()

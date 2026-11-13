"""WhatsApp Business Cloud API, with a mock outbox for the sandbox."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx

from harbor.config import Settings
from harbor.db import Database


def verify_signature(body: bytes, header: str | None, secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    provided = header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


def extract_messages(payload: dict) -> list[dict]:
    """Pull text messages out of a Cloud API webhook body."""
    found = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {
                contact.get("wa_id"): (contact.get("profile") or {}).get("name") or ""
                for contact in value.get("contacts") or []
            }
            for message in value.get("messages") or []:
                if message.get("type") != "text":
                    found.append(
                        {
                            "id": message.get("id") or "",
                            "from": message.get("from") or "",
                            "name": names.get(message.get("from"), ""),
                            "text": "",
                            "unsupported": True,
                        }
                    )
                    continue
                sender = message.get("from") or ""
                found.append(
                    {
                        "id": message.get("id") or "",
                        "from": sender,
                        "name": names.get(sender, ""),
                        "text": ((message.get("text") or {}).get("body") or "").strip(),
                        "unsupported": False,
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
        if not self.settings.whatsapp_is_live:
            self.db.audit(
                to,
                "whatsapp.send",
                "mock",
                {"to": to, "body": body, "mock": True},
            )
            return {"mock": True, "to": to, "body": body}
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
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to.lstrip("+"),
                "type": "text",
                "text": {"preview_url": False, "body": body},
            },
        )
        response.raise_for_status()
        data = response.json()
        self.db.audit(to, "whatsapp.send", "success", {"to": to, "response": data})
        return data


def dumps_payload(payload: dict) -> bytes:
    return json.dumps(payload).encode()

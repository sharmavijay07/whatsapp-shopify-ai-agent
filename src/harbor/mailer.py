"""Gmail SMTP for the shop owner.

smtp.gmail.com:465 with an app password. When GMAIL_USER or
GMAIL_APP_PASSWORD is empty, nothing is sent: the message is written to the
audit log so the sandbox still shows what would have gone out.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Callable

from harbor.config import Settings
from harbor.db import Database

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


class Mailer:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        smtp_factory: Callable[..., smtplib.SMTP_SSL] | None = None,
    ):
        self.settings = settings
        self.db = db
        self._smtp_factory = smtp_factory or smtplib.SMTP_SSL

    def close(self) -> None:
        return None

    def order_confirmation(
        self,
        *,
        phone: str,
        number: str,
        total: str,
        customer_email: str = "",
        customer_name: str = "",
        invoice_sent: bool | None = None,
    ) -> dict:
        invoice_line = "Payment is pending. This agent does not capture cards."
        if invoice_sent is True:
            invoice_line = "Shopify was asked to email the invoice. Payment is still pending."
        elif invoice_sent is False:
            invoice_line = "No customer email was on the order, so Shopify was not asked to send an invoice."
        body = (
            f"Harbor & Co. order #{number} is in the shop for {customer_name or phone}.\n"
            f"Phone: {phone}\n"
            f"Total: {total}\n"
            f"{invoice_line}\n"
        )
        recipients = self._recipients(customer_email)
        return self._send(
            phone=phone,
            kind="order_confirmation",
            recipients=recipients,
            subject=f"Harbor & Co. order #{number}",
            body=body,
        )

    def handoff_alert(self, *, phone: str, reason: str, escalation_id: str) -> dict:
        owner = self.settings.owner_email.strip()
        body = (
            f"A WhatsApp customer asked for a person.\n"
            f"Phone: {phone}\n"
            f"Reference: {escalation_id}\n"
            f"Message: {reason[:500]}\n"
            "Automated orders are paused on this number until the handoff is closed.\n"
        )
        return self._send(
            phone=phone,
            kind="handoff",
            recipients=[owner] if owner else [],
            subject=f"Harbor & Co. handoff {escalation_id}",
            body=body,
        )

    def status_update(
        self,
        *,
        phone: str,
        number: str,
        status_label: str,
        detail: str,
        customer_email: str = "",
    ) -> dict:
        body = (
            f"Harbor & Co. order #{number} changed.\n"
            f"Status: {status_label}\n"
            f"{detail}\n"
            f"Phone: {phone or '(unknown)'}\n"
        )
        return self._send(
            phone=phone,
            kind="order_status",
            recipients=self._recipients(customer_email),
            subject=f"Harbor & Co. order #{number} is {status_label}",
            body=body,
        )

    def _recipients(self, customer_email: str) -> list[str]:
        found: list[str] = []
        owner = self.settings.owner_email.strip()
        if owner:
            found.append(owner)
        customer = (customer_email or "").strip()
        if customer and customer.lower() not in {item.lower() for item in found}:
            found.append(customer)
        return found

    def _send(
        self,
        *,
        phone: str,
        kind: str,
        recipients: list[str],
        subject: str,
        body: str,
    ) -> dict:
        if not recipients:
            self.db.audit(
                phone or None,
                "email.skipped",
                "skipped",
                {"kind": kind, "reason": "no recipient"},
            )
            return {"demo": True, "kind": kind, "recipients": []}
        if not self.settings.email_is_configured:
            self.db.audit(
                phone or None,
                "email.demo",
                "skipped",
                {"kind": kind, "to": recipients, "subject": subject, "body": body},
            )
            return {"demo": True, "kind": kind, "recipients": recipients, "subject": subject}
        message = EmailMessage()
        message["From"] = self.settings.gmail_user.strip()
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message.set_content(body)
        password = self.settings.gmail_app_password.replace(" ", "")
        try:
            with self._smtp_factory(
                SMTP_HOST,
                SMTP_PORT,
                context=ssl.create_default_context(),
                timeout=20,
            ) as smtp:
                smtp.login(self.settings.gmail_user.strip(), password)
                smtp.send_message(message)
        except Exception as exc:  # noqa: BLE001 — a mail failure must not roll back an order
            self.db.audit(
                phone or None,
                "email.failed",
                "error",
                {"kind": kind, "to": recipients, "error": str(exc)[:300]},
            )
            return {"demo": False, "ok": False, "kind": kind, "error": str(exc)[:300]}
        self.db.audit(
            phone or None,
            "email.sent",
            "success",
            {"kind": kind, "to": recipients, "subject": subject},
        )
        return {"demo": False, "ok": True, "kind": kind, "recipients": recipients}

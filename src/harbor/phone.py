"""Phone numbers are the customer's identity. The model never supplies one."""

from __future__ import annotations

import re


class PhoneError(ValueError):
    pass


def normalize_phone(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise PhoneError("Phone number is empty.")
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    # Demo convenience: a 10-digit US number gets a country code.
    # Numbers that already include a country code are left alone.
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) < 11 or len(digits) > 15:
        raise PhoneError("That doesn't look like a phone number.")
    return f"+{digits}"


def format_phone(phone: str) -> str:
    digits = phone.lstrip("+")
    if digits.startswith("1") and len(digits) == 11:
        return f"+1 {digits[1:4]} {digits[4:7]} {digits[7:]}"
    return phone

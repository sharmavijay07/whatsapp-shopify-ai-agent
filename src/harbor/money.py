"""Integer cents, so totals cannot drift."""

from __future__ import annotations


def format_money(cents: int, currency: str = "USD") -> str:
    sign = "-" if cents < 0 else ""
    amount = abs(int(cents))
    formatted = f"{sign}${amount // 100:,}.{amount % 100:02d}"
    if currency and currency != "USD":
        return f"{formatted} {currency}"
    return formatted

"""Intent and line-item parsing.

These decisions are made in code from the customer's own message. Tool
arguments from the language model cannot override them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SIZE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(small|size\s*s|in\s+s)\b", re.I), "S"),
    (re.compile(r"\b(medium|med|size\s*m|in\s+m)\b", re.I), "M"),
    (re.compile(r"\b(large|size\s*l|in\s+l)\b", re.I), "L"),
]

NUMBER_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

EXACT_CONFIRM = {
    "yes",
    "yep",
    "yeah",
    "yup",
    "confirm",
    "confirmed",
    "i confirm",
    "yes i confirm",
    "yes, i confirm",
    "yes, confirm",
    "yes please",
    "yes, please",
    "please confirm",
    "place the order",
    "place my order",
    "go ahead",
    "do it",
    "that's correct",
    "thats correct",
    "that is correct",
    "ok confirm",
    "okay confirm",
}

NEW_ORDER = re.compile(
    r"\b(buy|purchase|i'll take|i’ll take|i'd like|i’d like|i would like|i want|"
    r"want to order|order\s+\d|order\s+the|order\s+a|order\s+an|order\s+me|get me)\b",
    re.I,
)
CANCEL = re.compile(
    r"^(no|nope|nah|cancel|stop|never mind|nevermind)[\s!.]*$"
    r"|\b(don't place|do not place|cancel (that|the|my) (order|draft)|never mind)\b",
    re.I,
)
ESCALATE = re.compile(
    r"\b(human|real person|talk to (?:a )?(?:person|someone|human)|speak to|"
    r"representative|customer service|escalate|manager)\b",
    re.I,
)
STATUS = re.compile(
    r"\b(where(?:'s| is) my order|order status|track(?:ing)?|"
    r"my orders|status of (?:my )?order|look up (?:my )?orders?)\b",
    re.I,
)
RECOMMEND = re.compile(
    r"\b(recommend|suggestion|suggest|gift|under\s*\$|what should i)\b",
    re.I,
)
AVAILABILITY = re.compile(
    r"\b(in stock|out of stock|available|availability|do you have|have you got)\b",
    re.I,
)
HELP = re.compile(
    r"\b(what can you (?:help|do)|how can you help|what do you do|who are you)\b",
    re.I,
)
GREETING = re.compile(
    r"^(hi|hello|hey|good morning|good afternoon|good evening)\b",
    re.I,
)
ANOTHER = re.compile(r"\banother order\b", re.I)
POLICY = re.compile(
    r"\b(return|refund|shipping|shipment|exchange|policy|wholesale|gift wrap)\b",
    re.I,
)
PRICE_ASK = re.compile(
    r"\b(how much|price|cost|what is the|what's the|tell me about|describe)\b",
    re.I,
)
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
PHONE_LIKE = re.compile(
    r"(?:\+\d[\d\s().-]{8,}\d)|(?:\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b)"
)
FILLER = re.compile(r"\b(of|the|a|an|those|some|me|please|just|for)\b", re.I)


def normalize_phrase(text: str) -> str:
    return " ".join(text.strip().lower().replace("’", "'").split())


def looks_like_new_order(text: str) -> bool:
    return bool(NEW_ORDER.search(text))


def is_cancel(text: str) -> bool:
    return bool(CANCEL.search(text.strip()))


def allows_duplicate_override(text: str) -> bool:
    return bool(ANOTHER.search(text))


def is_explicit_confirmation(text: str, token: str | None = None) -> bool:
    """A clear yes on its own, or the draft token. Not a new purchase request."""
    if looks_like_new_order(text) and not allows_duplicate_override(text):
        return False
    phrase = normalize_phrase(text).rstrip(".!")
    if phrase in EXACT_CONFIRM:
        return True
    if allows_duplicate_override(text) and re.search(r"\b(place|yes|confirm)\b", text, re.I):
        return True
    if token and re.search(rf"\b{re.escape(token)}\b", text, re.I):
        return True
    if (
        re.match(r"^(yes|yep|yeah|confirm|confirmed)\b", phrase)
        and "confirm" in phrase
        and len(phrase) < 80
    ):
        return True
    return False


def wrong_token(text: str, expected: str) -> bool:
    match = re.search(r"\b(?:token|code)\s+([A-Za-z0-9]{4})\b", text, re.I)
    if not match:
        return False
    return match.group(1).upper() != expected.upper()


def order_number_in(text: str) -> str | None:
    match = re.search(
        r"#\s*(\d{3,})\b|\border\s+#?\s*(\d{4,})\b|\b(\d{5,})\b",
        text,
        re.I,
    )
    if not match:
        return None
    return next(group for group in match.groups() if group)


def budget_cents(text: str) -> int | None:
    match = re.search(r"under\s*\$?\s*(\d{1,5})", text, re.I)
    if not match:
        match = re.search(r"\$\s*(\d{1,5})\s*(?:or less|budget|max)", text, re.I)
    if not match:
        return None
    return int(match.group(1)) * 100


def email_in(text: str) -> str:
    match = EMAIL.search(text or "")
    return match.group(0) if match else ""


def foreign_phones(text: str, session_phone: str) -> list[str]:
    from harbor.phone import PhoneError, normalize_phone

    found: list[str] = []
    for match in PHONE_LIKE.finditer(text or ""):
        try:
            number = normalize_phone(match.group(0))
        except PhoneError:
            continue
        if number != session_phone and number not in found:
            found.append(number)
    return found


def classify(text: str, *, has_pending: bool, token: str | None) -> str:
    if has_pending and is_cancel(text) and not looks_like_new_order(text):
        return "cancel"
    if is_explicit_confirmation(text, token):
        return "confirm"
    if ESCALATE.search(text):
        return "escalate"
    if STATUS.search(text) or order_number_in(text):
        return "status"
    if RECOMMEND.search(text):
        return "recommend"
    if AVAILABILITY.search(text):
        return "availability"
    if looks_like_new_order(text):
        return "order"
    if HELP.search(text) or (GREETING.match(text.strip()) and len(text) < 48):
        return "help"
    return "faq"


@dataclass
class ItemRequest:
    product: dict
    variant: dict | None
    quantity: int
    needs_size: bool = False
    options: list[str] = field(default_factory=list)


def _alias_span(text: str, alias: str) -> tuple[int, int] | None:
    match = re.search(rf"\b{re.escape(alias)}s?\b", text, re.I)
    if not match:
        return None
    return match.start(), match.end()


def _quantity_before(window: str) -> int:
    cleaned = FILLER.sub(" ", window.lower())
    cleaned = " ".join(cleaned.split())
    match = re.search(r"(\d+)\s*(?:x)?$", cleaned)
    if match:
        return max(1, int(match.group(1)))
    match = re.search(
        r"\b(a|an|one|two|three|four|five|six|seven|eight|nine|ten)$",
        cleaned,
    )
    if match:
        return NUMBER_WORDS[match.group(1)]
    return 1


def _size_in(window: str) -> str | None:
    for pattern, code in SIZE_PATTERNS:
        if pattern.search(window):
            return code
    return None


def parse_items(text: str, products: list[dict]) -> list[ItemRequest]:
    candidates: list[tuple[int, str, dict]] = []
    for product in products:
        aliases = product.get("aliases") or [product["title"]]
        for alias in aliases:
            candidates.append((len(alias), alias, product))
    candidates.sort(key=lambda item: -item[0])

    claimed: set[str] = set()
    spans: list[tuple[int, int, dict]] = []
    for _length, alias, product in candidates:
        if product["id"] in claimed:
            continue
        span = _alias_span(text, alias)
        if span is None:
            continue
        if any(not (span[1] <= start or span[0] >= end) for start, end, _ in spans):
            continue
        claimed.add(product["id"])
        spans.append((span[0], span[1], product))

    requests: list[ItemRequest] = []
    sized = [product for _, _, product in spans if len(product["variants"]) > 1]
    for start, end, product in spans:
        window = text[max(0, start - 48) : end + 48]
        quantity = _quantity_before(text[max(0, start - 40) : start])
        variants = product["variants"]
        if len(variants) == 1:
            requests.append(ItemRequest(product, variants[0], quantity, False))
            continue
        size = _size_in(window)
        if size is None and len(sized) == 1:
            size = _size_in(text)
        match = next((variant for variant in variants if variant["title"].upper() == size), None) if size else None
        if match is None:
            requests.append(
                ItemRequest(
                    product,
                    None,
                    quantity,
                    True,
                    [variant["title"] for variant in variants],
                )
            )
        else:
            requests.append(ItemRequest(product, match, quantity, False))
    return requests

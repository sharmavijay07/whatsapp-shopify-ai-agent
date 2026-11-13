"""Short ids. Kept in their own module so the store and the agent can share them."""

from __future__ import annotations

import secrets
import uuid

_TOKEN = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def confirmation_token() -> str:
    return "".join(secrets.choice(_TOKEN) for _ in range(4))

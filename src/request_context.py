from __future__ import annotations

import hashlib
import time
from contextvars import ContextVar, Token

_PURCHASE_REQUEST_ID: ContextVar[str] = ContextVar("purchase_request_id", default="")


def generate_purchase_request_id(discord_user_id: str) -> str:
    seed = f"{discord_user_id}|{time.time_ns()}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def set_purchase_request_id(value: str) -> Token[str]:
    return _PURCHASE_REQUEST_ID.set(str(value or "").strip())


def reset_purchase_request_id(token: Token[str]) -> None:
    _PURCHASE_REQUEST_ID.reset(token)


def get_purchase_request_id() -> str:
    return str(_PURCHASE_REQUEST_ID.get() or "").strip()

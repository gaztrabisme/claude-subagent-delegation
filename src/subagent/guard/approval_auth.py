"""Small, standard-library authentication helpers for approval socket messages."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

_NONCE = re.compile(r"^[0-9a-fA-F]{64}$")


def canonical_bytes(obj: dict[str, Any]) -> bytes:
    """Serialize an object using the approval socket's fixed wire encoding."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def sign(key: bytes, obj: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``obj`` signed with HMAC-SHA256, without mutating it."""
    unsigned = dict(obj)
    unsigned.pop("mac", None)
    mac = hmac.new(key, canonical_bytes(unsigned), hashlib.sha256).hexdigest()
    return {**unsigned, "mac": mac}


def verify(key: bytes, obj: dict[str, Any]) -> bool:
    """Verify an object's HMAC in constant time."""
    if not isinstance(obj, dict):
        return False
    supplied = obj.get("mac")
    if not isinstance(supplied, str):
        return False
    unsigned = dict(obj)
    unsigned.pop("mac", None)
    try:
        expected = hmac.new(key, canonical_bytes(unsigned), hashlib.sha256).hexdigest()
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, supplied)


def valid_nonce(value: object) -> bool:
    """Return whether a nonce is exactly 64 hexadecimal characters."""
    return isinstance(value, str) and _NONCE.fullmatch(value) is not None

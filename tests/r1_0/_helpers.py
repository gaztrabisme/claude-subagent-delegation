"""Shared offline helpers for the R1-0 acceptance suite."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from typing import Any

from subagent.guard.supervisor import Supervisor

REPO_ROOT = Path(__file__).resolve().parents[2]
APPROVAL_HOOK = REPO_ROOT / "src" / "subagent" / "guard" / "approval_hook.py"


def canonical_bytes(obj: dict[str, Any]) -> bytes:
    """Canonical JSON encoding specified by the R1-0 wire protocol."""
    body = {k: v for k, v in obj.items() if k != "mac"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def wire_sign(key: bytes, obj: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `obj` with a wire-protocol HMAC-SHA256 `mac`."""
    out = dict(obj)
    out["mac"] = hmac.new(key, canonical_bytes(out), hashlib.sha256).hexdigest()
    return out


def wire_verify(key: bytes, obj: dict[str, Any]) -> bool:
    """Constant-time verification against the wire-protocol HMAC-SHA256."""
    mac = obj.get("mac")
    if not isinstance(mac, str):
        return False
    expected = hmac.new(key, canonical_bytes(obj), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, expected)


class _MemoryStream:
    """Minimal async stream for exercising `Supervisor._handle` without a socket."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._chunks = [(json.dumps(payload) + "\n").encode("utf-8"), b""]
        self.sent = bytearray()

    async def __aenter__(self) -> _MemoryStream:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def receive(self, _max_bytes: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    async def send(self, data: bytes) -> None:
        self.sent.extend(data)

    def reply(self) -> dict[str, Any]:
        return json.loads(self.sent.decode("utf-8"))


def send_guard_request(supervisor: Supervisor, payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke `Supervisor._handle` directly over an in-memory stream."""
    stream = _MemoryStream(payload)
    asyncio.run(supervisor._handle(stream))
    return stream.reply()


def sbpl_allows_write(profile: str, target: Path) -> bool:
    """Evaluate macOS Seatbelt `(allow|deny) file-write*` rules in order."""
    target_strs = {
        str(target),
        os.path.abspath(target),
        str(target.resolve()),
    }
    allowed = "(allow default)" in profile
    rule_re = re.compile(r'^\((allow|deny)\s+file-write\*\s+(.+)\)$')
    filter_re = re.compile(r'\((subpath|literal)\s+"((?:[^"\\]|\\.)*)"\)')

    def _unescape(raw: str) -> str:
        return (
            raw.replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )

    def _matches(kind: str, raw_path: str) -> bool:
        rule_path = _unescape(raw_path).rstrip("/") or "/"
        for candidate in target_strs:
            cand = candidate.rstrip("/") or "/"
            if kind == "literal" and cand == rule_path:
                return True
            if kind == "subpath" and (
                rule_path == "/" or cand == rule_path or cand.startswith(rule_path + "/")
            ):
                return True
        return False

    for raw_line in profile.splitlines():
        match = rule_re.match(raw_line.strip())
        if not match:
            continue
        decision, body = match.group(1), match.group(2)
        for kind, raw_path in filter_re.findall(body):
            if _matches(kind, raw_path):
                allowed = decision == "allow"
    return allowed

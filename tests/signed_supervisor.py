"""Shared signed fake supervisor helpers for approval-hook and bridge tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    from subagent.guard.approval_auth import sign, valid_nonce, verify
except ModuleNotFoundError:
    # Pre-R1-0 base commits lack subagent.guard.approval_auth; keep a wire-compatible
    # fallback so base-commit verification runs the tests instead of failing import.
    _NONCE_RE = re.compile(r"^[0-9a-fA-F]{64}$")

    def _canonical_bytes(obj: dict[str, Any]) -> bytes:
        return json.dumps(
            obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")

    def sign(key: bytes, obj: dict[str, Any]) -> dict[str, Any]:
        unsigned = {k: v for k, v in obj.items() if k != "mac"}
        mac = hmac.new(key, _canonical_bytes(unsigned), hashlib.sha256).hexdigest()
        return {**unsigned, "mac": mac}

    def verify(key: bytes, obj: dict[str, Any]) -> bool:
        if not isinstance(obj, dict) or not isinstance(obj.get("mac"), str):
            return False
        unsigned = {k: v for k, v in obj.items() if k != "mac"}
        expected = hmac.new(key, _canonical_bytes(unsigned), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, obj["mac"])

    def valid_nonce(value: object) -> bool:
        return isinstance(value, str) and _NONCE_RE.fullmatch(value) is not None


def write_key_file(path: Path, key: bytes | None = None) -> tuple[bytes, Path, list[str]]:
    """Write a 0600 hex approval key file and return ``(key, path, ['--key-file', str(path)])``."""
    secret = key if key is not None else secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret.hex() + "\n", encoding="utf-8")
    path.chmod(0o600)
    return secret, path, ["--key-file", str(path)]


def sign_request(key: bytes, payload: dict[str, Any], nonce: str | None = None) -> dict[str, Any]:
    """Return a copy of ``payload`` with a 64-hex nonce and HMAC-SHA256 ``mac``."""
    body = dict(payload)
    body.setdefault("nonce", nonce if nonce is not None else secrets.token_hex(32))
    return sign(key, body)


@contextmanager
def private_socket_path(name: str = "a.sock", prefix: str = "sam-sock-"):
    """Yield a short Unix socket path inside a fresh 0700 directory."""
    base = "/tmp" if Path("/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(prefix=prefix, dir=base) as sock_dir:
        os.chmod(sock_dir, 0o700)
        yield Path(sock_dir) / name


@contextmanager
def signed_supervisor_socket(
    sock_path: Path,
    key: bytes,
    responder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    *,
    backlog: int = 8,
):
    """Listen on ``sock_path``, authenticate requests with ``key``, and sign replies."""
    sock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    sock_path.parent.chmod(0o700)
    sock_path.unlink(missing_ok=True)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(backlog)
    server.settimeout(0.1)
    sock_path.chmod(0o600)

    seen: list[dict[str, Any]] = []
    stop = threading.Event()
    decide = responder or (lambda _req: {"action": "allow", "reason": "ok"})

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                chunks: list[bytes] = []
                while b"\n" not in b"".join(chunks):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                raw = b"".join(chunks).split(b"\n", 1)[0]
                if not raw:
                    continue
                try:
                    req = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    req = None
                nonce = req.get("nonce") if isinstance(req, dict) else None
                if not isinstance(req, dict) or not valid_nonce(nonce) or not verify(key, req):
                    bad_reply = sign(
                        key,
                        {
                            "action": "deny",
                            "reason": "denied: invalid approval request",
                            "nonce": nonce if isinstance(nonce, str) else "",
                        },
                    )
                    conn.sendall((json.dumps(bad_reply) + "\n").encode("utf-8"))
                    continue
                seen.append(req)
                verdict = dict(decide(req))
                verdict["nonce"] = nonce
                reply = sign(key, verdict)
                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield seen
    finally:
        stop.set()
        server.close()
        thread.join(timeout=5)
        sock_path.unlink(missing_ok=True)

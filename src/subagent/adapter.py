"""Per-provider request adapters: a small HTTP proxy on 127.0.0.1 between a
child and a backend that cannot take Claude Code's requests as sent.

A provider that sets `adapter` gets its child's ANTHROPIC_BASE_URL pointed at one
of these proxies instead of at the backend. The proxy rewrites the JSON body
of each POST to /v1/messages with the named adapters, in order. fold_system
also handles /v1/messages/count_tokens. Everything else is forwarded untouched:
method, path, query, headers (the key included) and the answer, streamed back
as it arrives. It never logs bodies or headers.

The provider's own base_url stays the backend's, so the health gate and
telemetry keep probing the real host. One proxy per (adapter, upstream) is
started on first use and lives until shutdown().

Adapters:

fold_system  llama.cpp's chat template for Qwen3.8 raises "System message must
             be at the beginning" when a {"role": "system"} entry appears in
             `messages`, and Claude Code 2.1.276 sends one (its environment
             block) after the first user message. Each such entry is removed
             and its text folded into the next user message, or the previous
             one when none follows, or the top-level system when there is no
             user message. The top-level `system` stays first and unchanged.

strip_sampling  Removes generation settings so the model server owns sampling,
                thinking budgets and stop sequences. Pins max_tokens to the
                requested model's max_model_len from /v1/models because oMLX
                requires the field. If discovery fails, keeps the client's value.
"""

from __future__ import annotations

import copy
import http.client
import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("sam")

FOLD_SYSTEM = "fold_system"
STRIP_SAMPLING = "strip_sampling"

_SAMPLING_KEYS = frozenset({
    "temperature", "top_p", "top_k", "min_p", "max_tokens", "stop_sequences", "seed",
    "presence_penalty", "frequency_penalty", "repetition_penalty", "thinking",
})

_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
               "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


def _text_blocks(content: Any) -> list[dict[str, Any]]:
    """A system message's content as Anthropic text blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: list[dict[str, Any]] = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text":
            blocks.append({"type": "text", "text": str(block.get("text") or "")})
        elif isinstance(block, str):
            blocks.append({"type": "text", "text": block})
        elif block is not None:
            blocks.append({"type": "text", "text": json.dumps(block)})
    return [b for b in blocks if b["text"]]


def _as_list(content: Any) -> list[Any]:
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return []


def fold_system(body: dict[str, Any]) -> dict[str, Any]:
    """`body` with every system-role entry of `messages` folded into a user turn.

    Returns the body unchanged (the same object) when there is nothing to fold.
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not any(
        isinstance(m, dict) and m.get("role") == "system" for m in messages
    ):
        return body
    out = copy.deepcopy(body)
    kept: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []  # system text waiting for the next user turn
    for message in out["messages"]:
        if not isinstance(message, dict):
            kept.append(message)
            continue
        if message.get("role") == "system":
            pending.extend(_text_blocks(message.get("content")))
            continue
        if message.get("role") == "user" and pending:
            content = _as_list(message.get("content"))
            # tool_result blocks lead a user turn; the folded text goes after them.
            lead = 0
            while lead < len(content) and isinstance(content[lead], dict) \
                    and content[lead].get("type") == "tool_result":
                lead += 1
            message["content"] = content[:lead] + pending + content[lead:]
            pending = []
        kept.append(message)
    if pending:
        last_user = next((m for m in reversed(kept)
                          if isinstance(m, dict) and m.get("role") == "user"), None)
        if last_user is not None:
            last_user["content"] = _as_list(last_user.get("content")) + pending
        else:
            system = out.get("system")
            if isinstance(system, str):
                system = [{"type": "text", "text": system}] if system else []
            out["system"] = (system if isinstance(system, list) else []) + pending
    out["messages"] = kept
    return out


def strip_sampling(body: dict[str, Any], limit: int | None = None) -> dict[str, Any]:
    """Strip generation settings and pin max_tokens, or keep its client value.

    Preserve identity on a no-op; discovery is the proxy handler's responsibility.
    """
    removed = _SAMPLING_KEYS - {"max_tokens"}
    if removed.isdisjoint(body) and (limit is None or body.get("max_tokens") == limit):
        return body
    out = {key: value for key, value in body.items() if key not in removed}
    if limit is not None:
        out["max_tokens"] = limit
    return out


ADAPTERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    FOLD_SYSTEM: fold_system,
    STRIP_SAMPLING: strip_sampling,
}


def adapt_body(name: str, path: str, raw: bytes, limit: int | None = None) -> bytes:
    """The body to forward for a request to `path`. Anything that is not a
    JSON object sent to /v1/messages passes through byte for byte. fold_system
    also applies to /v1/messages/count_tokens for compatibility."""
    names = [part.strip() for part in name.split(",")]
    path = path.split("?", 1)[0]
    if any(n not in ADAPTERS for n in names) or not raw or path not in (
        "/v1/messages", "/v1/messages/count_tokens",
    ):
        return raw
    try:
        body = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(body, dict):
        return raw
    adapted = body
    for adapter_name in names:
        if adapter_name == STRIP_SAMPLING:
            if path != "/v1/messages":
                continue
            adapted = strip_sampling(adapted, limit)
        else:
            adapted = ADAPTERS[adapter_name](adapted)
    if adapted is body:
        return raw
    # json.loads accepts escaped lone surrogates; keep them escaped on the wire.
    return json.dumps(adapted, ensure_ascii=False).encode("utf-8", errors="backslashreplace")


class AdapterProxy:
    """127.0.0.1:<free port> -> `upstream`, rewriting bodies with adapter `name`."""

    def __init__(self, name: str, upstream: str):
        names = [part.strip() for part in name.split(",")]
        for adapter_name in names:
            if adapter_name not in ADAPTERS:
                raise ValueError(
                    f"unknown adapter {adapter_name!r}; known: {', '.join(ADAPTERS)}")
        parts = urlsplit(upstream)
        self.name = name
        self.upstream = upstream
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        https = parts.scheme == "https"
        prefix = parts.path.rstrip("/")
        adapter = name
        strip_required = STRIP_SAMPLING in names
        cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
        model_limits: dict[str, int | None] = {}
        model_revision = 0
        model_lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.0: one request per connection, so a streamed answer ends
            # when the connection closes and needs no length up front.
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args: Any) -> None:
                return

            def _body(self) -> bytes:
                if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                    data = b""
                    while True:
                        size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                        if size == 0:
                            self.rfile.readline()
                            return data
                        data += self.rfile.read(size)
                        self.rfile.readline()
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _model_limit(self, model: Any) -> int | None:
                limit = None
                if isinstance(model, str):
                    revision = model_revision
                    # Cache hits never wait for discovery of a different model.
                    if model not in model_limits:
                        with model_lock:
                            # Share an overlapping attempt even when it failed.
                            if model not in model_limits and revision == model_revision:
                                self._refresh_models()
                    limit = model_limits.get(model)
                if limit is None:
                    log.warning("strip_sampling: model %r has no available max_model_len; "
                                "forwarding client max_tokens",
                                model if isinstance(model, str) else "<missing or invalid>")
                return limit

            def _refresh_models(self) -> None:
                nonlocal model_limits, model_revision
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() in ("authorization", "x-api-key")}
                conn = cls(host, port, timeout=10)
                try:
                    conn.request("GET", prefix + "/v1/models", headers=headers)
                    resp = conn.getresponse()
                    info = json.loads(resp.read()) if resp.status == 200 else None
                    data = info.get("data") if isinstance(info, dict) else None
                    if isinstance(data, list):
                        refreshed: dict[str, int | None] = {}
                        for entry in data:
                            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                                continue
                            value = entry.get("max_model_len")
                            refreshed[entry["id"]] = (
                                value if type(value) is int and value > 0 else None)
                        model_limits = refreshed
                except (OSError, http.client.HTTPException, ValueError):
                    pass  # discovery must not prevent forwarding the message
                finally:
                    conn.close()
                    model_revision += 1

            def _forward(self) -> None:
                raw = self._body()
                has_body = self.command in ("POST", "PUT", "PATCH")
                if has_body:
                    try:
                        limit = None
                        if strip_required and self.path.split("?", 1)[0] == "/v1/messages":
                            try:
                                body = json.loads(raw)
                            except ValueError:
                                body = None
                            if isinstance(body, dict):
                                limit = self._model_limit(body.get("model"))
                        raw = adapt_body(adapter, self.path, raw, limit)
                    except Exception:  # noqa: BLE001
                        if strip_required and self.path.split("?", 1)[0] == "/v1/messages":
                            log.warning("adapter %s failed; request blocked", adapter,
                                        exc_info=True)
                            self.send_error(502, "request adapter failed")
                            return
                        log.warning("adapter %s failed; forwarding the body as sent", adapter,
                                    exc_info=True)
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in _HOP_BY_HOP}
                conn = cls(host, port, timeout=3600)
                try:
                    conn.request(self.command, prefix + self.path,
                                 body=raw if raw or has_body else None, headers=headers)
                    resp = conn.getresponse()
                except OSError as exc:
                    conn.close()
                    self.send_error(502, f"upstream unreachable: {type(exc).__name__}")
                    return
                try:
                    self.send_response(resp.status, resp.reason)
                    for key, value in resp.getheaders():
                        if key.lower() not in _HOP_BY_HOP:
                            self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while True:
                        chunk = resp.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except OSError:
                    pass  # the child went away mid-stream
                finally:
                    conn.close()

            do_GET = do_POST = do_HEAD = do_PUT = do_PATCH = do_DELETE = _forward

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                        name=f"sam-adapter-{name}")
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


_lock = threading.Lock()
_proxies: dict[tuple[str, str], AdapterProxy] = {}


def proxy_url(name: str, upstream: str) -> str:
    """The local URL that forwards to `upstream` through adapter `name`."""
    key = (name, upstream.rstrip("/"))
    with _lock:
        proxy = _proxies.get(key)
        if proxy is None:
            proxy = AdapterProxy(name, key[1])
            _proxies[key] = proxy
            log.info("adapter %s on %s -> %s", name, proxy.url, key[1])
        return proxy.url


def child_base_url(cfg: Any) -> str | None:
    """The base URL a child on `cfg` is given: the provider's own, or the local
    adapter proxy in front of it when the provider sets one."""
    base = getattr(cfg, "base_url", None)
    name = getattr(cfg, "adapter", None)
    if not base or not name:
        return base
    return proxy_url(name, base)


def shutdown() -> None:
    """Stop every adapter proxy."""
    with _lock:
        proxies = list(_proxies.values())
        _proxies.clear()
    for proxy in proxies:
        try:
            proxy.stop()
        except Exception:  # noqa: BLE001
            log.warning("adapter proxy stop failed", exc_info=True)

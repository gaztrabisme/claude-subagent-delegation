"""Per-agent HTTP CONNECT proxy enforcing the provider's network allowlist."""

from __future__ import annotations

import select
import socket
import socketserver
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from urllib.parse import urlsplit


def _authority(value: str, default_port: int) -> tuple[str, int]:
    parsed = urlsplit("//" + value)
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError("missing destination host")
    try:
        port = parsed.port or default_port
    except ValueError as exc:
        raise ValueError("invalid destination port") from exc
    if not 1 <= port <= 65535:
        raise ValueError("invalid destination port")
    return host, port


def _rule(value: str) -> tuple[str, int]:
    return _authority(value, 443)


def _matches(host: str, port: int, allow: Sequence[tuple[str, int]]) -> bool:
    host = host.rstrip(".").lower()
    for candidate, candidate_port in allow:
        if port != candidate_port:
            continue
        if candidate.startswith("*."):
            suffix = candidate[1:]
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == candidate:
            return True
    return False


class EgressProxy:
    """A loopback proxy that forwards only destinations in ``allow``."""

    def __init__(
        self,
        allow: Sequence[str],
        on_refusal: Callable[[str, int], None] | None = None,
    ):
        self.allow = tuple(_rule(value.strip()) for value in allow)
        self.on_refusal = on_refusal
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        if self._server is not None:
            return int(self._server.server_address[1])
        owner = self

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

            def __init__(self):
                super().__init__(("127.0.0.1", 0), Handler)

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                owner._handle(self.request)

        server = Server()
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="subagent-egress",
            daemon=True,
        )
        self._thread.start()
        return int(self._server.server_address[1])

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=3)

    def _refuse(self, client: socket.socket, host: str, port: int) -> None:
        if self.on_refusal is not None:
            with suppress(Exception):
                self.on_refusal(host, port)
        with suppress(OSError):
            client.sendall(
                b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n"
                b"Content-Length: 0\r\n\r\n"
            )

    def _handle(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        client.settimeout(15)
        try:
            data = bytearray()
            while b"\r\n\r\n" not in data and len(data) <= 65536:
                chunk = client.recv(4096)
                if not chunk:
                    return
                data.extend(chunk)
            if len(data) > 65536:
                client.sendall(b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n")
                return
            boundary = data.find(b"\r\n\r\n") + 4
            header, remainder = bytes(data[:boundary]), bytes(data[boundary:])
            request_line = header.split(b"\r\n", 1)[0].decode("latin-1")
            fields = request_line.split()
            if len(fields) != 3:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                return
            method, target, _version = fields
            if method.upper() == "CONNECT":
                host, port = _authority(target, 443)
                if not _matches(host, port, self.allow):
                    self._refuse(client, host, port)
                    return
                upstream = socket.create_connection((host, port), timeout=15)
                upstream.settimeout(None)
                client.settimeout(None)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if remainder:
                    upstream.sendall(remainder)
                self._relay(client, upstream)
                return

            parsed = urlsplit(target)
            if parsed.scheme.lower() != "http" or not parsed.hostname:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                return
            host = parsed.hostname.rstrip(".").lower()
            port = parsed.port or 80
            if not _matches(host, port, self.allow):
                self._refuse(client, host, port)
                return
            upstream = socket.create_connection((host, port), timeout=15)
            upstream.settimeout(None)
            client.settimeout(None)
            upstream.sendall(header + remainder)
            self._relay(client, upstream)
        except (OSError, ValueError, UnicodeError):
            if upstream is not None:
                with suppress(OSError):
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
        finally:
            if upstream is not None:
                upstream.close()

    @staticmethod
    def _relay(client: socket.socket, upstream: socket.socket) -> None:
        peers = (client, upstream)
        ended: set[socket.socket] = set()
        while len(ended) < 2:
            readable, _, _ = select.select(
                [sock for sock in peers if sock not in ended], [], [], 30
            )
            if not readable:
                return
            for source in readable:
                destination = upstream if source is client else client
                try:
                    data = source.recv(65536)
                except OSError:
                    return
                if not data:
                    ended.add(source)
                    with suppress(OSError):
                        destination.shutdown(socket.SHUT_WR)
                    continue
                try:
                    destination.sendall(data)
                except OSError:
                    return

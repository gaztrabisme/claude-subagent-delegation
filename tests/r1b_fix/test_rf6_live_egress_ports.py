"""RF6: an agent's egress proxy serves only clients in that agent's process tree.

Verification (a sandboxed child of the test process) must get no route to the
network through another agent's egress proxy on loopback.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from subagent import egress, sandbox
from tests.r1b_fix._helpers import HEADER, run_script

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")

_PROBE = HEADER + """
import socket

def status_line(port, target):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as s:
            s.settimeout(5.0)
            s.sendall(f"CONNECT {target} HTTP/1.1\\r\\nHost: {target}\\r\\n\\r\\n".encode())
            data = b""
            while b"\\r\\n" not in data:
                chunk = s.recv(1024)
                if not chunk:
                    break
                data += chunk
            return data.split(b"\\r\\n", 1)[0].decode("latin-1") or "closed"
    except PermissionError as exc:
        return f"PermissionError:{exc.errno}"
    except OSError as exc:
        return f"OSError:{exc.errno}"

def try_connect(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3.0):
            return "connected"
    except OSError as exc:
        return f"{type(exc).__name__}:{exc.errno}"

R["proxy"] = status_line(T["proxy_port"], T["target"])
own = socket.socket()
own.bind(("127.0.0.1", 0))
own.listen(1)
R["own"] = try_connect(own.getsockname()[1])
done()
"""


class _Upstream:
    """Loopback TCP server that counts accepted connections."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._conns: list[socket.socket] = []
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            self._conns.append(conn)

    def close(self) -> None:
        self.sock.close()
        for conn in self._conns:
            conn.close()


@contextmanager
def _proxy_and_upstream(allowed_pids, monkeypatch, register: bool) -> Iterator[tuple[int, _Upstream]]:
    upstream = _Upstream()
    proxy = egress.EgressProxy([f"127.0.0.1:{upstream.port}"], allowed_pids=allowed_pids)
    port = proxy.start()
    if register:
        monkeypatch.setattr(sandbox, "live_egress_ports", lambda: {port}, raising=False)
    try:
        yield port, upstream
    finally:
        proxy.stop()
        upstream.close()


def _connect_status(proxy_port: int, target_port: int) -> str:
    with socket.create_connection(("127.0.0.1", proxy_port), timeout=5.0) as s:
        s.settimeout(5.0)
        s.sendall(
            f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\nHost: 127.0.0.1:{target_port}\r\n\r\n".encode()
        )
        data = b""
        while b"\r\n" not in data:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
        return data.split(b"\r\n", 1)[0].decode("latin-1")


def _settle() -> None:
    import time

    time.sleep(0.3)


def test_rf6a_verification_gets_no_route_through_foreign_egress_proxy(tmp_path, monkeypatch) -> None:
    """RF6a: a proxy whose allowed pids exclude verification gives the probe no 200 and no upstream connection."""
    with _proxy_and_upstream(lambda: {os.getpid()}, monkeypatch, register=True) as (port, upstream):
        _, result, data = run_script(
            tmp_path, _PROBE, targets={"proxy_port": port, "target": f"127.0.0.1:{upstream.port}"}
        )
        _settle()
        accepted = upstream.accepted
    assert result.sandboxed is True
    proxy = data.get("proxy", "")
    assert proxy, data
    assert " 200" not in proxy and not proxy.startswith("HTTP/1.1 2"), data
    assert proxy.startswith(("PermissionError", "OSError", "closed", "HTTP/1.1 403")), data
    assert accepted == 0, f"upstream saw {accepted} connection(s) via foreign proxy"
    assert data.get("own") == "connected", data


def test_rf6b_client_in_allowed_pids_gets_route(monkeypatch) -> None:
    """RF6b: positive control: the test process, listed in allowed_pids, gets 200 and reaches upstream."""
    with _proxy_and_upstream(lambda: {os.getpid()}, monkeypatch, register=False) as (port, upstream):
        status = _connect_status(port, upstream.port)
        _settle()
        accepted = upstream.accepted
    assert status.startswith("HTTP/1.1 200"), status
    assert accepted == 1


def test_rf6c_without_allowed_pids_behaviour_unchanged(monkeypatch) -> None:
    """RF6c: allowed_pids=None keeps today's behaviour: any client gets 200."""
    with _proxy_and_upstream(None, monkeypatch, register=False) as (port, upstream):
        status = _connect_status(port, upstream.port)
        _settle()
        accepted = upstream.accepted
    assert status.startswith("HTTP/1.1 200"), status
    assert accepted == 1

"""RF6: verification denies connects to other agents' live egress proxy ports."""

from __future__ import annotations

import socket
import sys

import pytest

from subagent import sandbox
from tests.r1b_fix._helpers import HEADER, run_script

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")

_SCRIPT = HEADER + """
import socket

def try_connect(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3.0):
            return "connected"
    except PermissionError as exc:
        return f"PermissionError:{exc.errno}"
    except OSError as exc:
        return f"OSError:{exc.errno}"

R["blocked"] = try_connect(T["blocked_port"])
own = socket.socket()
own.bind(("127.0.0.1", 0))
own.listen(1)
R["own"] = try_connect(own.getsockname()[1])
done()
"""


def test_rf6_live_egress_port_refused_own_loopback_allowed(tmp_path, monkeypatch) -> None:
    """RF6: connect to a port in sandbox.live_egress_ports() is refused; own loopback server still works."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    monkeypatch.setattr(sandbox, "live_egress_ports", lambda: {port}, raising=False)
    try:
        _, result, data = run_script(tmp_path, _SCRIPT, targets={"blocked_port": port})
    finally:
        srv.close()
    assert result.sandboxed is True
    assert data.get("blocked", "").startswith("PermissionError"), data
    assert data.get("own") == "connected", data

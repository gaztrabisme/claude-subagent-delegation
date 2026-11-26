"""RF6d / RF7: the pid check fails closed, and runs.py actually wires it.

Fixed interfaces: ``egress.client_pid(port) -> int | None`` (pid owning the
client end 127.0.0.1:<port>, None when unknown) called through the module
attribute; ``runs.Agent._start_egress_proxy`` builds ``EgressProxy`` (looked up
as ``subagent.runs.EgressProxy``) with ``allowed_pids=`` a zero-argument callable.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from subagent import egress
from subagent.runs import COMPLETED, Registry
from tests.conftest import make_settings

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS owner lookup")


class _Upstream:
    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._conns: list[socket.socket] = []
        threading.Thread(target=self._loop, daemon=True).start()

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
        return data.split(b"\r\n", 1)[0].decode("latin-1") or "closed"


def test_rf6d_owner_lookup_failure_refuses_with_403_and_no_upstream(monkeypatch) -> None:
    """RF6d: when egress.client_pid returns None the proxy answers 403 and opens no upstream connection."""
    upstream = _Upstream()
    monkeypatch.setattr(egress, "client_pid", lambda port: None, raising=False)
    proxy = egress.EgressProxy(
        [f"127.0.0.1:{upstream.port}"], allowed_pids=lambda: {os.getpid()}
    )
    port = proxy.start()
    try:
        status = _connect_status(port, upstream.port)
        time.sleep(0.3)
        accepted = upstream.accepted
    finally:
        proxy.stop()
        upstream.close()
    assert status.startswith("HTTP/1.1 403"), status
    assert accepted == 0


def test_rf7_runs_wires_allowed_pids_to_the_agent_process_tree(tmp_path: Path, monkeypatch) -> None:
    """RF7: the agent's egress proxy gets a non-None allowed_pids covering its child, excluding an unrelated process."""
    import subagent.runs as runs_mod

    upstream = _Upstream()
    created: list[dict] = []
    real_proxy = runs_mod.EgressProxy

    class Recording(real_proxy):
        def __init__(self, *args, **kwargs):
            created.append(dict(kwargs))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(runs_mod, "EgressProxy", Recording)

    ws = tmp_path / "ws"
    ws.mkdir()
    sig = tmp_path / "sig"
    sig.mkdir()
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, socket, sys, time\n"
        "from pathlib import Path\n"
        "from urllib.parse import urlsplit\n"
        "if '--version' in sys.argv[1:]:\n"
        "    print('1.0.0-fake'); sys.exit(0)\n"
        f"sig = Path({str(sig)!r})\n"
        "u = urlsplit(os.environ['HTTPS_PROXY'])\n"
        "(sig / 'port').write_text(str(u.port))\n"
        "(sig / 'pid').write_text(str(os.getpid()))\n"
        "with socket.create_connection((u.hostname, u.port), timeout=5) as s:\n"
        f"    s.sendall(b'CONNECT 127.0.0.1:{upstream.port} HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n')\n"
        "    (sig / 'child_status').write_text(s.recv(4096).split(b'\\r\\n')[0].decode())\n"
        "    for _ in range(150):\n"
        "        if (sig / 'done').exists(): break\n"
        "        time.sleep(0.1)\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's-w'}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,\n"
        "                  'result': 'done', 'session_id': 's-w', 'num_turns': 1,\n"
        "                  'usage': {'input_tokens': 1, 'output_tokens': 1}}), flush=True)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    providers = {
        "glm": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-5.3-flash[1m]",
            "api_key_env": "GLM_API_KEY",
            "binary": str(fake),
            "network_allow": [f"127.0.0.1:{upstream.port}"],
        }
    }
    settings = make_settings(tmp_path, workspace=str(ws), providers=providers, supervisor="off")
    reg = Registry(settings, start_reaper=False)
    outsider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        agent = reg.create_agent("rf7", ws, provider="glm")
        run = agent.submit("go", verification="true")
        deadline = time.time() + 10
        while time.time() < deadline and not (sig / "child_status").exists():
            time.sleep(0.05)
        assert (sig / "child_status").exists(), "fake child never reached the proxy"
        child_pid = int((sig / "pid").read_text())
        proxy_port = int((sig / "port").read_text())

        assert created and created[-1].get("allowed_pids") is not None, created
        pids = created[-1]["allowed_pids"]()
        assert child_pid in pids, (child_pid, pids)
        assert outsider.pid not in pids, (outsider.pid, pids)
        assert (sig / "child_status").read_text().startswith("HTTP/1.1 200")

        probe = (
            "import socket,sys\n"
            f"s=socket.create_connection(('127.0.0.1',{proxy_port}),timeout=5)\n"
            f"s.sendall(b'CONNECT 127.0.0.1:{upstream.port} HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n')\n"
            "print(s.recv(4096).split(b'\\r\\n')[0].decode() or 'closed')\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=15
        ).stdout.strip()
        assert out.startswith("HTTP/1.1 403"), out
        time.sleep(0.3)
        assert upstream.accepted == 1, upstream.accepted
        (sig / "done").write_text("x")
        assert run.done.wait(timeout=10)
        assert run.state == COMPLETED, run.error
    finally:
        (sig / "done").write_text("x")
        outsider.kill()
        outsider.wait()
        reg.shutdown()
        upstream.close()

"""A10 acceptance tests: network denied by default, EgressProxy, env, lifecycle, trace, doctor."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from subagent import cli, config, sandbox
from subagent.providers import base as providers_base
from subagent.runs import COMPLETED, Registry
from subagent.telemetry.trace import Trace
from tests.conftest import make_settings

from ._helpers import REPO_ROOT


def _free_tcp_server():
    """Start a local HTTP/TCP server on 127.0.0.1 and return (server, port)."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"ok-from-origin"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    return srv, port


def test_a10_config_network_allow_loads_and_defaults(tmp_path: Path) -> None:
    """A10: [providers.<name>] network_allow loads into ProviderConfig.network_allow (default ())."""
    cfg_path = tmp_path / "net.toml"
    cfg_path.write_text(
        '[providers.with_allow]\n'
        'driver = "claude"\n'
        'base_url = "https://api.z.ai/api/anthropic"\n'
        'network_allow = ["Example.COM", "api.foo.test:8443", "*.sub.test"]\n\n'
        '[providers.without_allow]\n'
        'driver = "claude"\n'
        'base_url = "https://api.z.ai/api/anthropic"\n',
        encoding="utf-8",
    )
    settings = config.load(extra=cfg_path)
    assert settings.provider("with_allow").network_allow == (
        "Example.COM",
        "api.foo.test:8443",
        "*.sub.test",
    )
    assert settings.provider("without_allow").network_allow == ()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt network check")
def test_a10_1_sandbox_network_loopback_ports_and_external_deny() -> None:
    """A10.1: sandbox.wrap_argv(..., network_loopback_ports=[p]) allows p and denies external/unlisted."""
    # Default None keeps today's profile with no network rules.
    plain_profile = sandbox.build_profile([], network_loopback_ports=None)
    assert "network-outbound" not in plain_profile

    srv_allowed, allowed_port = _free_tcp_server()
    srv_unlisted, unlisted_port = _free_tcp_server()
    try:
        probe = (
            "import errno, socket, sys\n"
            f"allowed_port = {allowed_port}\n"
            f"unlisted_port = {unlisted_port}\n"
            "s_ext = socket.socket()\n"
            "s_ext.settimeout(2)\n"
            "try:\n"
            "    s_ext.connect(('192.0.2.1', 80))\n"
            "    sys.exit(10)\n"
            "except OSError as exc:\n"
            "    if exc.errno not in (errno.EPERM, errno.EACCES):\n"
            "        sys.exit(11)\n"
            "finally:\n"
            "    s_ext.close()\n"
            "s_ok = socket.socket()\n"
            "s_ok.settimeout(2)\n"
            "s_ok.connect(('127.0.0.1', allowed_port))\n"
            "s_ok.close()\n"
            "s_bad = socket.socket()\n"
            "s_bad.settimeout(2)\n"
            "try:\n"
            "    s_bad.connect(('127.0.0.1', unlisted_port))\n"
            "    sys.exit(12)\n"
            "except OSError as exc:\n"
            "    if exc.errno not in (errno.EPERM, errno.EACCES):\n"
            "        sys.exit(13)\n"
            "finally:\n"
            "    s_bad.close()\n"
            "sys.exit(0)\n"
        )
        wrapped = sandbox.wrap_argv(
            [sys.executable, "-c", probe],
            [],
            network_loopback_ports=[allowed_port],
        )
        assert wrapped[:2] == ["/usr/bin/sandbox-exec", "-p"]
        assert "network-outbound" in wrapped[2]
        can_nest = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
            capture_output=True,
            timeout=5,
        ).returncode == 0
        if can_nest:
            proc = subprocess.run(wrapped, capture_output=True, text=True, timeout=10)
            assert proc.returncode == 0, f"rc={proc.returncode} stderr={proc.stderr!r}"
    finally:
        srv_allowed.shutdown()
        srv_allowed.server_close()
        srv_unlisted.shutdown()
        srv_unlisted.server_close()


def test_a10_2_child_env_sets_proxy_vars_and_no_proxy(tmp_path: Path, monkeypatch) -> None:
    """A10.2: spawned child env carries HTTPS_PROXY, HTTP_PROXY, ALL_PROXY and NO_PROXY."""
    ws = tmp_path / "ws"
    ws.mkdir()
    fake_bin = tmp_path / "bin" / "fake"
    fake_bin.parent.mkdir()
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    codex_home = tmp_path / "user-codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("GLM_API_KEY", "test-glm")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-dsh")

    providers = {
        "claude": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-5.3-flash[1m]",
            "api_key_env": "GLM_API_KEY",
            "binary": str(fake_bin),
        },
        "codex": {
            "driver": "codex",
            "binary": str(fake_bin),
            "network_allow": ["api.openai.com"],
        },
        "omp": {
            "driver": "omp",
            "base_url": "http://127.0.0.1:8000/v1",
            "model": "qwen-test",
            "binary": str(fake_bin),
            "experimental": True,
        },
        "dsh": {
            "driver": "dsh",
            "vendor": "deepseek",
            "api_key_env": "DEEPSEEK_API_KEY",
            "binary": str(fake_bin),
            "network_allow": ["api.deepseek.com"],
            "experimental": True,
        },
    }
    settings = make_settings(tmp_path, workspace=str(ws), providers=providers, supervisor="off")
    for pname in ("claude", "codex", "omp", "dsh"):
        captured_env: list[dict[str, str]] = []

        def fake_start(self, *, stdin=False, closed_stdin=False, _cap=captured_env):
            _cap.append(dict(self.env))
            raise RuntimeError("captured")

        monkeypatch.setattr("subagent.providers.base.Process._start", fake_start)
        reg = Registry(settings, start_reaper=False)
        try:
            agent = reg.create_agent(pname, ws, provider=pname)
            run = agent.submit("go", verification="true")
            run.done.wait(timeout=5)
        finally:
            reg.shutdown()
        assert captured_env, pname
        env = captured_env[0]
        proxy = env.get("HTTPS_PROXY", "")
        assert proxy.startswith("http://127.0.0.1:"), (pname, env)
        assert env.get("HTTP_PROXY") == proxy, pname
        assert env.get("ALL_PROXY") == proxy, pname
        no_proxy = {item.strip().lower() for item in env.get("NO_PROXY", "").split(",") if item.strip()}
        assert {"127.0.0.1", "localhost"} <= no_proxy, (pname, env.get("NO_PROXY"))


def test_a10_3_egress_proxy_connect_and_plain_http_allow_and_refusal() -> None:
    """A10.3: EgressProxy tunnels/forwards allowed targets and returns 403 + on_refusal otherwise."""
    from subagent.egress import EgressProxy

    srv, srv_port = _free_tcp_server()
    refusals: list[tuple[str, int]] = []
    proxy = EgressProxy(
        allow=[f"127.0.0.1:{srv_port}", "*.Example.COM"],
        on_refusal=lambda h, p: refusals.append((h, p)),
    )
    proxy_port = proxy.start()
    try:
        # 1. Allowed CONNECT to local test server succeeds (200) and tunnels HTTP.
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=5) as sock:
            sock.sendall(
                f"CONNECT 127.0.0.1:{srv_port} HTTP/1.1\r\nHost: 127.0.0.1:{srv_port}\r\n\r\n".encode()
            )
            head = sock.recv(4096)
            assert head.startswith(b"HTTP/1.1 200"), head
            sock.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            body = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                body += chunk
            assert b"ok-from-origin" in body

        # 2. Unlisted CONNECT gets 403 and calls on_refusal.
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=5) as sock:
            sock.sendall(b"CONNECT evil.test:443 HTTP/1.1\r\nHost: evil.test:443\r\n\r\n")
            resp = sock.recv(4096)
            assert b"403" in resp.splitlines()[0], resp

        # 3. Apex example.com does NOT match *.Example.COM -> 403 + on_refusal.
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=5) as sock:
            sock.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
            resp = sock.recv(4096)
            assert b"403" in resp.splitlines()[0], resp

        # 4. Plain HTTP absolute URI: allowed succeeds, unlisted gets 403.
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=5) as sock:
            sock.sendall(
                f"GET http://127.0.0.1:{srv_port}/ HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{srv_port}\r\nConnection: close\r\n\r\n".encode()
            )
            resp = sock.recv(4096)
            assert b"200" in resp.splitlines()[0] and b"ok-from-origin" in resp

        with socket.create_connection(("127.0.0.1", proxy_port), timeout=5) as sock:
            sock.sendall(
                b"GET http://blocked.test/secret HTTP/1.1\r\n"
                b"Host: blocked.test\r\nConnection: close\r\n\r\n"
            )
            resp = sock.recv(4096)
            assert b"403" in resp.splitlines()[0], resp

        assert refusals == [
            ("evil.test", 443),
            ("example.com", 443),
            ("blocked.test", 80),
        ]
    finally:
        proxy.stop()
        srv.shutdown()
        srv.server_close()


def test_a10_3_e2e_fake_claude_refused_connect_writes_egress_refused_trace(
    tmp_path: Path, monkeypatch
) -> None:
    """A10.3: fake CLI child seeing HTTPS_PROXY gets 403 and writes one egress_refused trace record."""
    from subagent.runs import Agent

    can_nest = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
        capture_output=True,
        timeout=5,
    ).returncode == 0
    if not can_nest:
        real_agent_spawn = Agent._spawn

        def strip_nested_sandbox(self, run, prompt, resume):
            proc = real_agent_spawn(self, run, prompt, resume)
            if proc.argv[:2] == ["/usr/bin/sandbox-exec", "-p"]:
                proc.argv = proc.argv[3:]
            return proc

        monkeypatch.setattr(Agent, "_spawn", strip_nested_sandbox)

    ws = tmp_path / "ws"
    ws.mkdir()
    fake_claude = tmp_path / "bin" / "claude"
    fake_claude.parent.mkdir()
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, socket, sys\n"
        "from urllib.parse import urlsplit\n"
        "if '--version' in sys.argv[1:]:\n"
        "    print('1.0.0-fake')\n"
        "    sys.exit(0)\n"
        "proxy = os.environ['HTTPS_PROXY']\n"
        "u = urlsplit(proxy)\n"
        "with socket.create_connection((u.hostname, u.port), timeout=5) as s:\n"
        "    s.sendall(b'CONNECT unlisted.example:8443 HTTP/1.1\\r\\nHost: unlisted.example:8443\\r\\n\\r\\n')\n"
        "    status = s.recv(4096)\n"
        "    assert b'403' in status, status\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's-e2e'}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,\n"
        "                  'result': 'done', 'session_id': 's-e2e', 'num_turns': 1,\n"
        "                  'usage': {'input_tokens': 1, 'output_tokens': 1}}), flush=True)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    trace_path = tmp_path / "trace.jsonl"
    providers = {
        "glm": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-5.3-flash[1m]",
            "api_key_env": "GLM_API_KEY",
            "binary": str(fake_claude),
        }
    }
    settings = make_settings(
        tmp_path,
        workspace=str(ws),
        providers=providers,
        supervisor="off",
        trace=str(trace_path),
    )
    reg = Registry(settings, start_reaper=False, trace=Trace(trace_path))
    try:
        agent = reg.create_agent("e2e", ws, provider="glm")
        run = agent.submit("check egress", verification="true")
        assert run.done.wait(timeout=10)
        assert run.state == COMPLETED, run.error
    finally:
        reg.shutdown()

    records = [json.loads(ln) for ln in trace_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    refused = [r for r in records if r.get("type") == "egress_refused"]
    assert len(refused) == 1, f"expected 1 egress_refused record, got {records}"
    rec = refused[0]
    assert rec["agent_id"] == agent.agent_id
    assert rec["run_id"] == run.run_id
    assert rec["provider"] == "glm"
    assert rec["host"] == "unlisted.example"
    assert rec["port"] == 8443
    assert isinstance(rec["ts"], (int, float))


def test_a10_4_proxy_starts_on_first_spawn_and_stops_on_close(tmp_path: Path, monkeypatch) -> None:
    """A10.4: egress proxy starts with the agent's first spawn and stops when the agent closes."""
    from urllib.parse import urlsplit

    ws = tmp_path / "ws"
    ws.mkdir()
    fake_bin = tmp_path / "bin" / "claude"
    fake_bin.parent.mkdir()
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bin.chmod(0o755)

    seen_proxy: list[str] = []
    alive_during_spawn: list[bool] = []

    def fake_spawn(argv, env, cwd):
        proxy_url = env.get("HTTPS_PROXY", "")
        seen_proxy.append(proxy_url)
        u = urlsplit(proxy_url)
        try:
            with socket.create_connection((u.hostname, u.port), timeout=2):
                alive_during_spawn.append(True)
        except OSError:
            alive_during_spawn.append(False)
        from tests.test_runs import FakeProcess, _result

        return FakeProcess(_result("ok"), argv, env)

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", fake_spawn)
    providers = {
        "glm": {
            "driver": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "model": "glm-5.3-flash[1m]",
            "api_key_env": "GLM_API_KEY",
            "binary": str(fake_bin),
        }
    }
    settings = make_settings(tmp_path, workspace=str(ws), providers=providers, supervisor="off")
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("a10-life", ws, provider="glm")
        assert getattr(agent, "_egress_proxy", None) is None
        run = agent.submit("go", verification="true")
        assert run.done.wait(timeout=5)
        assert run.state == COMPLETED
        assert alive_during_spawn == [True]
        u = urlsplit(seen_proxy[0])
        agent.close("done")
        with pytest.raises(OSError):
            socket.create_connection((u.hostname, u.port), timeout=1)
    finally:
        reg.shutdown()


def test_a10_5_doctor_allowlist_cloud_warning_examples_and_vendor_map(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A10.5: doctor reports allowlist, warns on cloud provider with no route, examples list hosts, VENDOR_BY_HOST untouched."""
    # 1. VENDOR_BY_HOST stays unchanged and is not used as an allowlist.
    assert providers_base.VENDOR_BY_HOST == {
        "api.z.ai": "zai",
        "api.deepseek.com": "deepseek",
    }

    cfg_path = tmp_path / "doctor.toml"
    cfg_path.write_text(
        '[providers.cloud_noroute]\n'
        'driver = "codex"\n'
        'vendor = "zai"\n'
        'local = false\n\n'
        '[providers.cloud_with_allow]\n'
        'driver = "claude"\n'
        'base_url = "https://api.z.ai/api/anthropic"\n'
        'network_allow = ["extra.zai.example:8443"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(config.CONFIG_ENV, str(cfg_path))
    assert cli.main(["doctor", "--no-probe", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = {r["name"]: r for r in payload["providers"]}

    noroute = rows["cloud_noroute"]
    allow_key = "network_allow" if "network_allow" in noroute else "allowlist"
    assert allow_key in noroute, noroute
    assert noroute[allow_key] == []
    assert "warning" in noroute and noroute["warning"]

    with_allow = rows["cloud_with_allow"]
    assert "api.z.ai:443" in with_allow[allow_key] or "api.z.ai" in with_allow[allow_key]
    assert "extra.zai.example:8443" in with_allow[allow_key]

    # 2. Cloud examples in examples/config.*.toml list non-empty allowlist / base_url.
    examples_dir = REPO_ROOT / "examples"
    for name in (
        "config.claude.toml",
        "config.codex.toml",
        "config.copilot.toml",
        "config.deepseek.toml",
        "config.dsh.toml",
        "config.glm.toml",
        "config.antigravity.toml",
        "config.full.toml",
    ):
        monkeypatch.setenv(config.CONFIG_ENV, str(examples_dir / name))
        assert cli.main(["doctor", "--no-probe", "--json"]) == 0
        ex_data = json.loads(capsys.readouterr().out)
        for prow in ex_data["providers"]:
            cfg = config.load(extra=examples_dir / name).provider(prow["name"])
            if not cfg.local:
                assert prow.get(allow_key), f"{name}:{prow['name']} has empty allowlist"

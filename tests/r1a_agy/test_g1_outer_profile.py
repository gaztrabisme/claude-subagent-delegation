"""G1 acceptance tests: antigravity always runs under the server's outer sandbox profile."""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from subagent import sandbox
from subagent.providers import for_driver
from subagent.runs import COMPLETED, FAILED, Registry
from tests.r1a._helpers import sbpl_allows

from ._common import (
    PROVIDER,
    capture_spawn,
    fake_binary,
    init_event,
    make_agy_settings,
    make_home,
    result_event,
    run_fake,
    trace_of,
)


def _darwin_argv(tmp_path: Path, monkeypatch, agent_id: str = "agy-g1", **extra):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    home = make_home(tmp_path, monkeypatch)
    ws, sock_dir, settings = make_agy_settings(tmp_path, fake_binary(tmp_path), **extra)
    argv, env = capture_spawn(monkeypatch, settings, ws, agent_id)
    return argv, env, home, ws, sock_dir, settings


def test_g1_darwin_argv_starts_with_sandbox_exec_and_keeps_agy_flags(tmp_path, monkeypatch) -> None:
    """G1: on macOS the antigravity argv is `/usr/bin/sandbox-exec -p <profile>` then the agy argv."""
    argv, _, _, _, _, _ = _darwin_argv(tmp_path, monkeypatch)
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"], argv[:3]
    assert "(version 1)" in argv[2]
    rest = argv[3:]
    assert rest[0].endswith("agy"), rest[:2]
    assert "--sandbox" in rest and "--dangerously-skip-permissions" in rest
    assert "-p" in rest and "stream-json" in rest


def test_g1_1_write_roots_and_profile_write_policy(tmp_path, monkeypatch) -> None:
    """G1.1: file-write* only in workspace, agent state and agy's runtime dir; roots exposed by the driver."""
    argv, _, home, ws, _, settings = _darwin_argv(tmp_path, monkeypatch, "agy-w")
    profile = argv[2]
    state_dir = home / ".gemini" / "antigravity-cli"
    assert sbpl_allows(profile, "file-write*", ws / "new.txt")
    assert sbpl_allows(profile, "file-write*", state_dir / "conversations" / "c.db")
    assert sbpl_allows(profile, "file-write*", settings.session_root / "agents" / "agy-w" / "scratch.txt")
    assert not sbpl_allows(profile, "file-write*", tmp_path / "outside.txt")
    assert not sbpl_allows(profile, "file-write*", home / ".ssh" / "authorized_keys")
    assert not sbpl_allows(profile, "file-write*", home / "Documents" / "x.txt")
    assert not sbpl_allows(profile, "file-write*", ws.parent / "ws-sibling" / "x.txt")

    driver = for_driver("antigravity")
    roots = driver.sandbox_write_roots(settings, "agy-w", settings.provider(PROVIDER), ws)
    assert isinstance(roots, list) and roots and all(isinstance(r, str) and r for r in roots)
    resolved = {str(Path(r).resolve()) for r in roots}
    assert str(ws.resolve()) in resolved
    assert any(Path(r).resolve() == state_dir.resolve() or
               state_dir.resolve().is_relative_to(Path(r).resolve()) and
               Path(r).resolve() != home.resolve() for r in roots), roots
    assert str(home.resolve()) not in resolved and "/" not in roots


def test_g1_2_socket_dir_write_denied_and_other_agents_unreadable(tmp_path, monkeypatch) -> None:
    """G1.2: approval socket directory write-denied; other agents' directories read-denied (A9.2, A9.3)."""
    argv, _, _, _, sock_dir, settings = _darwin_argv(tmp_path, monkeypatch, "agy-s")
    profile = argv[2]
    assert not sbpl_allows(profile, "file-write*", sock_dir)
    assert not sbpl_allows(profile, "file-write*", sock_dir / "approval.sock")
    agents = settings.session_root / "agents"
    assert not sbpl_allows(profile, "file-read*", agents / "other-agent" / "approval.key")
    assert not sbpl_allows(profile, "file-read*", agents / "other-agent")
    assert sbpl_allows(profile, "file-read*", agents / "agy-s" / "approval.key")


def test_g1_3_owner_credential_homes_unreadable_but_gemini_home_readable(tmp_path, monkeypatch) -> None:
    """G1.3: file-read* of ~/.ssh, ~/.dsh, ~/.codex, ~/.claude, ~/.aws, ~/.config/gh denied; ~/.gemini not."""
    argv, _, home, _, _, _ = _darwin_argv(tmp_path, monkeypatch, "agy-r")
    profile = argv[2]
    for rel in (".ssh", ".dsh", ".codex", ".claude", ".aws", ".config/gh"):
        assert not sbpl_allows(profile, "file-read*", home / rel / "token"), rel
        assert not sbpl_allows(profile, "file-read*", home / rel), rel
    assert sbpl_allows(profile, "file-read*", home / ".gemini" / "antigravity-cli" / "token")
    assert sbpl_allows(profile, "file-read*", home / ".gemini" / "token")


def test_g1_4_network_policy_and_proxy_env(tmp_path, monkeypatch) -> None:
    """G1.4: profile denies outbound except proxy and loopback base_url ports; child env carries proxy vars."""
    argv, env, _, _, _, _ = _darwin_argv(
        tmp_path, monkeypatch, "agy-n", base_url="http://127.0.0.1:8123/v1",
        network_allow=["generativelanguage.googleapis.com"],
    )
    profile = argv[2]
    proxy = env.get("HTTPS_PROXY", "")
    assert proxy.startswith("http://127.0.0.1:"), env.keys()
    port = proxy.rsplit(":", 1)[1].rstrip("/")
    assert env.get("HTTP_PROXY") == proxy and env.get("ALL_PROXY") == proxy
    no_proxy = {v.strip().lower() for v in env.get("NO_PROXY", "").split(",")}
    assert {"127.0.0.1", "localhost"} <= no_proxy
    assert "network-outbound" in profile
    assert port in profile and "8123" in profile


def _local_server():
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
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv, srv.server_address[1]


@pytest.mark.skipif(sys.platform != "darwin", reason="antigravity runs only under macOS Seatbelt")
def test_g1_4_e2e_fake_agy_uses_proxy_allowlist_and_refusal_is_traced(tmp_path, monkeypatch) -> None:
    """G1.4: a fake agy reaches an allowed host through the proxy; an unlisted host gets 403 and a trace record."""
    srv, port = _local_server()
    prelude = (
        "from urllib.parse import urlsplit\n"
        "import socket\n"
        "u = urlsplit(os.environ['HTTPS_PROXY'])\n"
        "def connect(target):\n"
        "    with socket.create_connection((u.hostname, u.port), timeout=5) as s:\n"
        "        s.sendall(('CONNECT %s HTTP/1.1\\r\\nHost: %s\\r\\n\\r\\n' % (target, target)).encode())\n"
        "        return s.recv(4096)\n"
        f"assert connect('127.0.0.1:{port}').startswith(b'HTTP/1.1 200')\n"
        "assert b'403' in connect('unlisted.example:8443').splitlines()[0]\n"
    )
    try:
        agent, run, trace_path, _ = run_fake(
            tmp_path, monkeypatch, [init_event(), result_event("ok")], prelude=prelude,
            network_allow=[f"127.0.0.1:{port}"],
        )
    finally:
        srv.shutdown()
        srv.server_close()
    assert run.state == COMPLETED, (run.error, run.result_text)
    refused = trace_of(trace_path, "egress_refused")
    assert len(refused) == 1, refused
    rec = refused[0]
    assert (rec["host"], rec["port"], rec["provider"]) == ("unlisted.example", 8443, PROVIDER)
    assert rec["agent_id"] == agent.agent_id and rec["run_id"] == run.run_id


def test_g1_off_macos_antigravity_refuses_to_start(tmp_path, monkeypatch) -> None:
    """G1: off macOS the antigravity driver refuses to start; no child process is spawned."""
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    make_home(tmp_path, monkeypatch)
    ws, _, settings = make_agy_settings(tmp_path, fake_binary(tmp_path))
    started: list[list[str]] = []

    def fake_start(self, *, stdin=False, closed_stdin=False):
        started.append(list(self.argv))
        raise RuntimeError("must not be reached")

    monkeypatch.setattr("subagent.providers.base.Process._start", fake_start)
    reg = Registry(settings, start_reaper=False)
    refused = False
    try:
        try:
            agent = reg.create_agent("agy-l", ws, provider=PROVIDER)
            run = agent.submit("do work", verification="true")
            assert run.done.wait(5)
            refused = run.state == FAILED
        except Exception:  # noqa: BLE001 - refusing at create/submit is also a refusal
            refused = True
    finally:
        reg.shutdown()
    assert started == [], f"agy was spawned off macOS: {started}"
    assert refused, "antigravity must refuse (failed run or raised error) off macOS"

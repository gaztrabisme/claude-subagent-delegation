"""Spec 8.2: DevServer: loopback check, reuse, stop, missing binary, startup timeout."""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from subagent.graph import GraphUnavailable
from subagent.graph.server import DevServer, find_temporal_cli

from ..conftest import require_temporal
from ._cluster import pid_alive, wait_for


@pytest.mark.parametrize("address", ["0.0.0.0:7233", "10.1.2.3:7233", "example.com:7233",
                                     "192.0.2.7:0"])
def test_non_loopback_address_is_refused(tmp_path, address):
    with pytest.raises(ValueError, match="graph server address must be loopback"):
        DevServer(tmp_path, binary=Path(sys.executable),
                  address=address).start()


@pytest.mark.parametrize("address", ["127.0.0.1:0", "localhost:0", "127.0.0.1:7233"])
def test_loopback_addresses_construct(tmp_path, address):
    DevServer(tmp_path, binary=Path(sys.executable), address=address)


def test_missing_binary_message(tmp_path, monkeypatch):
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("TEMPORAL_BIN", str(tmp_path / "not-a-file"))
    with pytest.raises(GraphUnavailable) as info:
        find_temporal_cli()
    assert "Temporal CLI not found: put temporal on PATH or set TEMPORAL_BIN" in str(info.value)
    monkeypatch.delenv("TEMPORAL_BIN")
    with pytest.raises(GraphUnavailable) as info2:
        find_temporal_cli()
    assert "Temporal CLI not found: put temporal on PATH or set TEMPORAL_BIN" in str(info2.value)


def test_missing_binary_surfaces_on_start(tmp_path, monkeypatch):
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("TEMPORAL_BIN", str(tmp_path / "not-a-file"))
    with pytest.raises(GraphUnavailable, match="Temporal CLI not found"):
        DevServer(tmp_path / "state", address="127.0.0.1:0").start()


def test_explicit_binary_wins_over_environment(tmp_path, monkeypatch):
    explicit = tmp_path / "temporal-explicit"
    explicit.write_text("#!/bin/sh\n")
    explicit.chmod(0o755)
    monkeypatch.setenv("TEMPORAL_BIN", str(tmp_path / "other"))
    assert find_temporal_cli(explicit) == explicit
    env_bin = tmp_path / "temporal-env"
    env_bin.write_text("#!/bin/sh\n")
    env_bin.chmod(0o755)
    monkeypatch.setenv("TEMPORAL_BIN", str(env_bin))
    assert find_temporal_cli() == env_bin


def test_startup_timeout_kills_a_binary_that_never_listens(tmp_path):
    pidfile = tmp_path / "fake.pid"
    fake = tmp_path / "temporal-fake"
    fake.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nexec sleep 300\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    started = time.monotonic()
    with pytest.raises(GraphUnavailable) as info:
        DevServer(tmp_path / "state", binary=fake, address="127.0.0.1:0",
                  startup_timeout_s=1.5).start()
    assert time.monotonic() - started < 20
    message = str(info.value)
    assert "Temporal dev server did not become healthy in" in message
    assert "server.log" in message
    pid = int(pidfile.read_text().strip())
    wait_for(lambda: not pid_alive(pid), 10, "the fake server to be killed")
    assert not (tmp_path / "state" / "server.json").exists()


def test_health_of_nothing_listening(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    health = DevServer(tmp_path, binary=Path(sys.executable),
                       address=f"127.0.0.1:{port}").health(timeout_s=0.5)
    assert health["ok"] is False
    for key in ("ok", "address", "pid", "db_path", "error"):
        assert key in health
    assert health["error"]


def test_start_reuse_stop_and_no_process_left(tmp_path):
    binary = require_temporal()
    state = tmp_path / "state"
    first = DevServer(state, binary=binary, address="127.0.0.1:0", startup_timeout_s=30)
    try:
        info = first.start()
        assert info.started_by_us is True
        assert info.address.startswith("127.0.0.1:") and not info.address.endswith(":0")
        assert info.pid and pid_alive(info.pid)
        assert info.db_path == str(state / "temporal.db")
        assert info.binary == str(binary)
        record = json.loads((state / "server.json").read_text())
        assert {"pid", "address", "db_path", "binary", "started_at"} <= set(record)
        assert record["pid"] == info.pid
        assert oct(os.stat(state).st_mode & 0o777) == "0o700"
        health = first.health()
        assert health["ok"] is True and health["pid"] == info.pid
        assert health["address"] == info.address

        second = DevServer(state, binary=binary, address=info.address, startup_timeout_s=30)
        again = second.start()
        assert again.started_by_us is False
        assert again.pid == info.pid
        assert second.info().pid == info.pid
    finally:
        stopped = first.stop(timeout_s=10)
    assert stopped is True
    assert not pid_alive(info.pid)
    assert not (state / "server.json").exists()
    assert first.stop() is False  # nothing left to stop
    assert (state / "server.log").exists()


def test_stop_never_signals_a_pid_that_is_not_our_temporal(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    bystander = subprocess.Popen(["sleep", "60"])
    try:
        (state / "server.json").write_text(json.dumps({
            "pid": bystander.pid, "address": "127.0.0.1:1", "db_path": str(state / "temporal.db"),
            "binary": "temporal", "started_at": time.time()}))
        server = DevServer(state, binary=Path(sys.executable), address="127.0.0.1:0")
        assert server.stop(timeout_s=1) is False
        assert bystander.poll() is None, "stop() killed a process that is not the dev server"
    finally:
        bystander.kill()
        bystander.wait(timeout=10)

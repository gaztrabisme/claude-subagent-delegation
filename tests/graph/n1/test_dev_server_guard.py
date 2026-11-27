"""N1-1: a failing server.tmp guard must not leave the Temporal dev server running."""

from __future__ import annotations

import os

import pytest


class _FakeProc:
    pid = 987654
    returncode: int | None = None

    def __init__(self) -> None:
        self.events: list[str] = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.events.append("wait")
        self.returncode = 0
        return 0

    def terminate(self):
        self.events.append("terminate")

    def kill(self):
        self.events.append("kill")


def test_tmp_guard_failure_does_not_leave_the_dev_server_running(tmp_path, monkeypatch):
    from subagent.graph import server as srv

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    target = tmp_path / "elsewhere"
    target.write_text("x")
    os.symlink(target, state / "server.tmp")  # the guard refuses a non-regular server.tmp
    binary = tmp_path / "temporal"
    binary.write_text("#!/bin/sh\n")

    spawned: list[_FakeProc] = []
    killpg_calls: list[tuple[int, int]] = []

    def fake_popen(*args, **kwargs):
        proc = _FakeProc()
        spawned.append(proc)
        return proc

    monkeypatch.setattr(srv.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(srv.os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))
    monkeypatch.setattr(srv.DevServer, "_probe", lambda self, host, port: True)

    server = srv.DevServer(state, binary=binary, address="127.0.0.1:0", startup_timeout_s=5.0)
    with pytest.raises(OSError):
        server.start()

    for proc in spawned:  # spawning first is allowed, but then the process must be stopped
        stopped = bool(proc.events) or any(pgid == proc.pid for pgid, _ in killpg_calls)
        assert stopped, (
            "DevServer.start() raised on the server.tmp guard after spawning the dev server "
            "and left it running (no kill, no wait)")
    assert not (state / "server.json").exists()

"""Fix4 review (fix4-ds): temp-file symlink, leaked read connection, silent sidecar chmod failure."""

from __future__ import annotations

import errno
import logging
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from subagent.graph import server as srv
from subagent.graph import store as store_mod
from subagent.graph.store import Store


class _FakeProc:
    pid = 424242

    def poll(self):
        return None


def test_server_json_temp_symlink_not_followed(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    outside = tmp_path / "outside.txt"
    outside.write_text("precious")
    outside.chmod(0o644)
    # plant a symlink at every plausible temp name
    for name in ("server.tmp", "server.json.tmp"):
        (state / name).symlink_to(outside)

    monkeypatch.setattr(srv, "find_temporal_cli", lambda explicit=None: Path("/bin/true"))
    monkeypatch.setattr(srv.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(srv.DevServer, "_probe", lambda self, h, p: True)

    d = srv.DevServer(state, address="127.0.0.1:17233")
    try:
        d.start()
    except OSError:
        pass  # failing closed is acceptable
    else:
        j = state / "server.json"
        assert not j.is_symlink()
        st = j.lstat()
        assert stat.S_ISREG(st.st_mode)
        assert stat.S_IMODE(st.st_mode) == 0o600
    assert outside.read_text() == "precious"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


def test_read_connection_closed_when_first_query_raises(tmp_path, monkeypatch):
    s = Store(tmp_path / "g" / "graph.db")
    opened: list[object] = []

    class Wrapper:
        def __init__(self, conn):
            self._c = conn
            self.closed = False
            opened.append(self)

        def execute(self, *a, **k):
            raise sqlite3.OperationalError("boom")

        def close(self):
            self.closed = True
            self._c.close()

        def __getattr__(self, n):
            return getattr(self._c, n)

    monkeypatch.setattr(s, "_connect", lambda: Wrapper(sqlite3.connect(str(s.path))))
    with pytest.raises(sqlite3.OperationalError):
        s.read()
    assert opened and all(w.closed for w in opened)


def test_sidecar_chmod_failure_not_silent(tmp_path, monkeypatch, caplog):
    real = os.chmod

    def fake(path, mode, *a, **k):
        if str(path).endswith("-wal"):
            raise PermissionError(errno.EPERM, "nope")
        return real(path, mode, *a, **k)

    s = Store(tmp_path / "g" / "graph.db")
    wal = Path(f"{s.path}-wal")
    if not wal.exists():
        wal.touch()
    monkeypatch.setattr(store_mod.os, "chmod", fake)
    caplog.set_level(logging.WARNING)
    try:
        s._secure_sidecars()
    except OSError:
        return
    assert any(r.name.startswith("subagent.graph") and r.levelno >= logging.WARNING
               for r in caplog.records)

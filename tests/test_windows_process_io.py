"""UTF-8 provider pipes and Windows process probes."""

import importlib
import os
import subprocess
import sys

import pytest

from subagent.providers.base import Process

loop = importlib.import_module("subagent.loop.loop")


def test_provider_event_reader_utf8_and_replacement(tmp_path):
    source = (
        "import sys; "
        "sys.stdout.buffer.write(b'{\"text\":\"caf\\xc3\\xa9 \\xe2\\x80\\x9d\"}\\n'); "
        "sys.stdout.buffer.write(b'{\"text\":\"bad \\xff\"}\\n'); "
        "sys.stderr.buffer.write(b'caf\\xc3\\xa9 \\xff')"
    )
    process = Process([sys.executable, "-c", source], dict(os.environ), str(tmp_path))
    child = process._start()
    errors = []
    thread = process._drain_thread(child, errors)
    try:
        events = list(process._json_lines(child.stdout))
        assert child.wait(timeout=10) == 0
        thread.join(timeout=10)
        assert events == [{"text": "caf\u00e9 \u201d"}, {"text": "bad \ufffd"}]
        assert errors == ["caf\u00e9 \ufffd"]
    finally:
        process.kill()
        child.stdout.close()
        child.stderr.close()


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows process handles")
def test_windows_pid_probe_never_signals(monkeypatch):
    def forbidden(*args):
        pytest.fail("Windows liveness probe must not call os.kill")

    monkeypatch.setattr(os, "kill", forbidden)
    assert loop._pid_alive(os.getpid())
    assert not loop._pid_alive(0)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    assert not loop._pid_alive(child.pid)

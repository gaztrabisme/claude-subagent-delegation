"""N6 fix: SIGTERM to run_demo.py must stop its dev server and worker."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from ._util import DEMO, REPO, ps


def _holders(needle: str, exclude: set[int]) -> set[int]:
    return {pid for pid, _, cmd in ps() if needle in cmd and pid not in exclude}


def test_sigterm_leaves_no_server_or_worker(tmp_path):
    binary = os.environ.get("TEMPORAL_BIN") or shutil.which("temporal")
    if not binary:
        pytest.skip("Temporal CLI not found")
    pytest.importorskip("temporalio")
    out = tmp_path / "out.log"
    env = {**os.environ, "TEMPORAL_BIN": binary, "PYTHONUNBUFFERED": "1"}
    with open(out, "wb") as fh:
        proc = subprocess.Popen(
            [sys.executable, str(DEMO), "--dispatcher", "fake", "--max-minutes", "3"],
            cwd=REPO, env=env, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    run_dir = None
    me = {os.getpid()}
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            text = out.read_text(errors="replace")
            for line in text.splitlines():
                if line.startswith("run dir: "):
                    run_dir = line[len("run dir: "):].strip()
            if run_dir and "\nstarted " in "\n" + text:  # worker registered, graph started
                break
            assert proc.poll() is None, f"run_demo.py exited early: {text[-600:]}"
            time.sleep(0.3)
        assert run_dir, "run_demo.py never printed its run dir"
        assert "started " in out.read_text(errors="replace"), "the graph never started"
        assert _holders(run_dir, me | {proc.pid}), (
            "no server or worker names the run dir; nothing to test against")
        proc.send_signal(signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=60)
        end = time.monotonic() + 20
        left: set[int] = set()
        while time.monotonic() < end:
            left = _holders(run_dir, me)
            if not left:
                break
            time.sleep(0.5)
        assert not left, (
            "processes naming the run dir survive SIGTERM: "
            f"{sorted((p, c[:110]) for p, _, c in ps() if p in left)}")
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        if run_dir:
            for pid in _holders(run_dir, me):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
        proc.wait()

"""G-H1: SIGTERM to orchestrate.py while its children run must leave no process behind."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "graph_scale_gate" / "orchestrate.py"


def _ps() -> list[tuple[int, int, str]]:
    """(pid, ppid, command + environment) of every process; `ps e` appends the environment."""
    out = subprocess.run(["ps", "-axeww", "-o", "pid=,ppid=,command="], capture_output=True,
                         text=True, check=False).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def _descendants(root: int) -> set[int]:
    rows = _ps()
    found: set[int] = set()
    todo = [root]
    while todo:
        cur = todo.pop()
        for pid, ppid, _ in rows:
            if ppid == cur and pid not in found:
                found.add(pid)
                todo.append(pid)
    return found


def _with_run_dir(run_dir: Path, exclude: set[int]) -> set[int]:
    needle = str(run_dir)
    return {pid for pid, _, cmd in _ps() if needle in cmd and pid not in exclude}


def test_sigterm_to_the_orchestrator_leaves_no_child_process(tmp_path):
    binary = os.environ.get("TEMPORAL_BIN") or shutil.which("temporal")
    if not binary:
        pytest.skip("Temporal CLI not found")
    pytest.importorskip("temporalio")
    run_root = tmp_path / "runs"
    env = {**os.environ, "TEMPORAL_BIN": binary}
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--dispatch", "fake", "--run-root", str(run_root),
         "--timeout", "120"],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True)
    seen: set[int] = set()
    me = {os.getpid()}
    try:
        deadline = time.monotonic() + 90
        run_dir = None
        while time.monotonic() < deadline:
            dirs = sorted(run_root.glob("gate-fake-*")) if run_root.exists() else []
            if dirs and (dirs[0] / "audit.jsonl").exists() and (
                    "worker_started" in (dirs[0] / "audit.jsonl").read_text(errors="replace")):
                run_dir = dirs[0]
                break
            assert proc.poll() is None, "orchestrate.py exited before its worker started"
            time.sleep(0.3)
        assert run_dir is not None, "the worker never started"
        time.sleep(2.0)  # clients are up
        kids = _descendants(proc.pid)
        assert kids, "orchestrate.py has no child processes to test against"
        seen |= kids | _with_run_dir(run_dir, me | {proc.pid})
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pytest.fail("orchestrate.py did not exit within 60 s of SIGTERM")
        end = time.monotonic() + 20
        left: set[int] = set()
        while time.monotonic() < end:
            alive_rows = {pid for pid, _, _ in _ps()}
            left = {p for p in seen if p in alive_rows} | _with_run_dir(run_dir, me)
            if not left:
                break
            time.sleep(0.5)
        assert not left, (
            f"processes still alive after SIGTERM to the orchestrator: "
            f"{sorted((p, c[:110]) for p, _, c in _ps() if p in left)}")
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)  # the whole group: nothing outlives this test
        for pid in seen:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        proc.wait()

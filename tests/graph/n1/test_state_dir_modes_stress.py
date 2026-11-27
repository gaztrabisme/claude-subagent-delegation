"""N1-6: the h_fix1 file-mode scenario, ten times at once.

Records which entry is wider than 0600 (files) / 0700 (directories) in which run.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
RUNS = 10
DEADLINE_S = 85.0


def test_state_dir_modes_never_wider_in_ten_parallel_full_runs(tmp_path):
    pytest.importorskip("temporalio")
    binary = os.environ.get("TEMPORAL_BIN") or shutil.which("temporal")
    if not binary:
        pytest.skip("Temporal CLI not found")
    env = {**os.environ, "TEMPORAL_BIN": binary, "PYTHONPATH": f"{REPO}:{REPO / 'src'}"}
    scratch = []
    for i in range(RUNS):
        d = tmp_path / f"run{i}"
        d.mkdir()
        scratch.append(d)
    procs = [subprocess.Popen(
        [sys.executable, "-m", "tests.graph.n1._modes_child", str(d)], cwd=REPO, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        for d in scratch]
    outputs: dict[int, str] = {}
    try:
        deadline = time.monotonic() + DEADLINE_S
        for i, proc in enumerate(procs):
            try:
                outputs[i], _ = proc.communicate(timeout=max(1.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                outputs[i] = "TIMEOUT"
    finally:
        for proc in procs:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    wider: dict[int, list[str]] = {}
    broken: dict[int, str] = {}
    for i, text in outputs.items():
        line = next((ln for ln in text.splitlines() if ln.startswith("RESULT ")), None)
        if line is None:
            broken[i] = text[-600:]
            continue
        data = json.loads(line[len("RESULT "):])
        if data["state"] != "completed":
            broken[i] = f"run ended {data['state']}"
        if data["wider"]:
            wider[i] = data["wider"]
    assert not wider, f"entries wider than 0600/0700 (run -> entries): {wider}"
    assert not broken, f"runs that did not finish cleanly (run -> tail): {broken}"

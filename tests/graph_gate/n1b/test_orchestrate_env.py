"""N6/N5 fix: orchestrate.py finds `temporal` on PATH and gives children a minimal env."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from argparse import Namespace

import pytest

from ._util import ORCH, REPO, descendants, load, ps


def test_source_has_no_hard_coded_homebrew_path():
    assert "/opt/homebrew" not in ORCH.read_text(encoding="utf-8")


def _gate(tmp_path):
    mod = load(ORCH, "orchestrate_under_test")
    args = Namespace(run_root=str(tmp_path / "runs"), dispatch="fake", port=45999,
                     omlx_estimate=0.0, deepseek_estimate=0.05, budget_cap=0.3)
    return mod.Gate(args)


def test_default_binary_comes_from_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "temporal"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("GATE_AUDIT_LOG", "restore-me")  # Gate() sets it; monkeypatch restores
    monkeypatch.delenv("TEMPORAL_BIN", raising=False)
    monkeypatch.setenv("PATH", str(bindir))
    assert os.path.realpath(str(_gate(tmp_path).temporal)) == os.path.realpath(str(fake))


def test_temporal_bin_overrides_path(tmp_path, monkeypatch):
    monkeypatch.setenv("GATE_AUDIT_LOG", "restore-me")
    monkeypatch.setenv("TEMPORAL_BIN", "/somewhere/else/temporal")
    assert str(_gate(tmp_path).temporal) == "/somewhere/else/temporal"


def test_children_do_not_inherit_the_parent_environment(tmp_path):
    binary = os.environ.get("TEMPORAL_BIN") or shutil.which("temporal")
    if not binary:
        pytest.skip("Temporal CLI not found")
    pytest.importorskip("temporalio")
    run_root = tmp_path / "runs"
    # A spy stands in for `temporal`: it records its own environment, then execs the real one.
    spy_out = tmp_path / "server.env"
    spy = tmp_path / "temporal-spy"
    spy.write_text(f'#!/bin/sh\n/usr/bin/env > "{spy_out}"\nexec "{binary}" "$@"\n')
    spy.chmod(spy.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "TEMPORAL_BIN": str(spy), "SUBAGENT_TEST_SECRET": "x"}
    proc = subprocess.Popen(
        [sys.executable, str(ORCH), "--dispatch", "fake", "--run-root", str(run_root),
         "--timeout", "120"],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True)
    seen: set[int] = set()
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            dirs = sorted(run_root.glob("gate-fake-*")) if run_root.exists() else []
            if dirs and (dirs[0] / "audit.jsonl").exists() and (
                    "worker_started" in (dirs[0] / "audit.jsonl").read_text(errors="replace")):
                break
            assert proc.poll() is None, "orchestrate.py exited before its worker started"
            time.sleep(0.3)
        else:
            pytest.fail("the worker never started")
        time.sleep(2.0)
        kids = descendants(proc.pid)
        seen |= kids
        assert kids, "orchestrate.py has no child processes to test against"
        assert spy_out.exists(), "the server was not started through TEMPORAL_BIN"
        assert "SUBAGENT_TEST_SECRET" not in spy_out.read_text(errors="replace"), (
            "the Temporal server inherited the parent's environment")
        rows = {pid: cmd for pid, _, cmd in ps()}
        if "SUBAGENT_TEST_SECRET=x" in rows.get(proc.pid, ""):  # ps shows environments here
            leaked = sorted((pid, rows[pid][:100]) for pid in kids
                            if "SUBAGENT_TEST_SECRET=x" in rows.get(pid, ""))
            assert not leaked, f"children inherited the parent's environment: {leaked}"
    finally:
        proc.send_signal(signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=60)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        for pid in seen:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        proc.wait()

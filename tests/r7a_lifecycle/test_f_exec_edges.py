"""R7a lifecycle (f): edges of `subagent exec`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from subagent import cli, config
from subagent.lane_status import LaneStatusStore

from ..conftest import make_settings
from ..r6._helpers import init_git_repo, read_trace


def _project(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    make_settings(tmp_path, workspace=str(project), trace=str(trace_path))
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    return project, trace_path


def _runs(trace_path: Path):
    return [r for r in read_trace(trace_path) if r.get("kind") == "run"]


def test_f_a_lane_probe_lock_timeout_is_one_failed_trace_record_and_exit_1(
    tmp_path, monkeypatch, capsys
):
    project, trace_path = _project(tmp_path, monkeypatch)
    marker = project / "ran.txt"

    def locked_out(self, lane):
        raise TimeoutError("timed out waiting for lane probe to finish")

    monkeypatch.setattr(LaneStatusStore, "active_run", locked_out)
    rc = cli.main([
        "--root", str(project), "exec", "glm", "--",
        sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')",
    ])
    err = capsys.readouterr().err
    assert rc == 1
    assert "error" in err.lower()
    assert "Traceback" not in err
    assert not marker.exists()
    records = _runs(trace_path)
    assert len(records) == 1
    assert records[0]["entry"] == "exec-wrapper"
    assert records[0]["state"] == "failed"


def test_f_a_command_killed_by_a_signal_exits_128_plus_the_signal(tmp_path, monkeypatch):
    project, trace_path = _project(tmp_path, monkeypatch)
    real_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "killed-by-signal":
            return SimpleNamespace(returncode=-9)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    rc = cli.main(["--root", str(project), "exec", "glm", "--", "killed-by-signal"])
    assert rc == 137
    records = _runs(trace_path)
    assert len(records) == 1
    assert records[0]["exit_code"] == 137
    assert records[0]["state"] == "failed"


def test_f_a_symlink_created_under_a_locked_glob_is_a_violation(tmp_path, monkeypatch, capsys):
    project, trace_path = _project(tmp_path, monkeypatch)
    rev = init_git_repo(project, {"contracts/api.json": '{"v": 1}\n'})
    rc = cli.main([
        "--root", str(project), "exec", "glm", "--lock-commit", rev,
        "--locked", "contracts/*.json", "--",
        sys.executable, "-c",
        "import os; os.symlink('api.json', 'contracts/new.json')",
    ])
    err = capsys.readouterr().err
    assert rc == 3
    assert "contracts/new.json" in err
    records = _runs(trace_path)
    assert len(records) == 1
    assert records[0]["lock"] == "violated"
    assert records[0]["finish_reason"] == "violated_tests"

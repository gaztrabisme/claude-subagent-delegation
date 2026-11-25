"""Pins four review findings on `subagent exec` and `subagent promote`."""

from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from subagent import cli, config
from subagent.lane_status import LaneStatusStore

from ..conftest import make_settings
from ..r6._helpers import init_git_repo, read_trace
from ..r6.test_d_promote import _promote_mod, _recording_runner

OPTION_LIKE = "--abbrev-ref"
FILES = {
    "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
    "tests/test_sample.py": "def test_ok():\n    assert True\n",
}


def _setup(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    settings = make_settings(tmp_path, workspace=str(project), trace=str(trace_path))
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    return project, trace_path, settings


def _spy_subprocess_run(monkeypatch, *, fail_ls_tree: bool = False) -> list[list[str]]:
    """Record every subprocess.run argv seen by cli; optionally fail `git ls-tree`."""
    calls: list[list[str]] = []
    real = subprocess.run

    def spy(argv: Any, *args: Any, **kwargs: Any):
        cmd = [str(p) for p in argv] if isinstance(argv, (list, tuple)) else [str(argv)]
        calls.append(cmd)
        if fail_ls_tree and "ls-tree" in cmd:
            return subprocess.CompletedProcess(cmd, 128, stdout=b"", stderr=b"fatal: boom")
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", spy)
    return calls


def _rev_parse_calls(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if "rev-parse" in c]


# 1. exec --lock-commit option injection

def test_exec_lock_commit_option_like_rev_exits_2_before_argv_runs(tmp_path: Path, monkeypatch):
    project, trace_path, _ = _setup(tmp_path, monkeypatch)
    init_git_repo(project, FILES)
    marker = project / "ran.txt"
    calls = _spy_subprocess_run(monkeypatch)
    rc = cli.main([
        "--root", str(project),
        "exec", "glm", f"--lock-commit={OPTION_LIKE}",
        "--", sys.executable, "-c",
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')",
    ])
    assert rc == 2
    assert not marker.exists()
    assert read_trace(trace_path) == []
    # the rev must never be parsed by git as an option
    for call in _rev_parse_calls(calls):
        assert "--end-of-options" in call, call


def test_exec_lock_commit_rev_parse_places_end_of_options_before_rev(tmp_path: Path, monkeypatch):
    project, _, _ = _setup(tmp_path, monkeypatch)
    rev = init_git_repo(project, FILES)
    calls = _spy_subprocess_run(monkeypatch)
    rc = cli.main([
        "--root", str(project),
        "exec", "glm", "--lock-commit", rev,
        "--", sys.executable, "-c", "pass",
    ])
    assert rc == 0
    parses = _rev_parse_calls(calls)
    assert parses
    argv = parses[0]
    assert "--end-of-options" in argv
    idx = argv.index("--end-of-options")
    assert argv[idx + 1].startswith(rev)
    assert argv.index("rev-parse") < idx


# 2. promote option injection

def test_promote_option_like_commit_raises_unknown_commit_without_archive(tmp_path: Path):
    mod = _promote_mod()
    repo = tmp_path / "repo"
    init_git_repo(repo, {"README.md": "x\n"})
    settings = make_settings(tmp_path, workspace=str(repo))
    recorded: list[list[str]] = []
    with pytest.raises(mod.UnknownCommit) as exc:
        mod.promote(repo, OPTION_LIKE, settings, runner=_recording_runner(recorded))
    assert exc.value.code == 2
    assert not any("archive" in cmd for cmd in recorded)
    for cmd in recorded:
        if "rev-parse" in cmd:
            assert "--end-of-options" in cmd, cmd


def test_promote_rev_parse_places_end_of_options_immediately_before_rev(tmp_path: Path):
    mod = _promote_mod()
    repo = tmp_path / "repo"
    sha = init_git_repo(repo, {"README.md": "x\n"})
    settings = make_settings(tmp_path, workspace=str(repo))
    recorded: list[list[str]] = []
    with contextlib.suppress(Exception):  # install steps are not under test
        mod.promote(repo, sha, settings, runner=_recording_runner(recorded))
    parses = [c for c in recorded if "rev-parse" in c]
    assert parses
    argv = parses[0]
    assert "--end-of-options" in argv
    idx = argv.index("--end-of-options")
    assert argv[idx + 1].startswith(sha)


# 3. exec fails closed when ls-tree fails

def test_exec_lock_listing_failure_fails_closed(tmp_path: Path, monkeypatch):
    project, trace_path, _ = _setup(tmp_path, monkeypatch)
    rev = init_git_repo(project, FILES)
    _spy_subprocess_run(monkeypatch, fail_ls_tree=True)
    rc = cli.main([
        "--root", str(project),
        "exec", "glm", "--lock-commit", rev,
        "--", sys.executable, "-c",
        "import pathlib; pathlib.Path('tests/test_sample.py').unlink()",
    ])
    assert rc in (2, 3)
    assert not (project / "tests" / "test_sample.py").exists() or rc != 0
    for rec in (r for r in read_trace(trace_path) if r.get("kind") == "run"):
        assert rec["lock"] != "ok"
        assert rec["state"] == "failed"


# 4. exec interruption

def test_exec_keyboard_interrupt_traces_once_releases_lease_returns_130(
    tmp_path: Path, monkeypatch
):
    project, trace_path, settings = _setup(tmp_path, monkeypatch)
    real = subprocess.run

    def interrupting(argv: Any, *args: Any, **kwargs: Any):
        if isinstance(argv, (list, tuple)) and str(argv[0]) == sys.executable:
            raise KeyboardInterrupt
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", interrupting)
    try:
        rc = cli.main([
            "--root", str(project),
            "exec", "glm",
            "--", sys.executable, "-c", "pass",
        ])
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt propagated out of exec")
    assert rc == 130

    runs = [r for r in read_trace(trace_path) if r.get("kind") == "run"]
    assert len(runs) == 1
    assert runs[0]["state"] == "failed"
    assert runs[0]["entry"] == "exec-wrapper"
    assert LaneStatusStore(settings.session_root).active_count("glm") == 0

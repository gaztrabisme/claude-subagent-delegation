"""Acceptance tests for R6(a): `subagent exec <lane> ... -- <argv...>`."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from subagent import cli, config
from subagent.lane_status import LaneStatusStore

from ..conftest import make_settings
from ..test_trace_schema import REQUIRED, problems
from ._helpers import commit_files, init_git_repo, read_trace


def _setup_project(tmp_path: Path, monkeypatch, *, trace_name: str = "trace.jsonl"):
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    trace_path = tmp_path / trace_name
    settings = make_settings(
        tmp_path,
        workspace=str(project),
        trace=str(trace_path),
    )
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    return project, trace_path, settings


def test_a_exec_subcommand_registered_in_cli(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["exec", "--help"])
    captured = capsys.readouterr()
    assert exc.value.code == 0, captured.err


def test_a_exec_unknown_lane_exits_2_without_tracing(tmp_path: Path, monkeypatch, capsys):
    project, trace_path, _ = _setup_project(tmp_path, monkeypatch)
    marker = project / "should_not_exist.txt"

    try:
        rc = cli.main([
            "--root", str(project),
            "exec", "nonexistent-lane",
            "--", sys.executable, "-c",
            f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')",
        ])
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 2

    captured = capsys.readouterr()
    assert "invalid choice" not in captured.err
    assert rc == 2
    assert not marker.exists()
    assert read_trace(trace_path) == []


def test_a_exec_success_traces_run_and_charges_lane_status(tmp_path: Path, monkeypatch):
    project, trace_path, settings = _setup_project(tmp_path, monkeypatch)
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)

    cwd_file = project / "observed_cwd.txt"
    rc = cli.main([
        "--root", str(project),
        "exec", "glm",
        "--", sys.executable, "-c",
        f"import os, pathlib; pathlib.Path({str(cwd_file)!r}).write_text(os.getcwd())",
    ])
    assert rc == 0
    assert Path(cwd_file.read_text(encoding="utf-8")).resolve() == project.resolve()

    records = read_trace(trace_path)
    run_records = [r for r in records if r.get("kind") == "run"]
    assert len(run_records) == 1
    rec = run_records[0]

    cfg = settings.providers["glm"]
    assert rec["entry"] == "exec-wrapper"
    assert rec["lane"] == "glm"
    assert rec["provider"] == cfg.vendor
    assert rec["driver"] == cfg.driver
    assert Path(rec["workspace"]).resolve() == project.resolve()
    assert rec["state"] == "completed"
    assert rec["finish_reason"] == "complete"
    assert rec["exit_code"] == 0
    assert rec["lock"] is None
    assert REQUIRED["run"] <= set(rec)
    assert problems(rec) == []
    assert all(
        rec["usage"][k] == 0
        for k in ("input", "output", "cache_read", "cache_write", "reasoning", "total", "steps", "turns")
    )

    snap = LaneStatusStore(settings.session_root).snapshot(settings)["glm"]
    assert snap["checked_at"] is not None
    assert snap["source"] == "run"
    assert snap["state"] == "open"


def test_a_exec_nonzero_exit_preserves_code_and_traces_failed(tmp_path: Path, monkeypatch):
    project, trace_path, _ = _setup_project(tmp_path, monkeypatch)
    rc = cli.main([
        "--root", str(project),
        "exec", "glm",
        "--", sys.executable, "-c", "import sys; sys.exit(7)",
    ])
    assert rc == 7

    run_records = [r for r in read_trace(trace_path) if r.get("kind") == "run"]
    assert len(run_records) == 1
    rec = run_records[0]
    assert rec["entry"] == "exec-wrapper"
    assert rec["state"] == "failed"
    assert rec["finish_reason"] == "error"
    assert rec["exit_code"] == 7
    assert rec["lock"] is None
    assert REQUIRED["run"] <= set(rec)
    assert problems(rec) == []


def test_a_exec_missing_binary_exits_127_and_traces_failed(tmp_path: Path, monkeypatch):
    project, trace_path, _ = _setup_project(tmp_path, monkeypatch)
    rc = cli.main([
        "--root", str(project),
        "exec", "glm",
        "--", "/nonexistent/subagent-r6-missing-binary",
    ])
    assert rc == 127

    run_records = [r for r in read_trace(trace_path) if r.get("kind") == "run"]
    assert len(run_records) == 1
    rec = run_records[0]
    assert rec["entry"] == "exec-wrapper"
    assert rec["state"] == "failed"
    assert rec["finish_reason"] == "error"
    assert rec["exit_code"] == 127
    assert rec["lock"] is None
    assert REQUIRED["run"] <= set(rec)
    assert problems(rec) == []


def test_a_exec_holds_active_lane_lease_while_running(tmp_path: Path, monkeypatch):
    project, _, settings = _setup_project(tmp_path, monkeypatch)
    ready_file = tmp_path / "ready"
    go_file = tmp_path / "go"
    store = LaneStatusStore(settings.session_root)
    assert store.active_count("glm") == 0

    observed_during: list[int] = []
    errors: list[BaseException] = []

    def observer() -> None:
        try:
            deadline = time.monotonic() + 10.0
            while not ready_file.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("child never signalled ready")
                time.sleep(0.02)
            observed_during.append(store.active_count("glm"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            go_file.write_text("go", encoding="utf-8")

    thread = threading.Thread(target=observer, daemon=True)
    thread.start()

    child_code = (
        "import pathlib, time; "
        f"pathlib.Path({str(ready_file)!r}).write_text('1'); "
        f"deadline = time.monotonic() + 10.0; "
        f"\nwhile not pathlib.Path({str(go_file)!r}).exists() and time.monotonic() < deadline: time.sleep(0.02)\n"
    )
    rc = cli.main([
        "--root", str(project),
        "exec", "glm",
        "--", sys.executable, "-c", child_code,
    ])
    thread.join(timeout=5.0)
    assert not errors, errors
    assert rc == 0
    assert observed_during == [1]
    assert store.active_count("glm") == 0


def test_a_exec_lock_commit_unknown_rev_exits_2_before_running(tmp_path: Path, monkeypatch):
    project, trace_path, _ = _setup_project(tmp_path, monkeypatch)
    init_git_repo(project, {"tests/test_sample.py": "def test_ok(): pass\n"})
    marker = project / "ran.txt"

    rc = cli.main([
        "--root", str(project),
        "exec", "glm", "--lock-commit", "deadbeef00000000",
        "--", sys.executable, "-c",
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')",
    ])
    assert rc == 2
    assert not marker.exists()
    assert read_trace(trace_path) == []


def test_a_exec_lock_commit_unchanged_default_globs_returns_ok(tmp_path: Path, monkeypatch):
    project, trace_path, _ = _setup_project(tmp_path, monkeypatch)
    rev = init_git_repo(
        project,
        {
            "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
            "tests/test_sample.py": "def test_ok():\n    assert True\n",
            "src_mod.py": "VALUE = 1\n",
        },
    )
    rc = cli.main([
        "--root", str(project),
        "exec", "glm", "--lock-commit", rev,
        "--", sys.executable, "-c",
        "import pathlib; pathlib.Path('src_mod.py').write_text('VALUE = 2\\n')",
    ])
    assert rc == 0

    run_records = [r for r in read_trace(trace_path) if r.get("kind") == "run"]
    assert len(run_records) == 1
    rec = run_records[0]
    assert rec["lock"] == "ok"
    assert rec["state"] == "completed"
    assert rec["finish_reason"] == "complete"
    assert rec["exit_code"] == 0


@pytest.mark.parametrize(
    ("kind", "argv_exit", "expected_path"),
    [
        ("modified", 0, "tests/test_sample.py"),
        ("deleted", 0, "tests/test_sample.py"),
        ("added", 0, "tests/test_added.py"),
        ("modified", 1, "tests/test_sample.py"),
    ],
)
def test_a_exec_lock_commit_violation_exits_3_and_prints_paths(
    tmp_path: Path, monkeypatch, capsys, kind: str, argv_exit: int, expected_path: str
):
    project, trace_path, _ = _setup_project(tmp_path, monkeypatch)
    rev = init_git_repo(
        project,
        {
            "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
            "tests/test_sample.py": "def test_ok():\n    assert True\n",
        },
    )
    if kind == "modified":
        action = "pathlib.Path('tests/test_sample.py').write_text('def test_ok(): assert False\\n')"
    elif kind == "deleted":
        action = "pathlib.Path('tests/test_sample.py').unlink()"
    else:
        action = "pathlib.Path('tests/test_added.py').write_text('def test_new(): pass\\n')"

    rc = cli.main([
        "--root", str(project),
        "exec", "glm", "--lock-commit", rev,
        "--", sys.executable, "-c",
        f"import pathlib, sys; {action}; sys.exit({argv_exit})",
    ])
    captured = capsys.readouterr()
    assert rc == 3
    assert expected_path in captured.err

    run_records = [r for r in read_trace(trace_path) if r.get("kind") == "run"]
    assert len(run_records) == 1
    rec = run_records[0]
    assert rec["lock"] == "violated"
    assert rec["state"] == "failed"
    assert rec["finish_reason"] == "violated_tests"
    assert isinstance(rec["exit_code"], int)


def test_a_exec_locked_explicit_glob_scopes_lock_check(tmp_path: Path, monkeypatch, capsys):
    project, trace_path, _ = _setup_project(tmp_path, monkeypatch)
    rev = init_git_repo(
        project,
        {
            "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
            "tests/test_sample.py": "def test_ok(): pass\n",
            "contracts/api.json": '{"v": 1}\n',
        },
    )
    # Editing tests/test_sample.py while locking only contracts/*.json succeeds.
    rc_ok = cli.main([
        "--root", str(project),
        "exec", "glm", "--lock-commit", rev, "--locked", "contracts/*.json",
        "--", sys.executable, "-c",
        "import pathlib; pathlib.Path('tests/test_sample.py').write_text('def test_ok(): assert 1\\n')",
    ])
    assert rc_ok == 0

    rev2 = commit_files(project, {"tests/test_sample.py": "def test_ok(): assert 1\n"}, "update test")

    # Editing contracts/api.json triggers violation against rev2.
    rc_bad = cli.main([
        "--root", str(project),
        "exec", "glm", "--lock-commit", rev2, "--locked", "contracts/*.json",
        "--", sys.executable, "-c",
        "import pathlib; pathlib.Path('contracts/api.json').write_text('{\"v\": 2}\\n')",
    ])
    captured = capsys.readouterr()
    assert rc_bad == 3
    assert "contracts/api.json" in captured.err

    run_records = [r for r in read_trace(trace_path) if r.get("kind") == "run"]
    assert len(run_records) == 2
    assert run_records[0]["lock"] == "ok"
    assert run_records[1]["lock"] == "violated"
    assert json.loads((project / "contracts/api.json").read_text(encoding="utf-8")) == {"v": 2}

"""The loop commands' server split: `run wait review` need it, the rest do not.

Before the split every loop command started the in-process server, so even
`subagent detect` died with `RuntimeError: approval socket did not start` when
the caller could not bind Unix sockets — the case in a bench cell whose
orchestrator runs under Codex's sandbox.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from subagent import config
from subagent.cli import main
from subagent.loop import loop


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """An empty config home, so `config.load` sees only what a test writes."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    return home


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def _agent_config(tmp_path: Path) -> Path:
    """A configured provider and external supervisor for loop command tests."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[core]\ndefault_provider = "codex"\n\n'
        '[guard]\nsupervisor = "agent"\nsupervisor_cmd = "subagent serve"\n\n'
        '[providers.codex]\ndriver = "codex"\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def stub_server(monkeypatch):
    """The in-process server replaced by one that records start/stop and does nothing."""
    events: list[str] = []

    class StubServer:
        def __init__(self, session_root, settings, approval_socket=None):
            self.session_root = session_root
            self.settings = settings

        def start(self):
            events.append("start")
            return self

        def stop(self):
            events.append("stop")

    monkeypatch.setattr("subagent.core.InProcessServer", StubServer)
    return events


def _forbid_the_socket(monkeypatch) -> None:
    def start(self):
        raise RuntimeError("approval socket did not start: PermissionError")

    monkeypatch.setattr("subagent.core.InProcessServer.start", start)


@pytest.mark.parametrize("command,exit_code", [("detect", 0), ("checkpoints", 0), ("test", 2)])
def test_read_only_commands_work_where_no_socket_is_possible(
        tmp_path, monkeypatch, capsys, home, command, exit_code):
    monkeypatch.setenv(config.CONFIG_ENV, str(_agent_config(tmp_path)))
    root = _project(tmp_path)
    _forbid_the_socket(monkeypatch)
    assert main(["--root", str(root), command]) == exit_code
    data = json.loads(capsys.readouterr().out)
    if command == "checkpoints":
        assert data == {"checkpoints": []}
    elif command == "test":
        assert data["passed"] is False  # an empty project has no test command
    else:
        assert "test_cmd" in data  # detect


def test_run_wait_review_still_start_the_server(
        tmp_path, monkeypatch, capsys, home, stub_server):
    root = _project(tmp_path)
    monkeypatch.setenv(config.CONFIG_ENV, str(_agent_config(tmp_path)))
    assert main(["--root", str(root), "wait", "--timeout", "0"]) == 2
    assert stub_server == ["start", "stop"]  # started, and stopped on the way out
    assert json.loads(capsys.readouterr().out)["status"] == "no_run"


def test_run_refuses_live_run_and_points_to_subagent_wait(
        tmp_path, monkeypatch, capsys, home, stub_server):
    root = _project(tmp_path)
    monkeypatch.setenv(config.CONFIG_ENV, str(_agent_config(tmp_path)))
    plan = tmp_path / "plan.md"
    plan.write_text("Implement the task.\n", encoding="utf-8")
    state = root / ".subagent"
    (state / "runs").mkdir(parents=True)
    live_id = "live-run-123"
    (state / "current.json").write_text(
        json.dumps({"run_id": live_id, "pid": os.getpid(), "started": 1}),
        encoding="utf-8",
    )
    lock = (state / "run.lock").open("a")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def no_new_process(*args, **kwargs):
        pytest.fail("a second run was spawned while the first run was live")

    monkeypatch.setattr("subagent.loop.loop.subprocess.Popen", no_new_process)
    code = main(["--root", str(root), "run", "--plan", str(plan), "--wait", "540"])

    captured = capsys.readouterr()
    assert code != 0
    assert json.loads(captured.out) == {
        "status": "busy",
        "error": f"run {live_id} is still in progress; use `subagent wait`",
    }
    lock.close()


def test_review_a_foreground_custom_run_id_records_the_live_process(
        tmp_path, monkeypatch, capsys, home, stub_server):
    root = _project(tmp_path)
    monkeypatch.setenv(config.CONFIG_ENV, str(_agent_config(tmp_path)))
    plan = tmp_path / "plan.md"
    plan.write_text("Implement the task.\n", encoding="utf-8")
    monkeypatch.setattr("subagent.loop.loop.run_round", lambda *args, **kwargs: {
        "status": "done", "_runs": [],
    })

    assert main(["--root", str(root), "run", "--plan", str(plan),
                 "--run-id", "review-a-custom"]) == 0

    current = json.loads((root / ".subagent" / "current.json").read_text())
    assert current["run_id"] == "review-a-custom"
    assert current["pid"] == os.getpid()


def test_review_a_recycled_pid_does_not_wedge_run_or_wait(
        tmp_path, monkeypatch, capsys, home, stub_server):
    root = _project(tmp_path)
    monkeypatch.setenv(config.CONFIG_ENV, str(_agent_config(tmp_path)))
    plan = tmp_path / "plan.md"
    plan.write_text("Implement the task.\n", encoding="utf-8")
    state = root / ".subagent"
    (state / "runs").mkdir(parents=True)
    (state / "current.json").write_text(json.dumps({
        "run_id": "orphan-run", "pid": os.getpid(), "started": 1,
    }), encoding="utf-8")

    waited, code = loop.cmd_wait(root, None, SimpleNamespace(timeout=0))
    assert code == 2
    assert waited["status"] == "crashed"

    monkeypatch.setattr("subagent.loop.loop.autopilot", lambda *args: {"status": "done"})
    assert main(["--root", str(root), "run", "--auto", "--plan", str(plan)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "done"


def test_review_a_report_loads_the_project_config_from_its_directory(
        tmp_path, monkeypatch, home):
    root = _project(tmp_path)
    config_path = _agent_config(tmp_path)
    state = root / ".subagent"
    state.mkdir()
    (state / "config.toml").write_text(config_path.read_text(encoding="utf-8"),
                                       encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    called = []
    monkeypatch.setattr("subagent.report.main", lambda raw: called.append(raw) or 0)

    assert main(["report"]) == 0
    assert called == [[]]


def test_review_a_subdirectory_command_finds_the_ancestor_project_config(
        tmp_path, monkeypatch, capsys, home):
    root = _project(tmp_path)
    config_path = _agent_config(tmp_path)
    state = root / ".subagent"
    state.mkdir()
    (state / "config.toml").write_text(config_path.read_text(encoding="utf-8"),
                                       encoding="utf-8")
    nested = root / "src"
    nested.mkdir()
    monkeypatch.chdir(nested)
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    _forbid_the_socket(monkeypatch)

    assert main(["detect"]) == 0
    assert "test_cmd" in json.loads(capsys.readouterr().out)


def test_the_read_only_handlers_still_see_the_settings(
        tmp_path, monkeypatch, capsys, home):
    """The stand-in carries `settings`, so `[loop]` config still reaches `detect`."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[core]\ndefault_provider = "codex"\n\n'
        '[loop]\ntest_cmd = "echo ok"\n\n'
        '[providers.codex]\ndriver = "codex"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(config.CONFIG_ENV, str(path))
    root = _project(tmp_path)
    _forbid_the_socket(monkeypatch)
    assert main(["--root", str(root), "detect"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["test_cmd"] == "echo ok"
    assert data["framework"] == "custom"


def test_checkpoints_never_built_a_server(tmp_path, monkeypatch, capsys, home, stub_server):
    root = _project(tmp_path)
    monkeypatch.setenv(config.CONFIG_ENV, str(_agent_config(tmp_path)))
    assert main(["--root", str(root), "checkpoints"]) == 0
    assert stub_server == []


@pytest.mark.parametrize("command", ["detect", "run", "report"])
def test_commands_require_a_provider(tmp_path, monkeypatch, capsys, home, command):
    empty_config = tmp_path / "empty.toml"
    empty_config.write_text("")
    monkeypatch.setenv(config.CONFIG_ENV, str(empty_config))
    root = _project(tmp_path)

    args = [command] if command == "report" else ["--root", str(root), command]
    if command == "run":
        args += ["--plan", str(tmp_path / "missing-plan.md")]
    assert main(args) == 1
    captured = capsys.readouterr()
    assert "error: no providers configured; run `subagent init`" in captured.err

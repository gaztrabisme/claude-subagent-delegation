from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from subagent.runs import CANCELLED, COMPLETED, COMPLETED_UNVERIFIED, FAILED, Registry

from .conftest import make_settings


class FakeProcess:
    def __init__(self, events: list[dict[str, Any]], argv: list[str], env: dict[str, str], gate=None):
        self.argv = argv
        self.env = env
        self._events = events
        self._gate = gate
        self.killed = False

    def events(self) -> Iterator[dict[str, Any]]:
        if self._gate is not None:
            self._gate.wait(timeout=5)
        yield from self._events

    def kill(self) -> None:
        self.killed = True
        if self._gate is not None:
            self._gate.set()


def _result(text: str, session_id: str = "sess-1", **extra: Any) -> list[dict[str, Any]]:
    return [
        {"type": "system", "subtype": "init", "session_id": session_id},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "session_id": session_id,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "num_turns": 1,
            **extra,
        },
    ]


@pytest.fixture
def registry(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path, idle_timeout=3600, run_timeout=30)
    captured: list[FakeProcess] = []

    def spawn(argv, env, cwd):
        proc = FakeProcess(_result("done"), argv, env)
        captured.append(proc)
        return proc

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    reg = Registry(settings, start_reaper=False)
    reg.captured = captured  # type: ignore[attr-defined]
    yield reg
    reg.shutdown()


def _wait(run, timeout=5):
    assert run.done.wait(timeout), f"run did not finish: {run.state} {run.error}"
    return run


def test_completed_verbatim_under_cap(registry, tmp_path: Path):
    agent = registry.create_agent("t", tmp_path, "glm-5.3[1m]")
    assert agent.wait_ready(5) is None or "not on PATH" not in (agent.wait_ready(0) or "")
    run = _wait(agent.submit("do the thing", verification="true"))
    assert run.state == COMPLETED
    assert run.result_text == "done"
    assert run.distilled is False
    argv = registry.captured[0].argv  # type: ignore[attr-defined]
    assert "--dangerously-skip-permissions" in argv


def test_child_argv_runs_the_guard_hook(tmp_path: Path, monkeypatch):
    """--bare skips hooks, so the guard never ran on a child call (report item 0)."""
    settings = make_settings(tmp_path, supervisor="agent")
    captured: list[FakeProcess] = []

    def spawn(argv, env, cwd):
        captured.append(FakeProcess(_result("done"), argv, env))
        return captured[-1]

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    registry = Registry(settings, start_reaper=False)
    registry.captured = captured  # type: ignore[attr-defined]
    agent = registry.create_agent("t", tmp_path, "glm-5.3[1m]")
    _wait(agent.submit("do", verification="true"))
    registry.shutdown()
    argv = registry.captured[0].argv  # type: ignore[attr-defined]
    assert "--bare" not in argv
    settings_file = Path(argv[argv.index("--settings") + 1])
    assert settings_file == agent.settings.hooks_config(agent.agent_id)
    hooks = json.loads(settings_file.read_text())["hooks"]["PreToolUse"]
    assert "approval_hook.py" in hooks[0]["hooks"][0]["command"]
    # What --bare used to provide, now explicit.
    assert argv[argv.index("--tools") + 1] == "Bash,Read,Edit,Write,Grep,Glob"
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    env = registry.captured[0].env  # type: ignore[attr-defined]
    assert env["CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR"] == "1"


def test_distills_when_over_cap(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path, summary_tokens=10, chars_per_token=2)
    calls: list[FakeProcess] = []
    long_text = "x" * 500

    def spawn(argv, env, cwd):
        if any(a.startswith("Stop working") or "Summarize what you did" in a for a in argv) or "--resume" in argv:
            events = _result("## Goal\nshort", session_id="sess-1")
        else:
            events = _result(long_text, session_id="sess-1")
        proc = FakeProcess(events, argv, env)
        calls.append(proc)
        return proc

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("t", tmp_path, "glm-5.3[1m]")
        run = _wait(agent.submit("big", verification="true"))
        assert run.final_response == long_text
        assert run.distilled is True
        assert "Goal" in run.result_text
        assert any("--resume" in p.argv for p in calls)
    finally:
        reg.shutdown()


def test_false_verification_is_unverified(registry, tmp_path: Path):
    agent = registry.create_agent("t", tmp_path, "glm-5.3[1m]")
    run = _wait(agent.submit("do", verification="false"))
    assert run.state == COMPLETED_UNVERIFIED


def test_loop_strikes_fail_the_run(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path, loop_strikes=3)
    tool = {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
    events = [
        {"type": "system", "subtype": "init", "session_id": "sess-1"},
        {"type": "assistant", "message": {"content": [tool]}},
        {"type": "assistant", "message": {"content": [tool]}},
        {"type": "assistant", "message": {"content": [tool]}},
        {
            "type": "result",
            "is_error": False,
            "result": "looped",
            "session_id": "sess-1",
            "usage": {},
        },
    ]

    def spawn(argv, env, cwd):
        return FakeProcess(events, argv, env)

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("t", tmp_path, "glm-5.3[1m]")
        run = _wait(agent.submit("loop", verification="true"))
        assert run.state == FAILED
        assert run.trip and run.trip[0] == "loop"
    finally:
        reg.shutdown()


def test_cancel_mid_run(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    gate = threading.Event()

    def spawn(argv, env, cwd):
        return FakeProcess(_result("late"), argv, env, gate=gate)

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("t", tmp_path, "glm-5.3[1m]")
        run = agent.submit("hang", verification="true")
        agent.close()
        assert run.done.wait(5)
        assert run.state == CANCELLED
    finally:
        reg.shutdown()


def test_child_env_and_settings_isolation(registry, tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    agent = registry.create_agent("t", tmp_path, "glm-5.3[1m]")
    _wait(agent.submit("do", verification="true"))
    env = registry.captured[0].env  # type: ignore[attr-defined]
    assert env["ANTHROPIC_BASE_URL"].endswith("/api/anthropic")
    assert env["CLAUDE_CONFIG_DIR"].startswith(str(agent.settings.session_root))
    assert not (home / ".claude" / "settings.json").exists()


def test_continue_passes_resume(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    calls: list[FakeProcess] = []

    def spawn(argv, env, cwd):
        proc = FakeProcess(_result("ok", session_id="sess-9"), argv, env)
        calls.append(proc)
        return proc

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("t", tmp_path, "glm-5.3[1m]")
        _wait(agent.submit("first", verification="true"))
        _wait(agent.submit("second", verification="true"))
        assert "--resume" in calls[1].argv
        assert "sess-9" in calls[1].argv
    finally:
        reg.shutdown()

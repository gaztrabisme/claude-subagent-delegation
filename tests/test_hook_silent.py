"""A Codex run whose guard hook goes silent is stopped, not merely labelled.

Codex runs a tool call anyway when its PreToolUse hook fails to start. The
supervisor counts each hook invocation per agent (Supervisor.saw_hook), and
the run loop stops a Codex run at the first tool call that has no hook call
counted (Agent._hook_silence). The fake codex replays a fixture with 9
command items and never calls the hook; an on_event watcher stands in for a
working hook by reporting a hook call just before each tool call arrives.
"""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from subagent.guard.supervisor import Supervisor
from subagent.providers.codex import TOOL_STARTED
from subagent.runs import COMPLETED, FAILED, KILL_HOOK_SILENT, Registry
from subagent.telemetry.trace import Trace

from .conftest import make_settings


def _registry(tmp_path: Path, supervisor: str = "auto") -> tuple[Registry, Supervisor]:
    settings = make_settings(tmp_path, supervisor=supervisor, idle_timeout=3600, run_timeout=30)
    registry = Registry(settings, start_reaper=False, trace=Trace(tmp_path / "trace.jsonl"))
    return registry, Supervisor(settings, registry, trace=registry.trace)


@pytest.fixture
def served(tmp_path: Path):
    registry, supervisor = _registry(tmp_path)
    yield registry, supervisor
    registry.shutdown()


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def _hook_for_first(supervisor: Supervisor, agent_id: str, calls: int):
    """A watcher that reports a hook call before each of the first `calls` tool
    calls, at the first event naming it (Codex calls the hook before it starts one)."""
    left = [calls]
    seen: set = set()

    def watcher(event: dict) -> None:
        if event.get("type") == TOOL_STARTED:
            items = [event.get("item_id")]
        elif event.get("type") == "assistant":
            items = [block.get("id") for block in event["message"]["content"]
                     if block.get("type") == "tool_use"]
        else:
            return
        for item in items:
            if item in seen or left[0] <= 0:
                continue
            seen.add(item)
            left[0] -= 1
            supervisor.saw_hook(agent_id)

    return watcher


def _run(agent, on_event=None):
    assert agent.wait_ready(5) is None
    run = agent.submit("do it", verification="true", on_event=on_event)
    assert run.done.wait(15), f"run did not finish: {run.state} {run.error}"
    return run


def test_silent_hook_stops_the_run_at_the_first_tool_call(fake_codex, served, tmp_path):
    registry, _ = served
    agent = registry.create_agent("c", _workspace(tmp_path), provider="codex")
    run = _run(agent)
    assert run.state == FAILED
    assert run.finish_reason == KILL_HOOK_SILENT
    assert "guard hook did not run" in run.error
    assert "tool call 1 " in run.error
    # Stopped when the first of the fixture's commands started, before it finished.
    assert agent.tool_calls == 1 and run.usage.steps == 0
    assert run.verification_result is None
    records = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    record = next(r for r in records if r["kind"] == "run")
    assert record["guard"] == "sandbox (hook silent)" and record["hook_silent"] is True


def test_working_hook_lets_the_run_finish(fake_codex, served, tmp_path):
    registry, supervisor = served
    agent = registry.create_agent("c", _workspace(tmp_path), provider="codex")
    run = _run(agent, on_event=_hook_for_first(supervisor, agent.agent_id, 99))
    assert run.state == COMPLETED, run.error
    assert run.usage.steps == 9
    # The fixture starts 10 commands; the last one never completes.
    assert agent.hook_calls == agent.tool_calls == 10


def test_hook_that_stops_mid_run_is_caught_at_that_call(fake_codex, served, tmp_path):
    registry, supervisor = served
    agent = registry.create_agent("c", _workspace(tmp_path), provider="codex")
    run = _run(agent, on_event=_hook_for_first(supervisor, agent.agent_id, 3))
    assert run.state == FAILED and run.finish_reason == KILL_HOOK_SILENT
    assert run.usage.steps == 3  # three guarded commands finished; the fourth was stopped
    assert "tool call 4 " in run.error and "3 hook call(s) for 4 tool call(s)" in run.error


def _codex_agent(served, tmp_path):
    registry, _ = served
    agent = registry.create_agent("c", _workspace(tmp_path), provider="codex")
    assert agent.wait_ready(5) is None
    return agent


def _completed(item_id=None):
    block = {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
    if item_id:
        block["id"] = item_id
    return {"type": "assistant", "message": {"content": [block]}}


def test_a_tool_call_is_counted_once_across_start_and_finish(fake_codex, served, tmp_path):
    agent = _codex_agent(served, tmp_path)
    run = agent.submit("x", verification="true")
    run.done.wait(15)
    agent.tool_calls = agent.hook_calls = 0
    counted: set = set()
    agent.saw_hook()
    assert agent._hook_silence(run, {"type": TOOL_STARTED, "item_id": "item_1",
                                     "name": "Bash"}, counted) is None
    assert agent._hook_silence(run, _completed("item_1"), counted) is None
    assert agent.tool_calls == 1


def test_a_tool_call_without_a_start_event_is_counted_at_completion(fake_codex, served,
                                                                   tmp_path):
    agent = _codex_agent(served, tmp_path)
    run = agent.submit("x", verification="true")
    run.done.wait(15)
    agent.tool_calls = agent.hook_calls = 0
    counted: set = set()
    trip = agent._hook_silence(run, _completed(), counted)
    assert trip is not None and trip[0] == KILL_HOOK_SILENT
    agent.saw_hook()
    agent.saw_hook()
    assert agent._hook_silence(run, _completed(), counted) is None  # 2 hooks, 2 calls


def test_a_continue_after_a_silent_stop_stays_stopped(fake_codex, served, tmp_path):
    registry, _ = served
    agent = registry.create_agent("c", _workspace(tmp_path), provider="codex")
    assert _run(agent).finish_reason == KILL_HOOK_SILENT
    again = agent.submit("go on", verification="true")
    assert again.done.wait(15)
    assert again.finish_reason == KILL_HOOK_SILENT


def test_no_counting_without_a_supervisor(fake_codex, tmp_path):
    """Registry tests without a supervisor (and supervisor = off) keep the old behaviour."""
    settings = make_settings(tmp_path, supervisor="auto", idle_timeout=3600, run_timeout=30)
    registry = Registry(settings, start_reaper=False, trace=Trace(tmp_path / "trace.jsonl"))
    try:
        agent = registry.create_agent("c", _workspace(tmp_path), provider="codex")
        assert agent.hooks_counted is False
        assert _run(agent).state == COMPLETED
    finally:
        registry.shutdown()


def test_supervisor_off_does_not_count(fake_codex, tmp_path):
    registry, _ = _registry(tmp_path, supervisor="off")
    try:
        agent = registry.create_agent("c", _workspace(tmp_path), provider="codex")
        assert agent.hooks_counted is False
        assert _run(agent).state == COMPLETED
    finally:
        registry.shutdown()


class _Stream:
    """One request in, one reply out: the shape Supervisor._handle reads."""

    def __init__(self, request: dict):
        self.inbox = (json.dumps(request) + "\n").encode()
        self.sent = b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def receive(self, _size: int) -> bytes:
        chunk, self.inbox = self.inbox, b""
        return chunk

    async def send(self, data: bytes) -> None:
        self.sent += data


def test_supervisor_counts_hook_invocations_not_questions(fake_codex, served, tmp_path):
    """A patch touching two files asks twice in one hook call; that is one hook call."""
    registry, supervisor = served
    ws = _workspace(tmp_path)
    agent = registry.create_agent("c", ws, provider="codex")
    assert agent.wait_ready(5) is None
    base = {"tool_name": "Edit", "agent_id": agent.agent_id, "workspace": str(ws)}

    async def ask_all():
        await supervisor._handle(_Stream({**base, "tool_input": {"file_path": str(ws / "a")}}))
        await supervisor._handle(_Stream({**base, "tool_input": {"file_path": str(ws / "b")},
                                          "part": 1}))

    anyio.run(ask_all)
    assert agent.hook_calls == 1

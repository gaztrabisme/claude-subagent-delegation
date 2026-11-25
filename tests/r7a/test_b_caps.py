"""R7a (b): per-run `timeout` and `max_steps` on delegate.

Effective value = min(requested, provider limit); above the limit is clamped,
never an error; zero or negative is a RegistryError. The result carries
`caps` and an overrun is a trace fact. Pinned at `Agent.delegate(...)` and the
MCP `delegate` tool.
"""

from __future__ import annotations

import importlib

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from subagent import config
from subagent.providers import claude as claude_provider
from subagent.providers.claude import exit_event
from subagent.runs import COMPLETED, FAILED, Registry, RegistryError

from ..conftest import make_settings
from ..test_runs import FakeProcess, _result
from . import _helpers as h

PROVIDER = {"max_steps": 10, "run_timeout": 30.0}


def _delegate(reg, tmp_path, **kw):
    agent = reg.create_agent("t", tmp_path, "glm")
    return agent, agent.delegate("do it", "true", **kw)


def _argv_turns(proc) -> str:
    return proc.argv[proc.argv.index("--max-turns") + 1]


def _lifetime(run) -> float:
    return run.deadline - run.created_at


@pytest.mark.parametrize("requested,effective", [(4, 4), (99, 10), (10, 10)])
def test_b_max_steps_is_the_smaller_of_request_and_provider_limit(
    tmp_path, monkeypatch, requested, effective
):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())], provider_extra=PROVIDER)
    try:
        _, run = _delegate(reg, tmp_path, max_steps=requested)
        h.wait(run)
        assert run.state == COMPLETED, run.error
        assert _argv_turns(reg.spawned[0]) == str(effective)
        assert run.detail()["caps"]["max_steps"] == {
            "requested": requested, "effective": effective}
    finally:
        reg.shutdown()


@pytest.mark.parametrize("requested,effective", [(5, 5), (500, 30)])
def test_b_timeout_is_the_smaller_of_request_and_provider_limit(
    tmp_path, monkeypatch, requested, effective
):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())], provider_extra=PROVIDER)
    try:
        _, run = _delegate(reg, tmp_path, timeout=requested)
        h.wait(run)
        assert run.state == COMPLETED, run.error
        assert abs(_lifetime(run) - effective) < 2.0
        assert run.detail()["caps"]["timeout"] == {
            "requested": requested, "effective": effective}
    finally:
        reg.shutdown()


@pytest.mark.parametrize("kwargs,word", [
    ({"timeout": 0}, "timeout"),
    ({"timeout": -1}, "timeout"),
    ({"max_steps": 0}, "max_steps"),
    ({"max_steps": -3}, "max_steps"),
])
def test_b_zero_or_negative_requests_are_refused_by_the_registry(tmp_path, monkeypatch, kwargs, word):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())], provider_extra=PROVIDER)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        with pytest.raises(RegistryError, match=word):
            agent.delegate("do it", "true", **kwargs)
        assert reg.spawned == []
    finally:
        reg.shutdown()


def test_b_omitting_both_keeps_the_provider_defaults(tmp_path, monkeypatch):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())], provider_extra=PROVIDER)
    try:
        _, run = _delegate(reg, tmp_path)
        h.wait(run)
        assert run.state == COMPLETED, run.error
        assert _argv_turns(reg.spawned[0]) == "10"
        assert abs(_lifetime(run) - 30.0) < 2.0
        assert run.finish_reason != "timeout" and run.finish_reason != "steps"
    finally:
        reg.shutdown()


def test_b_a_run_that_hits_its_own_step_cap_records_the_overrun(
    tmp_path, monkeypatch, trace_records
):
    max_turns = {
        "type": "result", "subtype": "error_max_turns", "is_error": True, "result": None,
        "num_turns": 2, "terminal_reason": "max_turns", "session_id": "sess-1",
        "errors": ["Reached maximum number of turns (2)"],
    }
    events = [h.init(), h.tool("Bash", command="ls"), max_turns,
              exit_event(1, "", max_turns)]
    reg = h.registry(tmp_path, monkeypatch, [h.script(events)], provider_extra=PROVIDER)
    try:
        _, run = _delegate(reg, tmp_path, max_steps=2)
        h.wait(run)
        assert run.state == FAILED
        assert run.finish_reason == "steps"
        assert "2" in (run.error or "")
        records = h.run_records(trace_records)
        assert records[-1]["finish_reason"] == "steps"
        assert records[-1]["extra"]["overrun"] == {"kind": "steps", "limit": 2}
    finally:
        reg.shutdown()


def test_b_a_run_that_hits_its_own_timeout_records_the_overrun(
    tmp_path, monkeypatch, trace_records
):
    clock = h.Clock()
    monkeypatch.setattr("subagent.runs._now", clock)
    reg = h.registry(tmp_path, monkeypatch, [h.blocked()], provider_extra=PROVIDER)
    try:
        agent, run = _delegate(reg, tmp_path, timeout=5)
        h.wait_started(reg, run)
        clock.tick(60)  # past the 5 s cap, well inside the provider's 30 s
        reg.reap_once()
        h.wait(run)
        assert run.state == FAILED
        assert run.finish_reason == "timeout"
        records = h.run_records(trace_records)
        assert records[-1]["extra"]["overrun"] == {"kind": "timeout", "limit": 5}
        assert agent.agent_id
    finally:
        reg.shutdown()


# --- the MCP tool ------------------------------------------------------------------


@pytest.fixture
def server(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    module = importlib.import_module("subagent.mcp_server")
    registry = Registry(settings, start_reaper=False)
    spawned: list[FakeProcess] = []

    def spawn(argv, env, cwd):
        spawned.append(FakeProcess(_result("done"), argv, env))
        return spawned[-1]

    monkeypatch.setattr(claude_provider, "_spawn_claude", spawn)
    monkeypatch.setattr(module, "settings", settings)
    monkeypatch.setattr(module, "registry", registry)
    module.spawned = spawned  # type: ignore[attr-defined]
    yield module
    registry.shutdown()


def _call(server, tmp_path, **kwargs):
    async def go():
        return await server.delegate(
            task="do it", verification="true", workspace=str(tmp_path), **kwargs)

    return anyio.run(go)


def test_b_the_mcp_delegate_tool_takes_timeout_and_max_steps(server, tmp_path):
    out = _call(server, tmp_path, provider="glm", timeout=7, max_steps=3, wait_seconds=5)
    assert out["state"] == COMPLETED
    argv = server.spawned[0].argv
    assert argv[argv.index("--max-turns") + 1] == "3"
    assert out["caps"] == {"timeout": {"requested": 7, "effective": 7},
                           "max_steps": {"requested": 3, "effective": 3}}


def test_b_the_mcp_delegate_tool_clamps_a_request_above_the_provider_limit(server, tmp_path):
    limit = server.settings.providers["glm"].max_steps
    out = _call(server, tmp_path, provider="glm", max_steps=limit + 500, timeout=10**6,
                wait_seconds=5)
    assert out["state"] == COMPLETED
    assert out["caps"]["max_steps"] == {"requested": limit + 500, "effective": limit}
    run_limit = server.settings.providers["glm"].run_timeout
    assert out["caps"]["timeout"] == {"requested": 10**6, "effective": run_limit}


@pytest.mark.parametrize("kwargs", [{"timeout": 0}, {"max_steps": -1}])
def test_b_the_mcp_delegate_tool_refuses_zero_or_negative(server, tmp_path, kwargs):
    with pytest.raises((RegistryError, ToolError)):
        _call(server, tmp_path, provider="glm", **kwargs)
    assert server.spawned == []

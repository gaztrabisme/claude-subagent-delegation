"""R7a fix round: tests pinning review findings 2, 3, 4, 5, 7 and the reaper bugs.

Offline, in-process scripted children, patched `subagent.runs._now`.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import threading
import time

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from subagent import config, health
from subagent.providers import claude as claude_provider
from subagent.runs import Registry, RegistryError

from ..conftest import make_settings
from ..r7a import _helpers as h
from ..test_lane_status import FAKE_CLAUDE
from ..test_runs import FakeProcess, _result

IDLE = {"idle_timeout": 100.0, "max_agents": 1}


def _clock(monkeypatch) -> h.Clock:
    clock = h.Clock()
    monkeypatch.setattr("subagent.runs._now", clock)
    return clock


def _finished(reg, tmp_path, name="t"):
    agent = reg.create_agent(name, tmp_path, "glm")
    run = h.wait(agent.delegate("work", "true"))
    assert run.state == "completed", run.error
    return agent


class StubbornProcess:
    """Starts, then ignores kill until `release` is set (a worker slow to finish)."""

    def __init__(self, argv, env, release: threading.Event):
        self.argv, self.env, self._release = argv, env, release
        self.killed = False
        self.init_consumed = threading.Event()

    def events(self):
        yield h.init()
        # The worker asks for the next event only after it has handled the init
        # event, so this marks a point the product guarantees.
        self.init_consumed.set()
        self._release.wait(timeout=10)

    def kill(self) -> None:
        self.killed = True


# --- H1 (finding 2) ---------------------------------------------------------------


def test_h1_a_deadline_ended_run_still_working_at_the_next_pass_does_not_close_the_agent(
    tmp_path, monkeypatch
):
    clock = _clock(monkeypatch)
    release = threading.Event()
    procs: list[StubbornProcess] = []

    def stubborn(argv, env):
        procs.append(StubbornProcess(argv, env, release))
        return procs[-1]

    makers = [stubborn, h.echo_resume]
    reg = h.registry(tmp_path, monkeypatch, makers,
                     provider_extra={"run_timeout": 30.0, "max_agents": 2})
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = agent.delegate("work", "true")
        h.wait_started(reg, run)
        assert procs[0].init_consumed.wait(timeout=10)  # worker has handled init
        clock.tick(500)
        reg.reap_once()  # ends the run on its deadline
        assert run.state == "working"  # the worker has not finished it yet
        reg.reap_once()  # second pass sees the same run, still working
        assert agent.closed is False
        release.set()
        h.wait(run)
        assert agent.closed is False
        again = h.wait(agent.follow_up("more"))
        assert again.state == "completed", again.error
    finally:
        release.set()
        reg.shutdown()


# --- H2 (finding 3) ---------------------------------------------------------------


def _race_registry(tmp_path, monkeypatch, clock):
    return h.registry(
        tmp_path, monkeypatch, [h.script(h.completed()), h.blocked()],
        provider_extra={"max_agents": 1, "slot_release_grace_seconds": 0.0})


def _submit_in_the_gap(monkeypatch, box):
    real = Registry._releasable

    def releasable(agents, grace):
        candidate = real(agents, grace)
        if candidate is not None and "run" not in box:
            box["run"] = candidate.submit("new work", "true")
        return candidate

    monkeypatch.setattr(Registry, "_releasable", staticmethod(releasable))


def test_h2_a_submit_landing_before_the_release_is_not_cancelled(tmp_path, monkeypatch):
    clock = _clock(monkeypatch)
    reg = _race_registry(tmp_path, monkeypatch, clock)
    try:
        _finished(reg, tmp_path)
        clock.tick(10)
        box: dict = {}
        _submit_in_the_gap(monkeypatch, box)
        with contextlib.suppress(RegistryError):
            reg.create_agent("next", tmp_path, "glm")
        run = box["run"]
        assert run.state == "working", f"new run was {run.state}: {run.error}"
    finally:
        reg.shutdown()


def test_h2_the_create_agent_that_lost_the_race_raises_a_cap_error(tmp_path, monkeypatch):
    clock = _clock(monkeypatch)
    reg = _race_registry(tmp_path, monkeypatch, clock)
    try:
        old = _finished(reg, tmp_path)
        clock.tick(10)
        _submit_in_the_gap(monkeypatch, {})
        with pytest.raises(RegistryError, match="glm"):
            reg.create_agent("next", tmp_path, "glm")
        assert old.closed is False
    finally:
        reg.shutdown()


def test_h2_submit_on_an_agent_claimed_for_release_raises_registry_error(
    tmp_path, monkeypatch
):
    clock = _clock(monkeypatch)
    reg = _race_registry(tmp_path, monkeypatch, clock)
    try:
        old = _finished(reg, tmp_path)
        clock.tick(10)
        outcome: list[str] = []
        real_close = old.close

        def close(*args, **kwargs):
            try:
                old.submit("late", "true")
                outcome.append("submitted")
            except RegistryError:
                outcome.append("refused")
            return real_close(*args, **kwargs)

        old.close = close  # type: ignore[method-assign]
        reg.create_agent("next", tmp_path, "glm")
        assert outcome == ["refused"]
    finally:
        reg.shutdown()


# --- H3 (finding 4) ---------------------------------------------------------------


@pytest.fixture
def server(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, providers=h.provider(
        max_agents=1, slot_release_grace_seconds=0.0))
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    module = importlib.import_module("subagent.mcp_server")
    registry = Registry(settings, start_reaper=False)
    monkeypatch.setattr(
        claude_provider, "_spawn_claude",
        lambda argv, env, cwd: FakeProcess(_result("done"), argv, env))
    monkeypatch.setattr(module, "settings", settings)
    monkeypatch.setattr(module, "registry", registry)
    yield module
    registry.shutdown()


@pytest.mark.parametrize("kwargs", [{"timeout": 0}, {"timeout": -2}, {"max_steps": 0}])
def test_h3_a_bad_cap_is_refused_before_any_idle_agent_is_released(
    server, tmp_path, kwargs
):
    reg = server.registry
    idle = _finished(reg, tmp_path)
    count = len(reg.agents())

    async def go():
        return await server.delegate(
            task="do it", verification="true", workspace=str(tmp_path),
            provider="glm", **kwargs)

    with pytest.raises((RegistryError, ToolError)):
        anyio.run(go)
    assert idle.closed is False
    assert len(reg.agents()) == count


# --- H4 (finding 5) ---------------------------------------------------------------

CAPS = {"max_steps": 10, "run_timeout": 30.0, "max_agents": 3}


@pytest.mark.parametrize("kwargs", [
    {"timeout": float("nan")}, {"max_steps": float("nan")},
])
def test_h4_non_finite_nan_caps_are_refused(tmp_path, monkeypatch, kwargs):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())], provider_extra=CAPS)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        with pytest.raises(RegistryError):
            agent.delegate("do it", "true", **kwargs)
        assert reg.spawned == []
    finally:
        reg.shutdown()


def test_h4_infinite_timeout_is_clamped_to_the_provider_limit(tmp_path, monkeypatch):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())], provider_extra=CAPS)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = h.wait(agent.delegate("do it", "true", timeout=float("inf")))
        assert run.detail()["caps"]["timeout"]["effective"] == 30.0
    finally:
        reg.shutdown()


def test_h4_infinite_max_steps_is_clamped_to_the_provider_limit(tmp_path, monkeypatch):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())], provider_extra=CAPS)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = h.wait(agent.delegate("do it", "true", max_steps=float("inf")))
        assert run.detail()["caps"]["max_steps"]["effective"] == 10
    finally:
        reg.shutdown()


# --- H5 (finding 7) ---------------------------------------------------------------


def test_h5_a_fallback_hop_to_a_lower_step_lane_uses_that_lanes_limit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLM_API_KEY", "test-glm-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-ds-key")
    monkeypatch.setattr(
        health, "check", lambda cfg: health.Health(ok=True, base_url=cfg.base_url))
    fake = str(FAKE_CLAUDE)
    providers = {
        "glm": {"driver": "claude", "base_url": "https://api.z.ai/api/anthropic",
                "model": "glm-test", "api_key_env": "GLM_API_KEY", "vendor": "zai",
                "binary": fake, "max_steps": 10},
        "deepseek": {"driver": "claude", "base_url": "https://api.deepseek.com/anthropic",
                     "model": "ds-test", "api_key_env": "DEEPSEEK_API_KEY",
                     "vendor": "deepseek", "binary": fake, "max_steps": 3},
    }
    settings = make_settings(tmp_path, providers=providers, default_provider="glm")
    refusal = {"type": "result", "subtype": "error", "is_error": True,
               "result": 'API Error: {"code": "1310", "message": "weekly cap reached"}'}
    spawned: list[FakeProcess] = []

    def spawn(argv, env, cwd):
        events = [refusal] if not spawned else _result("done")
        spawned.append(FakeProcess(events, argv, env))
        return spawned[-1]

    monkeypatch.setattr(claude_provider, "_spawn_claude", spawn)
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("hop", tmp_path, provider="glm", fallback="full")
        assert agent.wait_ready(10) is None
        run = h.wait(agent.delegate("work", "true", max_steps=8), timeout=10)
        assert run.lane == "deepseek", (run.lane, run.error)
        assert len(spawned) == 2

        def turns(proc):
            return proc.argv[proc.argv.index("--max-turns") + 1]

        assert turns(spawned[0]) == "8"
        assert turns(spawned[1]) == "3"
    finally:
        reg.shutdown()


# --- H6 (reaper robustness) -------------------------------------------------------


def _two_idle(reg, tmp_path):
    a = _finished(reg, tmp_path, "a")
    b = _finished(reg, tmp_path, "b")
    a.last_activity -= 500
    b.last_activity -= 500
    assert [x.agent_id for x in reg.agents()][:2] == [a.agent_id, b.agent_id]
    return a, b


def test_h6_a_failing_close_does_not_stop_the_pass(tmp_path, monkeypatch, caplog):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())],
                     provider_extra={"idle_timeout": 100.0, "max_agents": 2})
    try:
        a, b = _two_idle(reg, tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("close exploded")

        a.close = boom  # type: ignore[method-assign]
        with caplog.at_level(logging.WARNING):
            acted = reg.reap_once()
        assert (b.agent_id, "idle") in acted
        assert b.closed is True
        assert b.agent_id not in [x.agent_id for x in reg.agents()]  # _evict_closed ran
        assert any(r.levelno >= logging.WARNING and a.agent_id in r.getMessage()
                   for r in caplog.records)
        del a.close  # the next pass works on A
        assert (a.agent_id, "idle") in reg.reap_once()
        assert a.closed is True
    finally:
        a.__dict__.pop("close", None)
        reg.shutdown()


def test_h6_the_close_bound_defaults_to_twenty_seconds(tmp_path, monkeypatch):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())])
    try:
        assert reg.reap_close_timeout == 20
        assert reg.reap_stuck == set()
    finally:
        reg.shutdown()


def test_h6_a_hanging_close_is_bounded_and_the_pass_moves_on(tmp_path, monkeypatch):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())],
                     provider_extra={"idle_timeout": 100.0, "max_agents": 2})
    gate = threading.Event()
    try:
        a, b = _two_idle(reg, tmp_path)
        reg.reap_close_timeout = 0.2
        a.close = lambda *args, **kwargs: gate.wait(timeout=6)  # type: ignore[method-assign]
        started = time.monotonic()
        acted = reg.reap_once()
        elapsed = time.monotonic() - started
        assert elapsed < 3, f"the pass waited {elapsed:.1f}s on a hung close"
        assert (b.agent_id, "idle") in acted
        assert b.closed is True
        assert a.agent_id in reg.reap_stuck
    finally:
        gate.set()
        a.__dict__.pop("close", None)
        reg.shutdown()

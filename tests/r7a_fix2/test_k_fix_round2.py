"""R7a fix round 2: tests pinning review findings K1 to K5.

Offline, in-process scripted children, patched `subagent.runs._now`.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from subagent import providers
from subagent.providers import base
from subagent.runs import KILL_CANCEL, KILL_TIMEOUT, Agent, Registry, RegistryError

from ..conftest import make_settings
from ..r7a import _helpers as h

SVC = {"max_agents": 1, "run_timeout": 30.0, "slot_release_grace_seconds": 0.0}


def _clock(monkeypatch) -> h.Clock:
    clock = h.Clock()
    monkeypatch.setattr("subagent.runs._now", clock)
    return clock


class StuckProcess:
    """Starts, then ignores kill until `release` is set; then finishes normally."""

    def __init__(self, release: threading.Event):
        self._release = release
        self.killed = False

    def events(self):
        yield h.init()
        self._release.wait(timeout=15)
        yield from h.completed()[1:]

    def kill(self) -> None:
        self.killed = True  # ignored on purpose: the child does not die


class ServiceDriver:
    """A driver with a `cancel` hook (a service job) whose stream can outlive the kill."""

    name = "fake-service"
    needs_api_key = False
    prompt_on_stdin = False

    def __init__(self, release: threading.Event):
        self.capabilities = base.ProviderCapabilities(cancel=True)
        self.release = release
        self.cancels: list[str] = []

    def guard(self, settings, cfg):
        return "service"

    def boot(self, settings, agent_id, cfg):
        return None, base.Session(provider=cfg.name)

    def spawn(self, cfg, settings, agent_id, prompt, cwd, session, model):
        return StuckProcess(self.release)

    def refusal(self, cfg, events):
        return None

    def cancel(self, session, *, reason, kind):
        self.cancels.append(kind)


def _service_registry(tmp_path, monkeypatch):
    release = threading.Event()
    driver = ServiceDriver(release)
    monkeypatch.setattr(providers, "for_driver", lambda _: driver)
    monkeypatch.setattr(Agent, "END_RUN_WAIT", 0.2)
    settings = make_settings(tmp_path, providers=h.provider(**SVC))
    reg = Registry(settings, start_reaper=False)
    return reg, release


def _spy_run_observations(reg) -> list[str]:
    """The finish reason of every run observation recorded to the lane status."""
    seen: list[str] = []
    real = reg.lane_status.record_observations

    def record(observations):
        seen.extend(str(o.get("finish_reason")) for o in observations if o.get("kind") == "run")
        return real(observations)

    reg.lane_status.record_observations = record  # type: ignore[method-assign]
    return seen


def _timeout_then_cancel(reg, release, tmp_path):
    """end_run finishes a stuck service-job run on its deadline, then the caller cancels.

    The stuck worker is released only once the close has set the kill kind.
    Returns (agent, run) after everything has settled.
    """
    agent = reg.create_agent("t", tmp_path, "glm")
    assert agent.wait_ready(5) is None
    run = agent.delegate("work", "true")
    end = time.monotonic() + 3
    while run.deadline is None and time.monotonic() < end:
        time.sleep(0.01)
    agent.end_run(run, "run deadline exceeded", KILL_TIMEOUT)
    assert run.done.is_set() and run.finish_reason == KILL_TIMEOUT, "precondition"
    closer = threading.Thread(target=agent.close, kwargs={"kind": KILL_CANCEL}, daemon=True)
    closer.start()
    end = time.monotonic() + 3
    while agent._kill_kind is None and time.monotonic() < end:
        time.sleep(0.01)
    release.set()
    closer.join(10)
    assert not closer.is_alive()
    return agent, run


# --- K1: a stuck reaper thread that returned ------------------------------------------


def _two_idle(reg, tmp_path):
    out = []
    for name in ("a", "b"):
        agent = reg.create_agent(name, tmp_path, "glm")
        run = h.wait(agent.delegate("work", "true"))
        assert run.state == "completed", run.error
        agent.last_activity -= 500
        out.append(agent)
    return out


def _stuck_thread(agent_id: str) -> threading.Thread:
    return next(t for t in threading.enumerate() if t.name == f"sam-reap-{agent_id}")


def test_k1_an_agent_whose_stuck_close_returned_is_reaped_on_a_later_pass(
    tmp_path, monkeypatch
):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())],
                     provider_extra={"idle_timeout": 100.0, "max_agents": 2})
    gate = threading.Event()
    try:
        a, _b = _two_idle(reg, tmp_path)
        reg.reap_close_timeout = 0.2
        a.close = lambda *args, **kwargs: gate.wait(timeout=6)  # type: ignore[method-assign]
        reg.reap_once()
        assert a.agent_id in reg.reap_stuck
        thread = _stuck_thread(a.agent_id)
        gate.set()
        thread.join(5)
        assert not thread.is_alive()
        del a.close  # the next attempt is the real close
        acted = reg.reap_once()
        assert (a.agent_id, "idle") in acted, "agent stayed skipped after its thread returned"
        assert a.closed is True
        assert a.agent_id not in reg.reap_stuck
    finally:
        gate.set()
        a.__dict__.pop("close", None)
        reg.shutdown()


def test_k1_an_error_the_stuck_thread_raised_is_logged_naming_the_agent(
    tmp_path, monkeypatch, caplog
):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())],
                     provider_extra={"idle_timeout": 100.0, "max_agents": 2})
    gate = threading.Event()
    try:
        a, _b = _two_idle(reg, tmp_path)
        reg.reap_close_timeout = 0.2

        def close(*args, **kwargs):
            gate.wait(timeout=6)
            raise RuntimeError("late boom")

        a.close = close  # type: ignore[method-assign]
        with caplog.at_level(logging.WARNING):
            reg.reap_once()
            caplog.clear()  # the "did not close in time" line is not the one under test
            thread = _stuck_thread(a.agent_id)
            gate.set()
            thread.join(5)
            del a.close
            reg.reap_once()
        named = [r for r in caplog.records
                 if r.levelno >= logging.WARNING and a.agent_id in r.getMessage()]
        assert named, "the stuck thread's error was never logged"
        assert "late boom" in caplog.text
    finally:
        gate.set()
        a.__dict__.pop("close", None)
        reg.shutdown()


# --- K2, K3: end_run's fallback finish, then a cancel, then the stuck worker ----------


def test_k2_a_later_cancel_does_not_write_a_second_run_record_or_change_the_outcome(
    tmp_path, monkeypatch, trace_records
):
    clock = _clock(monkeypatch)
    reg, release = _service_registry(tmp_path, monkeypatch)
    try:
        _agent, run = _timeout_then_cancel(reg, release, tmp_path)
        clock.tick(1)
        records = [r for r in h.run_records(trace_records) if r["run_id"] == run.run_id]
        assert len(records) == 1, [(r["state"], r["finish_reason"]) for r in records]
        assert (run.state, run.finish_reason) == ("failed", "timeout")
        assert (records[0]["state"], records[0]["finish_reason"]) == ("failed", "timeout")
    finally:
        release.set()
        reg.shutdown()


def test_k3_the_lane_records_one_observation_for_that_run(tmp_path, monkeypatch):
    _clock(monkeypatch)
    reg, release = _service_registry(tmp_path, monkeypatch)
    seen = _spy_run_observations(reg)
    try:
        _timeout_then_cancel(reg, release, tmp_path)
        assert seen == ["timeout"], seen
    finally:
        release.set()
        reg.shutdown()


# --- K4: a plain driver whose stream never drains after the kill -----------------------


class StubbornProcess:
    def __init__(self, release: threading.Event):
        self._release = release
        self.killed = False

    def events(self):
        yield h.init()
        self._release.wait(timeout=15)

    def kill(self) -> None:
        self.killed = True


def _stubborn_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(Agent, "END_RUN_WAIT", 0.2)
    release = threading.Event()
    reg = h.registry(tmp_path, monkeypatch,
                     [lambda argv, env: StubbornProcess(release)], provider_extra=SVC)
    return reg, release


def _ended_stubborn_run(reg, tmp_path, clock):
    agent = reg.create_agent("t", tmp_path, "glm")
    run = agent.delegate("work", "true")
    h.wait_started(reg, run)
    clock.tick(500)
    agent.end_run(run, "run deadline exceeded", KILL_TIMEOUT)
    return agent, run


def test_k4_a_deadline_ended_run_is_finished_by_the_server_when_the_child_ignores_kill(
    tmp_path, monkeypatch
):
    clock = _clock(monkeypatch)
    reg, release = _stubborn_registry(tmp_path, monkeypatch)
    try:
        agent, run = _ended_stubborn_run(reg, tmp_path, clock)
        assert run.done.is_set(), "the run was left WORKING behind a stuck stream"
        assert (run.state, run.finish_reason) == ("failed", "timeout")
        assert agent.busy is False
        assert agent._active_lane is None, "the lane lease is still held"
    finally:
        release.set()
        reg.shutdown()


def test_k4_the_stuck_agent_does_not_hold_its_lane_slot(tmp_path, monkeypatch):
    # Pinned: the server finishes the run itself (above), so the agent counts as
    # finished and a new agent on the one-slot provider releases it. The stuck
    # stream ends 0.5 s into the release, as a real child eventually does.
    clock = _clock(monkeypatch)
    reg, release = _stubborn_registry(tmp_path, monkeypatch)
    try:
        agent, _run = _ended_stubborn_run(reg, tmp_path, clock)
        timer = threading.Timer(0.5, release.set)
        timer.start()
        try:
            new = reg.create_agent("next", tmp_path, "glm")
        except RegistryError as exc:
            pytest.fail(f"slot still held by the stuck agent: {exc}")
        finally:
            timer.cancel()
        assert new.agent_id != agent.agent_id
    finally:
        release.set()
        reg.shutdown()


# --- K5: a refused submit keeps the released-agents list -------------------------------


class _ClaimLandsBeforeRecheck:
    """Stands in for `Agent._wake`: a release claim lands just before the locked re-check."""

    def __init__(self, agent: Agent, real):
        self._agent, self._real = agent, real
        self.armed = False
        self._owner = threading.get_ident()

    def __enter__(self):
        if self.armed and threading.get_ident() == self._owner:
            self.armed = False
            self._agent._closing = True
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_k5_a_refused_submit_does_not_lose_the_released_agents_list(tmp_path, monkeypatch):
    clock = _clock(monkeypatch)
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed()), h.blocked()],
                     provider_extra={"max_agents": 1, "slot_release_grace_seconds": 0.0})
    try:
        old = reg.create_agent("old", tmp_path, "glm")
        h.wait(old.delegate("work", "true"))
        clock.tick(10)
        agent = reg.create_agent("next", tmp_path, "glm")  # releases `old`
        assert old.closed is True
        proxy = _ClaimLandsBeforeRecheck(agent, agent._wake)
        agent._wake = proxy  # type: ignore[assignment]
        proxy.armed = True
        with pytest.raises(RegistryError):
            agent.submit("late", "true")
        agent._closing = False  # the claim did not stick; the caller tries again
        run = agent.submit("again", "true")
        assert run.released_agents == [old.agent_id]
    finally:
        reg.shutdown()

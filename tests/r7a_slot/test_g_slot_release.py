"""R7a slot release (g, S65): a full lane frees a finished idle agent for a new request."""

from __future__ import annotations

import re

import pytest

from subagent.config import ConfigError
from subagent.runs import Registry, RegistryError

from ..conftest import default_providers, make_settings
from ..r7a import _helpers as h


def _registry(tmp_path, monkeypatch, makers, *, glm=None, alt=False, **core):
    """A Registry on lane `glm` (and a twin lane `alt` when asked)."""
    base = default_providers()["glm"]
    table = {"glm": {**base, "max_agents": 1, **(glm or {})}}
    if alt:
        table["alt"] = {**base, "max_agents": 1}
    settings = make_settings(tmp_path, providers=table, **core)
    spawned: list = []

    def spawn(argv, env, cwd):
        maker = makers[len(spawned)] if len(spawned) < len(makers) else makers[-1]
        spawned.append(maker(argv, env))
        return spawned[-1]

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    reg = Registry(settings, start_reaper=False)
    reg.spawned = spawned  # type: ignore[attr-defined]
    return reg


def _clock(monkeypatch):
    clock = h.Clock()
    monkeypatch.setattr("subagent.runs._now", clock)
    return clock


def _finished(reg, tmp_path, provider="glm", name="t"):
    agent = reg.create_agent(name, tmp_path, provider)
    run = h.wait(agent.delegate("work", "true"))
    assert run.state == "completed", run.error
    return agent, run


def _grace(reg):
    return reg.settings.providers["glm"].slot_release_grace_seconds


def test_g_a_finished_agent_idle_past_grace_is_released_for_a_new_one(
    tmp_path, monkeypatch, trace_records
):
    clock = _clock(monkeypatch)
    reg = _registry(tmp_path, monkeypatch, [h.script(h.completed())],
                    glm={"slot_release_grace_seconds": 120.0})
    try:
        old, _ = _finished(reg, tmp_path)
        clock.tick(121)
        new = reg.create_agent("next", tmp_path, "glm")
        assert new.agent_id != old.agent_id
        assert old.closed is True
        run = h.wait(new.delegate("work", "true"))
        assert run.state == "completed", run.error
        rec = [r for r in h.run_records(trace_records) if r["run_id"] == run.run_id][-1]
        assert rec["extra"]["released_agents"] == [old.agent_id]
    finally:
        reg.shutdown()


def test_g_an_agent_idle_inside_grace_is_kept_and_the_request_refused(tmp_path, monkeypatch):
    clock = _clock(monkeypatch)
    reg = _registry(tmp_path, monkeypatch, [h.script(h.completed())],
                    glm={"slot_release_grace_seconds": 120.0})
    try:
        old, _ = _finished(reg, tmp_path)
        clock.tick(119)
        with pytest.raises(RegistryError) as err:
            reg.create_agent("next", tmp_path, "glm")
        text = str(err.value)
        assert "glm" in text
        assert re.search(r"\b1\b", text)
        assert "cancel" in text.lower()
        assert old.closed is False
    finally:
        reg.shutdown()


def test_g_the_default_grace_is_120_seconds_when_the_key_is_omitted(tmp_path, monkeypatch):
    clock = _clock(monkeypatch)
    reg = _registry(tmp_path, monkeypatch, [h.script(h.completed())])
    try:
        assert _grace(reg) == 120.0
        old, _ = _finished(reg, tmp_path)
        clock.tick(119)
        with pytest.raises(RegistryError):
            reg.create_agent("early", tmp_path, "glm")
        clock.tick(2)  # 121 s idle
        new = reg.create_agent("late", tmp_path, "glm")
        assert new.agent_id != old.agent_id and old.closed is True
    finally:
        reg.shutdown()


def test_g_a_busy_agent_is_never_released_however_old_its_clock(tmp_path, monkeypatch):
    clock = _clock(monkeypatch)
    reg = _registry(tmp_path, monkeypatch, [h.blocked()],
                    glm={"slot_release_grace_seconds": 120.0})
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = agent.delegate("work", "true")
        h.wait_started(reg, run)
        assert agent.busy
        clock.tick(10_000)
        with pytest.raises(RegistryError, match="glm"):
            reg.create_agent("next", tmp_path, "glm")
        assert agent.closed is False
        assert run.state == "working"
    finally:
        reg.shutdown()


def test_g_an_agent_with_a_working_run_after_a_finished_one_is_not_released(
    tmp_path, monkeypatch
):
    clock = _clock(monkeypatch)
    reg = _registry(tmp_path, monkeypatch, [h.script(h.completed()), h.blocked()],
                    glm={"slot_release_grace_seconds": 0.0})
    try:
        agent, _ = _finished(reg, tmp_path)
        second = agent.delegate("again", "true")
        h.wait_started(reg, second)
        clock.tick(10_000)
        with pytest.raises(RegistryError, match="glm"):
            reg.create_agent("next", tmp_path, "glm")
        assert agent.closed is False
    finally:
        reg.shutdown()


def test_g_only_as_many_agents_as_needed_are_released_oldest_first(
    tmp_path, monkeypatch, trace_records
):
    clock = _clock(monkeypatch)
    reg = _registry(tmp_path, monkeypatch, [h.script(h.completed())],
                    glm={"max_agents": 2, "slot_release_grace_seconds": 120.0}, max_agents=5)
    try:
        first, _ = _finished(reg, tmp_path, name="first")
        clock.tick(10)
        second, _ = _finished(reg, tmp_path, name="second")
        clock.tick(500)  # both far past grace
        new = reg.create_agent("third", tmp_path, "glm")
        assert first.closed is True
        assert second.closed is False
        run = h.wait(new.delegate("work", "true"))
        rec = [r for r in h.run_records(trace_records) if r["run_id"] == run.run_id][-1]
        assert rec["extra"]["released_agents"] == [first.agent_id]
    finally:
        reg.shutdown()


def test_g_agents_on_another_lane_are_never_released_for_this_lane(tmp_path, monkeypatch):
    clock = _clock(monkeypatch)
    reg = _registry(tmp_path, monkeypatch, [h.script(h.completed())], alt=True,
                    glm={"slot_release_grace_seconds": 0.0}, max_agents=5)
    try:
        glm_agent, _ = _finished(reg, tmp_path, "glm", "g")
        alt_agent, _ = _finished(reg, tmp_path, "alt", "a")
        clock.tick(10_000)
        # glm is full: its own finished agent is released, the alt agent is left alone.
        new = reg.create_agent("next", tmp_path, "glm")
        assert glm_agent.closed is True
        assert alt_agent.closed is False
        assert new.cfg.name == "glm"
        assert alt_agent.agent_id in [x.agent_id for x in reg.agents()]
    finally:
        reg.shutdown()


def test_g_grace_zero_releases_a_finished_agent_at_once(tmp_path, monkeypatch, trace_records):
    _clock(monkeypatch)
    reg = _registry(tmp_path, monkeypatch, [h.script(h.completed())],
                    glm={"slot_release_grace_seconds": 0})
    try:
        old, _ = _finished(reg, tmp_path)
        new = reg.create_agent("next", tmp_path, "glm")  # no clock tick at all
        assert old.closed is True
        run = h.wait(new.delegate("work", "true"))
        rec = [r for r in h.run_records(trace_records) if r["run_id"] == run.run_id][-1]
        assert rec["extra"]["released_agents"] == [old.agent_id]
    finally:
        reg.shutdown()


@pytest.mark.parametrize("value", [-1, -0.5])
def test_g_a_negative_grace_is_a_config_refusal(tmp_path, value):
    key = "slot_release_grace_seconds"
    with pytest.raises(ConfigError, match=key):
        make_settings(tmp_path, providers=h.provider(**{key: value}))


def test_g_a_released_agent_cannot_continue_but_its_result_stays_readable(
    tmp_path, monkeypatch
):
    clock = _clock(monkeypatch)
    reg = _registry(tmp_path, monkeypatch, [h.script(h.completed())],
                    glm={"slot_release_grace_seconds": 120.0})
    try:
        old, old_run = _finished(reg, tmp_path)
        clock.tick(121)
        reg.create_agent("next", tmp_path, "glm")
        assert old.closed is True
        with pytest.raises(RegistryError, match="closed"):
            old.delegate("more", "true")
        assert old.agent_id not in [a.agent_id for a in reg.agents()]
        archived = reg.find_run(old_run.run_id)
        assert archived.state == "completed"
        assert archived.detail()["run_id"] == old_run.run_id
    finally:
        reg.shutdown()

"""R7a lifecycle (e): the reaper closes a finished, idle agent on its own."""

from __future__ import annotations

from subagent.runs import RegistryError  # noqa: F401  (documented failure type)

from ..r7a import _helpers as h

IDLE = {"idle_timeout": 100.0, "max_agents": 1}


def _live_by_lane(reg):
    out: dict[str, int] = {}
    for agent in reg.agents():
        if not agent.closed:
            out[agent.cfg.name] = out.get(agent.cfg.name, 0) + 1
    return out


def _slots(reg):
    return reg.lane_status.snapshot(reg.settings, live_by_lane=_live_by_lane(reg))["glm"]["slots"]


def _finished_agent(reg, tmp_path):
    agent = reg.create_agent("t", tmp_path, "glm")
    run = h.wait(agent.delegate("work", "true"))
    assert run.state == "completed", run.error
    return agent


def test_e_an_agent_idle_past_its_timeout_is_reaped_without_a_cancel(tmp_path, monkeypatch):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())], provider_extra=IDLE)
    try:
        agent = _finished_agent(reg, tmp_path)
        assert _slots(reg)["live"] == 1
        agent.last_activity -= 500
        acted = reg.reap_once()
        assert (agent.agent_id, "idle") in acted
        assert agent.closed is True
        assert agent.agent_id not in [a.agent_id for a in reg.agents()]
    finally:
        reg.shutdown()


def test_e_a_reaped_agent_frees_its_provider_slot(tmp_path, monkeypatch):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())], provider_extra=IDLE,
                     max_agents=1)
    try:
        agent = _finished_agent(reg, tmp_path)
        assert _slots(reg)["live"] == 1
        agent.last_activity -= 500
        reg.reap_once()
        assert _slots(reg)["live"] == 0
        again = reg.create_agent("next", tmp_path, "glm")  # neither cap refuses it now
        assert again.agent_id != agent.agent_id
    finally:
        reg.shutdown()


def test_e_an_agent_younger_than_the_timeout_is_not_reaped(tmp_path, monkeypatch):
    reg = h.registry(tmp_path, monkeypatch, [h.script(h.completed())], provider_extra=IDLE)
    try:
        agent = _finished_agent(reg, tmp_path)
        agent.last_activity -= 50
        assert reg.reap_once() == []
        assert agent.closed is False
        assert _slots(reg)["live"] == 1
    finally:
        reg.shutdown()


def test_e_a_busy_agent_is_not_reaped_however_old_its_last_activity(tmp_path, monkeypatch):
    reg = h.registry(tmp_path, monkeypatch, [h.blocked()], provider_extra=IDLE)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = agent.delegate("work", "true")
        h.wait_started(reg, run)
        assert agent.busy
        agent.last_activity -= 500
        acted = reg.reap_once()
        assert (agent.agent_id, "idle") not in acted
        assert agent.closed is False
        assert run.state == "working"
    finally:
        reg.shutdown()

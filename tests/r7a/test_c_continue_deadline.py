"""R7a (c): `continue` resumes a run that ended on its deadline.

After finish_reason "timeout" (the reaper, or the run's own cap) the agent is
not closed: the next run resumes the same provider session. A cancel still
closes the agent.
"""

from __future__ import annotations

import pytest

from subagent.runs import COMPLETED, FAILED, RegistryError

from . import _helpers as h

PROVIDER = {"run_timeout": 30.0}


def _timed_out(tmp_path, monkeypatch, *, own_cap: bool):
    """A registry, its agent and a run the reaper ended on its deadline."""
    clock = h.Clock()
    monkeypatch.setattr("subagent.runs._now", clock)
    reg = h.registry(tmp_path, monkeypatch, [h.blocked([h.init("sess-1")]), h.echo_resume],
                     provider_extra=PROVIDER)
    agent = reg.create_agent("t", tmp_path, "glm")
    run = agent.delegate("work", "true", **({"timeout": 5} if own_cap else {}))
    h.wait_started(reg, run)
    clock.tick(100)
    acted = reg.reap_once()
    assert (agent.agent_id, "timeout") in acted
    h.wait(run)
    return reg, agent, run


@pytest.mark.parametrize("own_cap", [False, True], ids=["reaper", "own-cap"])
def test_c_the_agent_survives_a_deadline_and_continue_resumes_its_session(
    tmp_path, monkeypatch, own_cap
):
    reg, agent, first = _timed_out(tmp_path, monkeypatch, own_cap=own_cap)
    try:
        assert first.state == FAILED and first.finish_reason == "timeout"
        assert agent.closed is False
        assert agent.info()["state"] != "closed"
        second = h.wait(agent.follow_up("go on", verification="true"))
        assert second.state == COMPLETED, second.error
        argv = reg.spawned[1].argv
        assert argv[argv.index("--resume") + 1] == "sess-1"
        assert first.session_id == "sess-1"
        assert second.session_id == first.session_id == agent.session_id
    finally:
        reg.shutdown()


def test_c_the_timed_out_agent_is_still_listed_as_live(tmp_path, monkeypatch):
    reg, agent, _ = _timed_out(tmp_path, monkeypatch, own_cap=False)
    try:
        assert agent in reg.agents()
        assert reg.agent(agent.agent_id) is agent
    finally:
        reg.shutdown()


def test_c_a_cancelled_run_still_closes_its_agent(tmp_path, monkeypatch):
    reg = h.registry(tmp_path, monkeypatch, [h.blocked([h.init("sess-1")]), h.echo_resume])
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = agent.delegate("work", "true")
        h.wait_started(reg, run)
        agent.close("cancelled by caller")
        h.wait(run)
        assert run.state == "cancelled"
        assert agent.closed is True
        with pytest.raises(RegistryError):
            agent.follow_up("go on", verification="true")
    finally:
        reg.shutdown()

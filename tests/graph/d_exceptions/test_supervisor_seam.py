"""Routine guard escalations are settled by the supervisor, never queued (SPEC 6)."""

from __future__ import annotations

import asyncio
import shlex
import sys

import pytest

from tests.conftest import make_settings

ESCALATING_CALL = ("Bash", {"command": "curl -o out.json https://api.example.com/data"})


def _reviewer(tmp_path, verdict: str):
    script = tmp_path / f"reviewer_{verdict}.py"
    record = tmp_path / f"reviewer_{verdict}.calls"
    script.write_text(
        "import sys\n"
        f"open({str(record)!r}, 'a').write('call\\n')\n"
        "sys.stdin.read()\n"
        f"print({verdict!r})\n"
        "print('stub reviewer')\n",
        encoding="utf-8",
    )
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}", record


def _decide(supervisor, workspace):
    return asyncio.run(supervisor.decide(*ESCALATING_CALL, workspace))


def test_no_client_session_has_no_elicitation_or_capability_methods():
    from subagent.graph.exceptions import NoClientSession

    session = NoClientSession()
    for name in ("check_client_capability", "elicit", "elicit_form", "create_message"):
        assert not hasattr(session, name)


def test_agent_supervisor_on_a_no_client_session_runs_the_agent_tier(tmp_path):
    from subagent.graph.exceptions import NoClientSession
    from subagent.guard.classify import ALLOW
    from subagent.guard.supervisor import Supervisor

    cmd, record = _reviewer(tmp_path, "ALLOW")
    settings = make_settings(tmp_path, supervisor="agent", supervisor_cmd=cmd)
    supervisor = Supervisor(settings)
    supervisor.bind(NoClientSession())
    assert supervisor.tier == "agent"

    verdict = _decide(supervisor, tmp_path)
    assert verdict.action == ALLOW
    assert supervisor.decisions[-1]["tier"] == "agent"
    assert record.read_text().count("call") == 1


def test_agent_supervisor_denial_is_a_verdict_not_a_queue_record(tmp_path):
    from subagent.graph.exceptions import NoClientSession
    from subagent.guard.classify import DENY
    from subagent.guard.supervisor import Supervisor

    cmd, _ = _reviewer(tmp_path, "DENY")
    settings = make_settings(tmp_path, supervisor="agent", supervisor_cmd=cmd)
    supervisor = Supervisor(settings)
    supervisor.bind(NoClientSession())
    verdict = _decide(supervisor, tmp_path)
    assert verdict.action == DENY
    assert supervisor.decisions[-1]["tier"] == "agent"


@pytest.mark.parametrize("mode", ["auto", "elicitation", "sampling"])
def test_without_an_agent_reviewer_escalation_denies_at_the_deterministic_tier(tmp_path, mode):
    from subagent.graph.exceptions import NoClientSession
    from subagent.guard.classify import DENY
    from subagent.guard.supervisor import Supervisor

    settings = make_settings(tmp_path, supervisor=mode)
    supervisor = Supervisor(settings)
    supervisor.bind(NoClientSession())  # no capability probe, no elicitation attempted
    assert supervisor.tier == "deterministic"

    verdict = _decide(supervisor, tmp_path)
    assert verdict.action == DENY
    assert "fails closed" in verdict.reason
    assert supervisor.decisions[-1]["tier"] == "deterministic"

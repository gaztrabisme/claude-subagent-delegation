"""R1b probes ported as offline regression tests for the public lane contract."""

from __future__ import annotations

import dataclasses
import json
import threading
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from subagent.providers.epi import EPI_PROVIDER, Translator
from subagent.runs import RegistryError

from .test_harness_lane import lane  # noqa: F401 - fixture
from .test_providers_epi import calls, epi  # noqa: F401 - fixture


def wait(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "probe did not reach its gate"
        time.sleep(0.01)


def install_binary(fixture, binary, **extra):
    settings, _, registry, _ = fixture
    cfg = dataclasses.replace(settings.provider(), binary=str(binary),
                              extra={**settings.provider().extra, **extra})
    registry.settings = dataclasses.replace(settings, providers={"epi": cfg})


def test_cancel_before_receipt_never_submits_work(epi, tmp_path, monkeypatch):  # noqa: F811
    reached = tmp_path / "pre-send"
    admitted = tmp_path / "new-admission"
    cancelled = tmp_path / "cancelled-key"
    binary = tmp_path / "stall-before-send"
    binary.write_text('#!/usr/bin/env python3\nimport json, sys, time\nfrom pathlib import Path\n'
                      f'reached=Path({str(reached)!r}); admitted=Path({str(admitted)!r})\n'
                      f'cancelled=Path({str(cancelled)!r})\n'
                      'tool=sys.argv[2]\n'
                      'if tool == "delegate" and "--follow" in sys.argv:\n'
                      '    reached.touch(); time.sleep(30)\n'
                      'elif tool == "delegate":\n'
                      '    admitted.touch()\n'
                      '    print(json.dumps({"state":"working","phase":"running",'
                      '"agent_id":"native-a","run_id":"native-r"}))\n'
                      'elif tool == "cancel" and "--request-id" in sys.argv:\n'
                      '    key=sys.argv[sys.argv.index("--request-id")+1]\n'
                      '    cancelled.write_text(key)\n'
                      '    print(json.dumps({"request_id":key,"closed":True,"admitted":False,'
                      '"agent_id":None,"usage":{}}))\n'
                      'elif tool == "cancel":\n'
                      '    print(json.dumps({"agent_id":"native-a","closed":True,"runs":[]}))\n')
    binary.chmod(0o755)
    install_binary(epi, binary)
    agent = epi[2].create_agent("never-sent", tmp_path, provider="epi", fallback="none")
    assert agent.wait_ready(5) is None
    run = agent.delegate("Question: explain", "true")
    wait(reached.exists)
    monkeypatch.setattr(agent.session().data["receipt"], "wait", lambda timeout=None: False)
    agent.close()
    assert run.done.is_set() and not admitted.exists(), "cancellation submitted new work"
    assert cancelled.read_text() == run.request_id


@pytest.mark.parametrize("loss", ["exit", "malformed"])
def test_transport_loss_after_admission_reconciles_with_epi(epi, tmp_path, monkeypatch, loss):  # noqa: F811
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "awaiting_approval")
    if loss == "exit":
        monkeypatch.setenv("FAKE_EPI_EXIT_AFTER_ADMISSION", "1")
    else:
        binary = tmp_path / "malformed-follow"
        from .test_providers_epi import FAKE, load

        snapshots = load("delegate_awaiting_approval.jsonl")
        binary.write_text('#!/usr/bin/env python3\nimport os, sys\n'
                          'if sys.argv[2] == "delegate":\n'
                          f'    print({json.dumps(snapshots[0])!r}, flush=True)\n'
                          '    print("not-json", flush=True)\n'
                          'else:\n'
                          f'    os.execv(sys.executable,[sys.executable,{str(FAKE)!r},*sys.argv[1:]])\n')
        binary.chmod(0o755)
        install_binary(epi, binary)
    agent = epi[2].create_agent("lost", tmp_path, provider="epi", fallback="none")
    assert agent.wait_ready(5) is None
    at_release = []
    run = agent.delegate("change", "true", pre_verify=lambda: at_release.append(calls(epi)) or {})
    assert run.done.wait(5)
    assert at_release
    assert any(c["argv"][1] in {"await", "cancel"} for c in at_release[0]), (
        "guard released after transport loss without reconciling native work"
    )
    assert run.state == "completed"  # terminal native await, never synthetic client failure


@pytest.mark.parametrize("resolve", [True, False])
def test_unconfirmed_cancel_keeps_guard_until_resolved(epi, tmp_path, monkeypatch, resolve):  # noqa: F811
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "awaiting_approval")
    monkeypatch.setenv("FAKE_EPI_HUMAN_WAIT", "1")
    cfg = epi[0].provider()
    epi[2].settings = dataclasses.replace(epi[0], providers={"epi": dataclasses.replace(
        cfg, extra={**cfg.extra, "cancel_retry_seconds": 0.5},
    )})
    released = threading.Event()
    agent = epi[2].create_agent("guard", tmp_path, provider="epi", fallback="none")
    assert agent.wait_ready(5) is None
    run = agent.delegate("task", "true", pre_verify=lambda: released.set() or {})
    wait(lambda: run.needs_human)
    original = EPI_PROVIDER._call
    attempts = []
    recover = threading.Event()

    def flaky_cancel(session, tool, *args):
        if tool == "cancel":
            attempts.append(args)
            if not recover.is_set():
                raise RuntimeError("epi cancel client failed")
        return original(session, tool, *args)

    monkeypatch.setattr(EPI_PROVIDER, "_call", flaky_cancel)
    closer = threading.Thread(target=agent.close)
    closer.start()
    try:
        wait(lambda: attempts)
        assert not released.is_set() and not run.done.is_set()
        if resolve:
            recover.set()
        closer.join(3)
        assert not closer.is_alive()
        assert len(attempts) >= 2
        if resolve:
            assert run.done.is_set() and released.is_set()
            assert run.detail()["extra"]["cancellation_confirmed"] is True
        else:
            assert not released.is_set() and not run.done.is_set()
            assert run.state == "working" and not agent.closed
            assert run.detail()["extra"]["cancellation_confirmed"] is False
            recover.set()
            agent.close()
            assert released.is_set() and run.done.is_set()
    finally:
        recover.set()
        closer.join(3)
        agent.close()


def test_mcp_request_id_forwarded_and_replay_returns_original_run(lane, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "question_completed")
    arguments = {"task": "Question: explain", "verification": "true", "provider": "epi",
                 "wait_seconds": 5, "request_id": "r1b-caller-retry"}
    first = lane.call("delegate", **arguments)
    second = lane.call("delegate", **{**arguments, "wait_seconds": 0})
    records = lane.calls("delegate")
    assert len(records) == 1
    assert records[0]["argv"][records[0]["argv"].index("--request-id") + 1] == arguments["request_id"]
    assert first["run_id"] == second["run_id"] and first["agent_id"] == second["agent_id"]
    with pytest.raises(ToolError, match="request_id"):
        lane.call("delegate", **{**arguments, "task": "Different task"})
    follow = {"agent_id": first["agent_id"], "message": "Explain again", "verification": "true",
              "wait_seconds": 5, "request_id": "r1b-continue-retry"}
    one = lane.call("continue", **follow)
    two = lane.call("continue", **{**follow, "wait_seconds": 0})
    assert one["run_id"] == two["run_id"]
    args = lane.calls("continue")[0]["argv"]
    assert args[args.index("--request-id") + 1] == follow["request_id"]
    assert len(lane.calls("continue")) == 1
    with pytest.raises(ToolError, match="request_id"):
        lane.call("continue", **{**follow, "message": "Different message"})


def test_cancel_queued_continue_has_zero_usage(epi, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "question_completed")
    agent = epi[2].create_agent("queued", tmp_path, provider="epi", fallback="none")
    assert agent.wait_ready(5) is None
    first = agent.delegate("Question: explain", "true")
    assert first.done.wait(5) and first.usage.total > 0
    original = agent._execute
    entered = threading.Event()
    release = threading.Event()

    def gated(run):
        entered.set()
        assert release.wait(5)
        original(run)

    monkeypatch.setattr(agent, "_execute", gated)
    second = agent.follow_up("Question: explain again")
    assert entered.wait(5) and second.started_at is None
    closer = threading.Thread(target=agent.close)
    closer.start()
    try:
        wait(lambda: second.done.is_set())
    finally:
        release.set()
        closer.join(5)
    assert not closer.is_alive()
    assert second.usage.total == 0 and agent.usage().total == first.usage.total


@pytest.mark.parametrize("tool", ["delegate", "continue"])
@pytest.mark.parametrize("wait_seconds", [0, 5])
def test_lane_argument_error_is_tool_error(lane, monkeypatch, tool, wait_seconds):  # noqa: F811
    if tool == "continue":
        monkeypatch.setenv("FAKE_EPI_SCENARIO", "question_completed")
        first = lane.delegate(task="Question: explain", verification="true", wait_seconds=5)
        monkeypatch.setenv("FAKE_EPI_SCENARIO", "argument_error")
        arguments = {"agent_id": first["agent_id"], "message": "Change: edit files",
                     "verification": "true", "wait_seconds": wait_seconds}
    else:
        monkeypatch.setenv("FAKE_EPI_SCENARIO", "argument_error")
        arguments = {"task": "Change: edit files", "verification": "true", "provider": "epi",
                     "wait_seconds": wait_seconds}
    with pytest.raises(ToolError, match='verification.*has no objective check'):
        lane.call(tool, **arguments)
    assert "test-token-canary" not in json.dumps(lane.calls())


def test_queued_admission_timeout_preserves_active_guard(epi, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "awaiting_approval")
    monkeypatch.setenv("FAKE_EPI_HUMAN_WAIT", "1")
    released = threading.Event()
    agent = epi[2].create_agent("queued barrier", tmp_path, provider="epi", fallback="none")
    assert agent.wait_ready(5) is None
    first = agent.delegate("change", "true", pre_verify=lambda: released.set() or {})
    wait(lambda: first.needs_human)
    second = agent.follow_up("another change", "true")
    try:
        with pytest.raises(RegistryError, match="admission"):
            agent.wait_admission(second, timeout=0.1)
        assert not agent.closed and not released.is_set() and not first.done.is_set()
        assert second.done.is_set() and second.usage.total == 0
        assert len([c for c in calls(epi) if c["argv"][1] == "continue"]) == 0
        assert not any(c["argv"][1] == "cancel" for c in calls(epi))
    finally:
        agent.close()


def test_poll_cannot_turn_admitted_job_into_prework_refusal(epi, monkeypatch):  # noqa: F811
    session = epi[1]
    session.session_id = "already-admitted"
    session.data["run_id"] = "saved-native-run"
    monkeypatch.setattr(EPI_PROVIDER, "_call", lambda *a: {"state": "rejected", "error": "no lane"})
    with pytest.raises(ValueError, match="after admission"):
        EPI_PROVIDER.poll(session)


def test_ambiguous_first_receipt_loss_cancels_request_before_guard_release(
    epi, tmp_path, monkeypatch,  # noqa: F811
):
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "ambiguous_error")
    agent = epi[2].create_agent("unknown admission", tmp_path, provider="epi", fallback="none")
    assert agent.wait_ready(5) is None
    at_release = []
    run = agent.delegate("change", "true", pre_verify=lambda: at_release.append(calls(epi)) or {})
    assert run.done.wait(5)
    assert at_release and any(c["argv"][1] == "cancel" for c in at_release[0])
    cancel = [c["argv"] for c in calls(epi) if c["argv"][1] == "cancel"][0]
    assert cancel[cancel.index("--request-id") + 1] == run.request_id
    assert run.detail()["extra"]["cancellation_confirmed"]


def test_invalid_initial_terminal_reconciles_and_continues_same_agent(
    epi, tmp_path, monkeypatch,  # noqa: F811
):
    from .test_providers_epi import FAKE, load

    valid = load("delegate_question_completed.jsonl")[-1]
    invalid = {**valid, "verification": {"passed": False}}
    binary = tmp_path / "invalid-first-terminal"
    binary.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
                      'if sys.argv[2] == "delegate":\n'
                      '    with open(os.environ["FAKE_EPI_RECORD"], "a") as record:\n'
                      '        record.write(json.dumps({"argv":sys.argv[1:]})+"\\n")\n'
                      f'    print({json.dumps(invalid)!r}, flush=True)\n'
                      'else:\n'
                      f'    os.execv(sys.executable,[sys.executable,{str(FAKE)!r},*sys.argv[1:]])\n')
    binary.chmod(0o755)
    install_binary(epi, binary)
    original = EPI_PROVIDER._call
    monkeypatch.setattr(EPI_PROVIDER, "_call", lambda session, tool, *args:
                        valid if tool == "await" else original(session, tool, *args))
    agent = epi[2].create_agent("recover identity", tmp_path, provider="epi", fallback="none")
    assert agent.wait_ready(5) is None
    first = agent.delegate("Question: explain", "true")
    assert first.done.wait(5) and first.state == "completed"
    second = agent.follow_up("Explain again", "true")
    assert second.done.wait(5) and second.state == "completed"
    submissions = [c["argv"][1] for c in calls(epi) if c["argv"][1] in {"delegate", "continue"}]
    assert submissions == ["delegate", "continue"]
    assert agent.session_id == valid["agent_id"]


def test_request_cancel_recovered_identity_stays_bound_to_outer_agent(
    epi, tmp_path, monkeypatch,  # noqa: F811
):
    from .test_providers_epi import load

    monkeypatch.setenv("FAKE_EPI_SCENARIO", "ambiguous_error")
    native = load("delegate_question_completed.jsonl")[-1]
    original = EPI_PROVIDER._call

    def recovered(session, tool, *args):
        if tool == "cancel" and "--request-id" in args:
            return {"request_id": session.data["request_id"], "closed": True, "admitted": True,
                    "agent_id": native["agent_id"], "usage": native["usage"]}
        return original(session, tool, *args)

    monkeypatch.setattr(EPI_PROVIDER, "_call", recovered)
    agent = epi[2].create_agent("cancel receipt", tmp_path, provider="epi", fallback="none")
    assert agent.wait_ready(5) is None
    first = agent.delegate("Question: explain", "true")
    assert first.done.wait(5)
    second = agent.follow_up("Explain again", "true")
    assert second.done.wait(5)
    submissions = [c["argv"][1] for c in calls(epi) if c["argv"][1] in {"delegate", "continue"}]
    assert submissions == ["delegate", "continue"]


def test_continue_after_no_admission_refuses_instead_of_redelegating(lane, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "rejected")
    first = lane.delegate(task="Question: original", fallback="none", wait_seconds=5)
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "question_completed")
    with pytest.raises(ToolError, match="never admitted"):
        lane.call("continue", agent_id=first["agent_id"], message="Question: new", wait_seconds=0)
    assert len(lane.calls("delegate")) == 1 and not lane.calls("continue")


def test_schema_diagnostic_cannot_echo_file_only_token(epi, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    binary = tmp_path / "echo-secret-schema"
    binary.write_text('#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\n'
                      'token=Path(sys.argv[sys.argv.index("--token-file")+1]).read_text().strip()\n'
                      'sys.stderr.write("schema-invalid: token diagnostic "+token+"\\n")\n'
                      'sys.exit(1)\n')
    binary.chmod(0o755)
    install_binary(epi, binary)
    agent = epi[2].create_agent("bad diagnostic", tmp_path, provider="epi", fallback="none")
    assert agent.wait_ready(5) is None
    run = agent.delegate("Question: explain", "true")
    assert run.done.wait(5)
    assert run.admission_error and "test-token-canary" not in run.admission_error
    assert "test-token-canary" not in json.dumps(run.detail())


@pytest.mark.parametrize("usage", [0, 99])
def test_request_cancel_tombstone_blocks_late_delegate(epi, tmp_path, monkeypatch, usage):  # noqa: F811
    session = epi[1]
    result = EPI_PROVIDER._call(session, "cancel", "--request-id", "late-send")
    assert result["closed"] and result["admitted"] is False
    agent = epi[2].create_agent("late send", tmp_path, provider="epi", fallback="none")
    assert agent.wait_ready(5) is None
    if usage:
        original = Translator.outcome

        def charged_tombstone(self, detail):
            if detail.get("admitted") is False:
                detail["usage"]["total"] = usage
            return original(self, detail)

        monkeypatch.setattr(Translator, "outcome", charged_tombstone)
    run = agent.delegate("Question: explain", "true", request_id="late-send")
    assert run.done.wait(5)
    assert run.state == "cancelled" and run.usage.total == 0
    assert agent.session().session_id is None


@pytest.mark.parametrize("poll", [False, True])
def test_admitted_snapshot_cannot_be_replaced_by_unadmitted_tombstone(epi, poll):  # noqa: F811
    from .test_providers_epi import load

    session = epi[1]
    detail = load("delegate_awaiting_approval.jsonl")[0]
    session.data["request_id"] = "admitted-key"
    translator = Translator(session)
    translator.feed(detail)
    if poll:
        translator = Translator(session, run_id=detail["run_id"])
    with pytest.raises(ValueError, match="admission identities"):
        translator.outcome({"state": "cancelled", "admitted": False,
                            "request_id": "admitted-key"})


def test_queued_continue_cannot_delegate_after_predecessor_refusal(epi, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_EPI_SCENARIO", "rejected")
    agent = epi[2].create_agent("queued refusal", tmp_path, provider="epi", fallback="none")
    assert agent.wait_ready(5) is None
    entered = threading.Event()
    release = threading.Event()

    def gated():
        entered.set()
        assert release.wait(5)

    first = agent.delegate("Original task", "true", pre_run=gated)
    assert entered.wait(5)
    second = agent.follow_up("New followup", "true")
    release.set()
    assert first.done.wait(5) and second.done.wait(5)
    assert second.state == "failed" and "never admitted" in second.admission_error
    assert len([c for c in calls(epi) if c["argv"][1] == "delegate"]) == 1

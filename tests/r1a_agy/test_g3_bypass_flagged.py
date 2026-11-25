"""G3 acceptance tests: a BypassSandbox tool step is flagged in the trace and ends the run."""

from __future__ import annotations

import os
import sys
import time

import pytest

from subagent.runs import COMPLETED, FAILED

from ._common import (
    PROVIDER,
    init_event,
    result_event,
    run_fake,
    text_step,
    tool_step,
    trace_of,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="antigravity runs only under macOS Seatbelt"
)

CMD = "echo hi > /tmp/outside-marker"
LATER_TEXT = "LATER_WORK_MARKER"
LATER_RESULT = "LATER_RESULT_MARKER"


def _later_steps() -> list:
    return [
        text_step(3, LATER_TEXT),
        tool_step(4, "ACTIVE", {"AbsolutePath": "/etc/hosts"}, name="view_file"),
        result_event(LATER_RESULT),
    ]


def _text(run) -> str:
    return f"{run.result_text}\n{run.final_response}"


@pytest.mark.parametrize("value", [True, "true", "TRUE", "True"])
def test_g3_1_true_values_emit_exactly_one_sandbox_bypass_record(tmp_path, monkeypatch, value) -> None:
    """G3.1: BypassSandbox true (bool, or "true" in any case) gives exactly one sandbox_bypass record."""
    events = [
        init_event(),
        tool_step(2, "ACTIVE", {"BypassSandbox": value, "CommandLine": CMD}),
        tool_step(2, "DONE", {"BypassSandbox": value, "CommandLine": CMD}),
        *_later_steps(),
    ]
    agent, run, trace_path, _ = run_fake(tmp_path, monkeypatch, events)
    records = trace_of(trace_path, "sandbox_bypass")
    assert len(records) == 1, records
    rec = records[0]
    assert rec["agent_id"] == agent.agent_id
    assert rec["run_id"] == run.run_id
    assert rec["provider"] == PROVIDER
    assert rec["tool"] == "run_command"
    assert rec["command"] == CMD
    assert isinstance(rec["ts"], (int, float))


@pytest.mark.parametrize("state", ["PENDING", "DONE"])
def test_g3_1_any_step_state_is_flagged(tmp_path, monkeypatch, state) -> None:
    """G3.1: a bypassing tool step is flagged in any state, not only ACTIVE."""
    events = [init_event(), tool_step(2, state, {"BypassSandbox": True, "CommandLine": CMD}),
              *_later_steps()]
    _, run, trace_path, _ = run_fake(tmp_path, monkeypatch, events)
    assert len(trace_of(trace_path, "sandbox_bypass")) == 1
    assert run.finish_reason == "sandbox_bypass"


def test_g3_1_command_is_clipped_to_500_characters(tmp_path, monkeypatch) -> None:
    """G3.1: the record's command is clipped to 500 characters."""
    long_cmd = "echo " + "x" * 2000
    events = [init_event(), tool_step(2, "ACTIVE", {"BypassSandbox": True, "CommandLine": long_cmd}),
              result_event()]
    _, _, trace_path, _ = run_fake(tmp_path, monkeypatch, events)
    (rec,) = trace_of(trace_path, "sandbox_bypass")
    assert 0 < len(rec["command"]) <= 500
    assert rec["command"][:100] == long_cmd[:100]


def test_g3_2_run_is_killed_and_failed_with_finish_reason(tmp_path, monkeypatch) -> None:
    """G3.2: the agy process is killed; the run is failed with finish_reason "sandbox_bypass"."""
    events = [init_event(), tool_step(2, "ACTIVE", {"BypassSandbox": True, "CommandLine": CMD}),
              "SLEEP", result_event(LATER_RESULT)]
    started = time.monotonic()
    _, run, _, _ = run_fake(tmp_path, monkeypatch, events, wait=12)
    assert time.monotonic() - started < 12
    assert run.state == FAILED, (run.state, run.error)
    assert run.finish_reason == "sandbox_bypass"
    pid = int((tmp_path / "ws" / "pid.txt").read_text())
    deadline = time.monotonic() + 5
    alive = True
    while alive and time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            alive = False
    assert not alive, "agy process survived the bypass kill"


def test_g3_2_later_steps_are_not_translated_as_work(tmp_path, monkeypatch) -> None:
    """G3.2: steps after the bypass step (text, tools, result) never become work or the run's result."""
    events = [init_event(), tool_step(2, "ACTIVE", {"BypassSandbox": True, "CommandLine": CMD}),
              *_later_steps()]
    _, run, _, _ = run_fake(tmp_path, monkeypatch, events)
    assert run.state == FAILED
    assert LATER_TEXT not in _text(run)
    assert LATER_RESULT not in _text(run)
    assert run.usage.steps <= 1, run.usage.steps


def test_g3_3_result_text_names_the_bypass(tmp_path, monkeypatch) -> None:
    """G3.3: the run's result text tells the caller the run ended because of a sandbox bypass."""
    events = [init_event(), tool_step(2, "ACTIVE", {"BypassSandbox": "TRUE", "CommandLine": CMD}),
              result_event("fine")]
    _, run, _, _ = run_fake(tmp_path, monkeypatch, events)
    assert "bypass" in _text(run).lower(), (run.result_text, run.final_response, run.error)


@pytest.mark.parametrize("params", [
    {"BypassSandbox": False, "CommandLine": CMD},
    {"BypassSandbox": "false", "CommandLine": CMD},
    {"CommandLine": CMD},
])
def test_g3_negative_false_or_absent_changes_nothing(tmp_path, monkeypatch, params) -> None:
    """G3: BypassSandbox false (bool or string) or absent: no record, run completes as before."""
    events = [init_event(), tool_step(2, "ACTIVE", params), tool_step(2, "DONE", params),
              text_step(3, LATER_TEXT), result_event("finished")]
    _, run, trace_path, spawned = run_fake(tmp_path, monkeypatch, events)
    # Benign steps run as before, and the child is still under the outer profile (G1).
    assert spawned and spawned[0][:2] == ["/usr/bin/sandbox-exec", "-p"], spawned
    assert trace_of(trace_path, "sandbox_bypass") == []
    assert run.state == COMPLETED, (run.state, run.error)
    assert run.finish_reason == "completed"
    assert "finished" in _text(run)

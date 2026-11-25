"""G3 acceptance tests.

G3 as amended (S56, TCR G3 options a and b together): a bypassing command stays inside the outer
profile (G1/G2), shown on agy's real stream; and the bypass audit (on by default) records each
distinct BypassSandbox call from agy's own conversation record after the turn, without ending the
run. The synthetic-stream tests below check the stream detector kept as a second layer; real agy
strips BypassSandbox from stream-json, so that detector does not fire on it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from subagent.runs import COMPLETED, FAILED

from ._common import (
    PROVIDER,
    SEATBELT,
    can_nest_sandbox,
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


REAL_STREAM = (Path(__file__).resolve().parents[1]
               / "fixtures" / "antigravity" / "real-bypass.stdout.jsonl")


def _real_events(tmp_path: Path, scratch: Path) -> tuple[list, list[str]]:
    """agy's saved stream of a bypassing turn, placeholders filled; its run_command lines."""
    text = REAL_STREAM.read_text(encoding="utf-8")
    text = text.replace("<WORKSPACE>", str(tmp_path / "ws")).replace("<SCRATCH>", str(scratch))
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    params = [
        ev["step_update"]["tool_info"]["parameters"]
        for ev in events
        if ev.get("event") == "step_update"
        and ev["step_update"].get("tool_name") == "run_command"
        and isinstance(ev["step_update"].get("tool_info"), dict)
    ]
    assert params and all("BypassSandbox" not in p for p in params)  # agy strips the flag
    return events, [p["CommandLine"] for p in params]


def test_g3a_real_bypass_stream_is_confined_by_the_outer_profile(tmp_path, monkeypatch) -> None:
    """G3 (a): for agy's real bypassing turn, the bypassed command cannot leave the run's profile."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    outside = scratch / "outside-marker.txt"
    events, commands = _real_events(tmp_path, scratch)
    _, run, trace_path, spawned = run_fake(tmp_path, monkeypatch, events)
    assert spawned and spawned[0][:2] == [SEATBELT, "-p"], spawned
    assert run.state == COMPLETED, (run.state, run.error)
    assert not [r for r in trace_of(trace_path, "sandbox_bypass")
                if r.get("source") != "conversation_record"]  # the real stream carries no flag
    if not can_nest_sandbox():
        return  # already sandboxed: the profile text checks live in the G1 tests
    # Run the bypassed command exactly as agy's child would: under the profile the run got.
    proc = subprocess.run([SEATBELT, "-p", spawned[0][2], "/bin/sh", "-c", commands[0]],
                          capture_output=True, text=True, timeout=15, cwd=tmp_path / "ws")
    assert proc.returncode != 0, proc
    assert not outside.exists(), "a bypassed command wrote outside the workspace"

def test_g3b_real_bypass_turn_is_recorded_from_the_conversation_record(tmp_path, monkeypatch) -> None:
    """G3 (b): agy's real bypassing turn yields one sandbox_bypass record per distinct call."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    events, commands = _real_events(tmp_path, scratch)
    conversation = events[0]["conversation_id"]
    state = tmp_path / "agy_state"
    db = state / "conversations" / f"{conversation}.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("create table steps (step_index integer primary key, step_payload blob)")
    for i, command in enumerate([*commands, commands[0]]):  # the live record repeats the call
        obj = json.dumps({"BypassSandbox": True, "CommandLine": command}, separators=(",", ":"))
        con.execute("insert into steps values (?, ?)", (i, b"\x00\x12" + obj.encode() + b"\x00"))
    con.commit()
    con.close()
    agent, run, trace_path, _ = run_fake(tmp_path, monkeypatch, events,
                                         bypass_audit=True, agy_state_dir=str(state))
    records = trace_of(trace_path, "sandbox_bypass")
    assert sorted(r["command"] for r in records) == sorted(set(commands)), records
    for rec in records:
        assert rec["source"] == "conversation_record"
        assert (rec["agent_id"], rec["run_id"], rec["provider"]) == (
            agent.agent_id, run.run_id, PROVIDER)
    assert run.state == COMPLETED  # recorded, not ended

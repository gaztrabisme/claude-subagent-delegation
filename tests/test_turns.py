"""Token accounting that survives failure, per-turn records, and the run
record's new fields.

On 2026-09-18 two GLM runs that did real work were traced with usage all 0:
tokens were read only from the final `result` event, which a run that fails,
times out or is cancelled never gets. Usage is now read per assistant message
as it streams in.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from subagent_mcp.runs import CANCELLED, COMPLETED, FAILED, Registry, _Meter, exit_event
from subagent_mcp.trace import Trace

from .conftest import make_settings
from .test_runs import FakeProcess, _result, _wait


def _message(mid: str, inp: int, out: int, *, tool: str | None = None,
             cache_read: int = 0) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": f"{mid} text"}]
    if tool:
        content = [{"type": "tool_use", "id": f"tu-{mid}", "name": tool,
                    "input": {"command": mid}}]
    return {"type": "assistant", "session_id": "s1", "message": {
        "id": mid, "model": "glm-5.3-flash", "content": content,
        "usage": {"input_tokens": inp, "output_tokens": out,
                  "cache_read_input_tokens": cache_read}}}


def _registry(tmp_path: Path, monkeypatch, script, **overrides) -> Registry:
    settings = make_settings(tmp_path, **overrides)
    spawned: list[Any] = []

    def spawn(argv, env, cwd):
        item = script[len(spawned)] if len(spawned) < len(script) else script[-1]
        proc = item if not isinstance(item, list) else FakeProcess(item, argv, env)
        spawned.append(proc)
        return proc

    monkeypatch.setattr("subagent_mcp.runs._spawn_claude", spawn)
    reg = Registry(settings, start_reaper=False)
    reg.spawned = spawned  # type: ignore[attr-defined]
    return reg


def _kind(records: list[dict], kind: str) -> list[dict]:
    return [r for r in records if r["kind"] == kind]


def test_a_child_that_dies_after_two_messages_keeps_their_usage(
    tmp_path, monkeypatch, trace_records
):
    died = exit_event(1, "API Error: Request rejected (500) internal", None)
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        _message("m1", 100, 20, tool="Bash", cache_read=7),
        _message("m1", 100, 20, cache_read=7),  # same response, next content block
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        _message("m2", 50, 10),
        died,
    ]
    reg = _registry(tmp_path, monkeypatch, [events])
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = _wait(agent.delegate("do", "true"))
        assert run.state == FAILED
        assert (run.usage.input, run.usage.output, run.usage.cache_read) == (150, 30, 7)
        assert run.usage.steps == 1
        turns = _kind(trace_records, "turn")
        assert [(t["message_id"], t["input"], t["output"], t["tool_calls"]) for t in turns] == [
            ("m1", 100, 20, ["Bash"]),
            ("m2", 50, 10, []),
        ]
        assert [t["turn"] for t in turns] == [0, 1]
        assert turns[0]["lane"] == "glm" and turns[0]["provider"] == "zai"
        assert turns[0]["model"] == "glm-5.3-flash"
        assert turns[1]["ts_start"] >= turns[0]["ts_end"]
        record = _kind(trace_records, "run")[-1]
        assert record["usage"]["input"] == 150 and record["usage"]["output"] == 30
        assert record["turns"] == 2 and record["tool_calls"] == 1
        assert record["end_state"] == FAILED and record["verification_passed"] is None
    finally:
        reg.shutdown()


def test_the_result_usage_wins_when_it_is_larger(tmp_path, monkeypatch):
    events = [_message("m1", 10, 2), *_result("ok")[2:]]
    events[-1]["usage"] = {"input_tokens": 40, "output_tokens": 9}
    reg = _registry(tmp_path, monkeypatch, [events])
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = _wait(agent.delegate("do", "true"))
        assert run.state == COMPLETED
        assert (run.usage.input, run.usage.output) == (40, 9)
    finally:
        reg.shutdown()


class _Blocking:
    """Two messages, then hangs until killed: a child cancelled mid-work."""

    def __init__(self) -> None:
        self.killed = threading.Event()
        self.streamed = threading.Event()

    def events(self) -> Iterator[dict[str, Any]]:
        yield {"type": "system", "subtype": "init", "session_id": "s1"}
        yield _message("m1", 300, 40, tool="Bash")
        yield _message("m2", 200, 25)
        self.streamed.set()
        self.killed.wait(5)

    def kill(self) -> None:
        self.killed.set()


def test_a_cancelled_run_records_what_it_spent(tmp_path, monkeypatch, trace_records):
    child = _Blocking()
    reg = _registry(tmp_path, monkeypatch, [child])
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = agent.delegate("do", "true")
        assert child.streamed.wait(5)
        agent.close("cancelled by caller")
        assert run.state == CANCELLED
        assert (run.usage.input, run.usage.output) == (500, 65)
        records = _kind(trace_records, "run")
        assert records and records[-1]["usage"]["input"] == 500
        assert records[-1]["end_state"] == CANCELLED
        assert len(_kind(trace_records, "turn")) == 2
    finally:
        reg.shutdown()


def test_a_retried_rate_limited_attempt_still_counts(tmp_path, monkeypatch, trace_records):
    limited = exit_event(1, "API Error: Request rejected (429) rate limit", None)
    first = [{"type": "system", "subtype": "init", "session_id": "s1"},
             _message("m1", 70, 5), limited]
    second = [_message("m2", 30, 3), *_result("ok")[2:]]
    second[-1]["usage"] = {"input_tokens": 30, "output_tokens": 3}
    reg = _registry(tmp_path, monkeypatch, [first, second], rate_limit_backoff=0.001)
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = _wait(agent.delegate("do", "true"))
        assert run.state == COMPLETED
        assert (run.usage.input, run.usage.output) == (100, 8)
        assert [t["attempt"] for t in _kind(trace_records, "turn")] == [0, 1]
    finally:
        reg.shutdown()


def test_meter_reads_stream_events_for_first_token_and_stop_reason():
    meter = _Meter(0, 100.0)
    meter.see({"type": "stream_event", "event": {"type": "message_start", "message": {
        "id": "m1", "model": "q", "usage": {"input_tokens": 50, "output_tokens": 1}}}}, 101.0)
    meter.see({"type": "stream_event", "event": {"type": "content_block_delta"}}, 102.0)
    meter.see({"type": "stream_event", "event": {"type": "content_block_delta"}}, 103.0)
    meter.see({"type": "stream_event", "event": {
        "type": "message_delta", "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": 80}}}, 106.0)
    meter.see({"type": "stream_event", "event": {"type": "message_stop"}}, 106.0)
    [turn] = meter.records()
    assert (turn["ts_start"], turn["ts_first_token"], turn["ts_end"]) == (100.0, 102.0, 106.0)
    assert (turn["input"], turn["output"], turn["stop_reason"]) == (50, 80, "end_turn")
    assert meter.first_assistant == 102.0
    assert meter.usage()["output"] == 80


def test_synthetic_api_error_message_is_not_a_turn():
    meter = _Meter(0, 0.0)
    meter.see({"type": "assistant", "message": {"model": "<synthetic>", "content": [
        {"type": "text", "text": "API Error"}], "usage": {"input_tokens": 0}}}, 1.0)
    assert meter.records() == [] and meter.first_assistant is None


def test_run_record_fields(tmp_path, monkeypatch, trace_records):
    reg = _registry(tmp_path, monkeypatch, [_result("ok")])
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        reg.trace.verdict(agent_id=agent.agent_id, tool="Bash", action="allow", tier="policy",
                          reason="r", facts={}, latency_ms=1.0)
        reg.trace.verdict(agent_id=agent.agent_id, tool="Bash", action="deny", tier="policy",
                          reason="r", facts={}, latency_ms=1.0)
        first = _wait(agent.delegate("do", "true", parent={"claudecode/toolUseId": "toolu_1"}))
        second = _wait(agent.follow_up("more"))
        assert (first.continues, second.continues) == (0, 1)
        runs = _kind(trace_records, "run")
        assert runs[0]["guard_verdicts"] == {"allow": 1, "deny": 1, "escalate": 0}
        assert runs[1]["guard_verdicts"] == {"allow": 0, "deny": 0, "escalate": 0}
        assert runs[0]["parent"] == {"claudecode/toolUseId": "toolu_1"}
        assert "parent" not in runs[1]
        assert runs[0]["verification_passed"] is True and runs[0]["end_state"] == COMPLETED
        assert runs[0]["ttft_seconds"] is not None and runs[0]["ttft_seconds"] >= 0
        assert runs[0]["wall_seconds"] >= runs[0]["elapsed_seconds"]
        assert runs[1]["continues"] == 1 and runs[0]["refusal_code"] is None
    finally:
        reg.shutdown()


def test_child_argv_streams_partial_messages(tmp_path, monkeypatch):
    reg = _registry(tmp_path, monkeypatch, [_result("ok")])
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        _wait(agent.delegate("do", "true"))
        assert "--include-partial-messages" in reg.spawned[0].argv  # type: ignore[attr-defined]
    finally:
        reg.shutdown()


def test_stream_events_are_not_kept_in_the_attempt(tmp_path, monkeypatch):
    stream = {"type": "stream_event", "event": {"type": "content_block_delta"}}
    events = [_message("m1", 1, 1), *([stream] * 50), *_result("ok")[2:]]
    reg = _registry(tmp_path, monkeypatch, [events])
    try:
        agent = reg.create_agent("t", tmp_path, "glm")
        run = _wait(agent.delegate("do", "true"))
        assert run.state == COMPLETED
        assert not any("stream_event" in line for line in run.transcript)
    finally:
        reg.shutdown()


def test_codex_turn_gets_the_turn_completed_usage(fake_codex, tmp_path: Path, trace_records):
    settings = make_settings(tmp_path, idle_timeout=3600, run_timeout=30)
    reg = Registry(settings, start_reaper=False, trace=Trace(tmp_path / "trace.jsonl"))
    try:
        ws = tmp_path / "ws"
        ws.mkdir()
        agent = reg.create_agent("t", ws, lane="codex", fallback="none")
        assert agent.wait_ready(5) is None
        run = agent.submit("do it", verification="true")
        assert run.done.wait(10)
        turns = _kind(trace_records, "turn")
        assert len(turns) == 1
        assert turns[0]["output"] == run.usage.output == 117200
        assert turns[0]["cache_read"] == run.usage.cache_read
        assert turns[0]["lane"] == "codex" and len(turns[0]["tool_calls"]) == run.usage.steps
    finally:
        reg.shutdown()


def test_meter_usage_only_in_stream_events_after_a_tool_result():
    """U-T1: GLM's usage arrives only in message_delta, and a denied tool's
    result can come back before that delta. The turn still gets it."""
    meter = _Meter(0, 100.0)
    stream = lambda event: {"type": "stream_event", "event": event}  # noqa: E731
    zero = {"input_tokens": 0, "output_tokens": 0}
    meter.see(stream({"type": "message_start", "message": {
        "id": "m1", "model": "glm", "usage": zero}}), 101.0)
    meter.see(stream({"type": "content_block_start", "index": 0}), 102.0)
    meter.see({"type": "assistant", "message": {"id": "m1", "model": "glm", "usage": zero,
               "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}},
              103.0)
    meter.see({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "denied"}]}}, 103.5)
    meter.see(stream({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                      "usage": {"input_tokens": 3805, "output_tokens": 189,
                                "cache_read_input_tokens": 0}}), 104.0)
    meter.see(stream({"type": "message_stop"}), 104.0)
    meter.see(stream({"type": "message_start", "message": {
        "id": "m2", "model": "glm", "usage": zero}}), 105.0)
    meter.see(stream({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                      "usage": {"input_tokens": 250, "output_tokens": 79,
                                "cache_read_input_tokens": 3776}}), 106.0)
    meter.see(stream({"type": "message_stop"}), 106.0)
    first, second = meter.records()
    assert (first["input"], first["output"], first["stop_reason"]) == (3805, 189, "tool_use")
    assert first["tool_calls"] == ["Bash"]
    # The second request went out at the tool result, not at the late message_stop.
    assert second["ts_start"] == 103.5
    assert (second["input"], second["cache_read"], second["stop_reason"]) == (250, 3776, "end_turn")
    # Without a result event (a failed or cancelled run) the sum still holds it.
    assert meter.usage()["input"] == 4055 and meter.usage()["output"] == 268

"""The trace schema: required keys for every record kind, and a check that
the records real (fake-child) runs write satisfy it.

conftest.trace_records runs `problems` over every record any test writes, so
this file is the schema for the whole suite, not only for the tests below.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from subagent_mcp.lanes import load_lanes
from subagent_mcp.runs import COMPLETED, Registry
from subagent_mcp.telemetry import Telemetry
from subagent_mcp.trace import KINDS, SCHEMA, Trace

from .conftest import make_settings
from .test_runs import FakeProcess, _wait

COMMON = {"schema", "ts", "kind"}

USAGE_KEYS = {"input", "output", "cache_read", "cache_write", "reasoning", "total", "steps",
              "turns"}

REQUIRED: dict[str, set[str]] = {
    "run": {
        "run_id", "agent_id", "session_id", "model", "lane", "provider", "driver",
        "workspace", "guard", "state", "finish_reason", "elapsed_seconds", "wall_seconds",
        "ttft_seconds", "turns", "tool_calls", "continues", "end_state",
        "verification_passed", "guard_verdicts", "refusal_code", "usage", "prompt_chars",
        "result_chars", "distilled", "truncated", "verification", "error",
    },
    "hop": {
        "run_id", "agent_id", "hop", "lane", "provider", "driver", "guard", "model",
        "outcome", "code", "reset_at", "closed_until", "admission", "would_refuse",
        "admit_reason",
    },
    "turn": {
        "run_id", "agent_id", "lane", "provider", "model", "turn", "attempt", "message_id",
        "ts_start", "ts_first_token", "ts_end", "input", "output", "cache_read",
        "cache_write", "reasoning", "tool_calls", "stop_reason",
    },
    "sample": {"lane", "run_ids", "snapshot"},
    "run_summary": {
        "run_id", "agent_id", "lane", "samples", "peak_model_memory_used", "peak_swap_mb",
        "max_pressure", "peak_vram_used_mb", "decode_turns", "decode_tps_mean",
        "decode_tps_p10", "energy_wh", "gpu_seconds",
    },
    "verdict": {"agent_id", "tool", "action", "tier", "escalated", "reason", "facts",
                "latency_ms"},
    "calibration": {"run_id", "chars", "output_tokens", "observed", "assumed"},
}


def problems(record: dict[str, Any]) -> list[str]:
    """What is wrong with one record, as readable lines. Empty when it is valid."""
    kind = record.get("kind")
    label = f"{kind} {record.get('run_id') or ''}".strip()
    if kind not in REQUIRED:
        return [f"unknown kind {kind!r}"]
    found = [f"{label}: missing {key}" for key in sorted((COMMON | REQUIRED[kind]) - set(record))]
    if record.get("schema") != SCHEMA:
        found.append(f"{label}: schema {record.get('schema')!r} != {SCHEMA}")
    if kind == "run":
        usage = record.get("usage") or {}
        found += [f"{label}: usage missing {k}" for k in sorted(USAGE_KEYS - set(usage))]
        verdicts = record.get("guard_verdicts")
        if not isinstance(verdicts, dict) or set(verdicts) != {"allow", "deny", "escalate"}:
            found.append(f"{label}: guard_verdicts {verdicts!r}")
        if not isinstance(record.get("turns"), int) or not isinstance(record.get("continues"),
                                                                        int):
            found.append(f"{label}: turns/continues not integers")
    if kind == "turn":
        if not isinstance(record.get("tool_calls"), list):
            found.append(f"{label}: tool_calls is not a list")
        for key in ("input", "output", "cache_read", "cache_write", "reasoning"):
            if not isinstance(record.get(key), int):
                found.append(f"{label}: {key} is not an integer")
    if kind == "sample" and (
        not isinstance(record.get("run_ids"), list)
        or not isinstance(record.get("snapshot"), dict)
    ):
        found.append(f"{label}: run_ids/snapshot shape")
    return found


def test_every_kind_has_a_schema():
    assert set(REQUIRED) == set(KINDS)


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _events() -> list[dict[str, Any]]:
    message = {"id": "msg-1", "model": "qwen", "content": [
        {"type": "tool_use", "id": "tu-1", "name": "Bash", "input": {"command": "ls"}}],
        "usage": {"input_tokens": 30, "output_tokens": 12}}
    return [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "stream_event", "event": {"type": "message_start", "message": {
            "id": "msg-1", "model": "qwen", "usage": {"input_tokens": 30, "output_tokens": 1}}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0}},
        {"type": "assistant", "message": message},
        {"type": "stream_event", "event": {"type": "message_delta",
                                           "delta": {"stop_reason": "tool_use"},
                                           "usage": {"output_tokens": 12}}},
        {"type": "stream_event", "event": {"type": "message_stop"}},
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "done",
         "session_id": "s1", "usage": {"input_tokens": 30, "output_tokens": 12}, "num_turns": 1},
    ]


def test_records_from_a_local_lane_run_satisfy_the_schema(tmp_path: Path, monkeypatch,
                                                          mock_endpoint):
    """A routed run on omlx with telemetry on: run, hop, turn, run_summary and
    sample records, each validated from the files the server wrote."""
    monkeypatch.setenv("SAM_OMLX_API_KEY", "omlx-test")
    mock_endpoint.routes["/omlx/api/status"] = (200, {"status": "ok", "models_loaded": 1})
    lanes = load_lanes({"SAM_OMLX_BASE_URL": mock_endpoint.url("omlx")})
    from dataclasses import replace

    lanes["omlx"] = replace(lanes["omlx"],
                            health_url=f"{mock_endpoint.url('omlx')}/api/status")
    settings = make_settings(tmp_path, lanes=lanes, trace=str(tmp_path / "trace.jsonl"))
    gate = threading.Event()

    def spawn(argv, env, cwd):
        return FakeProcess(_events(), argv, env, gate=gate)

    monkeypatch.setattr("subagent_mcp.runs._spawn_claude", spawn)
    trace = Trace(tmp_path / "trace.jsonl")
    telemetry = Telemetry(
        Trace(tmp_path / "metrics.jsonl"),
        probes={"omlx": lambda lane: {"omlx": {"model_memory_used": 5}, "mac": {}}},
        env={},
        interval=0.05,
    )
    reg = Registry(settings, start_reaper=False, trace=trace, telemetry=telemetry)
    try:
        agent = reg.create_agent("t", tmp_path, lane="omlx", fallback="none")
        run = agent.delegate("do it", "true")
        threading.Timer(0.2, gate.set).start()
        _wait(run)
        assert run.state == COMPLETED
    finally:
        reg.shutdown()

    records = _read(tmp_path / "trace.jsonl") + _read(tmp_path / "metrics.jsonl")
    kinds = {r["kind"] for r in records}
    assert {"run", "hop", "turn", "run_summary", "sample"} <= kinds
    assert [p for r in records for p in problems(r)] == []


def test_verdict_and_calibration_records_satisfy_the_schema(tmp_path: Path):
    trace = Trace(tmp_path / "trace.jsonl")
    trace.verdict(agent_id="a1", tool="Bash", action="allow", tier="policy", reason="r",
                  facts={}, latency_ms=1.0)
    trace.calibration(run_id="run-1", chars=350, output_tokens=100, assumed=3.5)
    records = _read(tmp_path / "trace.jsonl")
    assert [r["kind"] for r in records] == ["verdict", "calibration"]
    assert [p for r in records for p in problems(r)] == []


def test_the_validator_catches_a_missing_key():
    assert problems({"schema": SCHEMA, "ts": 0, "kind": "turn", "run_id": "r"})
    assert problems({"schema": SCHEMA, "ts": 0, "kind": "nonsense"}) == ["unknown kind 'nonsense'"]

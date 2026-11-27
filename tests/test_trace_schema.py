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

from subagent.runs import COMPLETED, Registry, Run
from subagent.telemetry.sampler import Telemetry
from subagent.telemetry.source_usage import make_source_usage
from subagent.telemetry.trace import KINDS, SCHEMA, Trace

from .conftest import default_providers, make_settings
from .test_runs import FakeProcess, _wait

COMMON = {"schema", "ts", "kind"}
# Optional on every kind: the bench harness tags records with its run id, and
# the loop tags them with the delegation they belong to.
COMMON_OPTIONAL = {"bench_run_id", "delegation_id"}

USAGE_KEYS = {"input", "output", "cache_read", "cache_write", "reasoning", "total", "steps",
              "turns"}
# Fields a usage dict may also carry, never required: credits is Copilot's
# AI-credit spend (nano-AIU / 1e9), present only on providers that bill it.
OPTIONAL_USAGE_KEYS = {"credits", "usage_unreported"}

REQUIRED: dict[str, set[str]] = {
    "run": {
        "run_id", "agent_id", "session_id", "model", "lane", "provider", "driver",
        "workspace", "guard", "state", "finish_reason", "elapsed_seconds", "wall_seconds",
        "ttft_seconds", "turns", "tool_calls", "continues", "end_state",
        "verification_passed", "guard_verdicts", "refusal_code", "usage", "prompt_chars",
        "result_chars", "distilled", "truncated", "verification", "error", "refused_codes",
    },
    "hop": {
        "run_id", "agent_id", "hop", "lane", "provider", "driver", "guard", "model",
        "outcome", "code", "reset_at", "closed_until", "admission", "would_refuse",
        "admit_reason", "cold_load",
    },
    "turn": {
        "run_id", "agent_id", "lane", "provider", "model", "turn", "attempt", "message_id",
        "ts_start", "ts_first_token", "ts_end", "input", "output", "cache_read",
        "cache_write", "reasoning", "tool_calls", "stop_reason", "source_usage", "unmeasured",
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
    "delegation": {
        "orchestrator", "mode", "status", "rounds", "reviews", "usage_total",
        "credits_total", "cost_total", "wall_seconds", "changed_files", "verified_pass",
    },
    # R1a A10.3 and A12: additive record types, schema version unchanged.
    "egress_refused": {"agent_id", "run_id", "provider", "host", "port", "type"},
    "guard_tamper": {"agent_id", "run_id", "path", "type"},
}

# Fields a kind may also carry, never required.
OPTIONAL: dict[str, set[str]] = {
    "run": {"cost", "native_usage", "uat", "usage_unreported", "credits",
            "credits_unit", "cash_usd", "turn_usage", "extra", "needs_human",
            "human_action", "plan_id", "verification_note"},
    "delegation": {"stopped_because", "test_writer", "uat"},
}

# R1a-agy G3 and addendum R2: additive record types, schema version unchanged.
REQUIRED["sandbox_bypass"] = {"agent_id", "run_id", "provider", "tool", "command", "type"}
REQUIRED["bypass_audit_unavailable"] = {"agent_id", "run_id", "provider", "reason", "type"}
OPTIONAL["sandbox_bypass"] = {"source"}

UAT_KEYS = {"row", "label", "kind", "command", "passed", "output_tail"}


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
        found += [f"{label}: usage unknown {k}" for k in
                  sorted(set(usage) - USAGE_KEYS - OPTIONAL_USAGE_KEYS)]
        if "credits" in usage and usage["credits"] is not None and not isinstance(
            usage["credits"], (int, float)
        ):
            found.append(f"{label}: usage credits not a number")
        refused = record.get("refused_codes")
        if not isinstance(refused, list) or not all(c is None or isinstance(c, str)
                                                    for c in refused):
            found.append(f"{label}: refused_codes {refused!r}")
        verdicts = record.get("guard_verdicts")
        if not isinstance(verdicts, dict) or set(verdicts) != {"allow", "deny", "escalate"}:
            found.append(f"{label}: guard_verdicts {verdicts!r}")
        if not isinstance(record.get("turns"), int) or not isinstance(record.get("continues"),
                                                                        int):
            found.append(f"{label}: turns/continues not integers")
        cost = record.get("cost")
        if cost is not None and (
            not isinstance(cost, dict)
            or not {"provider_usd", "counterfactual_usd", "kind", "note"} <= set(cost)
        ):
            found.append(f"{label}: cost shape {cost!r}")
        if "native_usage" in record and type(record["native_usage"]) is not bool:
            found.append(f"{label}: native_usage is not boolean")
        for key in ("credits", "cash_usd"):
            if key in record and record[key] is not None and (
                type(record[key]) not in (int, float)
            ):
                found.append(f"{label}: {key} is not a number or null")
        for key in ("credits_unit", "human_action", "plan_id", "verification_note"):
            if key in record and record[key] is not None and not isinstance(record[key], str):
                found.append(f"{label}: {key} is not a string or null")
        if "needs_human" in record and type(record["needs_human"]) is not bool:
            found.append(f"{label}: needs_human is not boolean")
        if "extra" in record and not isinstance(record["extra"], dict):
            found.append(f"{label}: extra is not a dict")
        for source in (record, usage):
            if "usage_unreported" in source and (
                not isinstance(source["usage_unreported"], list)
                or any(not isinstance(key, str) or key not in USAGE_KEYS
                       for key in source["usage_unreported"])
            ):
                found.append(f"{label}: usage_unreported is not a list of usage keys")
        if "turn_usage" in record:
            turns = record["turn_usage"]
            if not isinstance(turns, list):
                found.append(f"{label}: turn_usage is not a list")
            else:
                keys = {"ts_start", "ts_first_token", "ts_end", "provider", "model",
                        "input", "output", "cache_read", "cache_write", "reasoning",
                        "tool_calls", "stop_reason", "usage_unreported"}
                for i, turn in enumerate(turns):
                    if not isinstance(turn, dict) or not keys <= set(turn):
                        found.append(f"{label}: turn_usage {i} shape")
    if kind == "turn":
        if not isinstance(record.get("tool_calls"), list):
            found.append(f"{label}: tool_calls is not a list")
        for key in ("input", "output", "cache_read", "cache_write", "reasoning"):
            if not isinstance(record.get(key), int):
                found.append(f"{label}: {key} is not an integer")
        source_usage = record.get("source_usage")
        input_sources = {"harness_prompt", "plan", "tool_results", "test_output"}
        if not isinstance(source_usage, dict) or set(source_usage) != input_sources | {"model_output"}:
            found.append(f"{label}: source_usage bucket names")
        else:
            for source in input_sources:
                bucket = source_usage[source]
                if not isinstance(bucket, dict) or set(bucket) != {"input_chars", "input_tokens_est"}:
                    found.append(f"{label}: source_usage {source} shape")
                    continue
                for unit in ("input_chars", "input_tokens_est"):
                    if type(bucket.get(unit)) is not int or bucket[unit] < 0:
                        found.append(f"{label}: source_usage {source}.{unit} is not a non-negative integer")
            model_output = source_usage["model_output"]
            if not isinstance(model_output, dict) or set(model_output) != {
                "output_tokens", "reasoning_tokens"
            }:
                found.append(f"{label}: source_usage model_output shape")
            else:
                for unit in ("output_tokens", "reasoning_tokens"):
                    if type(model_output.get(unit)) is not int or model_output[unit] < 0:
                        found.append(f"{label}: source_usage model_output.{unit} is not a non-negative integer")
        unmeasured = record.get("unmeasured")
        if not isinstance(unmeasured, list) or any(
            not isinstance(source, str)
            or source not in {"harness_prompt", "plan", "tool_results", "test_output", "model_output"}
            for source in unmeasured
        ):
            found.append(f"{label}: unmeasured is not a list of source bucket names")
    if kind == "sample" and (
        not isinstance(record.get("run_ids"), list)
        or not isinstance(record.get("snapshot"), dict)
    ):
        found.append(f"{label}: run_ids/snapshot shape")
    if kind == "delegation":
        orchestrator = record.get("orchestrator")
        if not isinstance(orchestrator, dict) or not {"harness", "model"} <= set(orchestrator):
            found.append(f"{label}: orchestrator shape")
        if not isinstance(record.get("rounds"), list) or not isinstance(record.get("reviews"), list):
            found.append(f"{label}: rounds/reviews not lists")
        if not isinstance(record.get("usage_total"), dict):
            found.append(f"{label}: usage_total not a dict")
        cost_total = record.get("cost_total")
        if not isinstance(cost_total, dict) or set(cost_total) != {"provider_usd", "counterfactual_usd"}:
            found.append(f"{label}: cost_total shape")
        for i, round_ in enumerate(record.get("rounds") or []):
            if not isinstance(round_, dict):
                found.append(f"{label}: round {i} not a dict")
                continue
            if not isinstance(round_.get("usage"), dict):
                found.append(f"{label}: round {i} usage not a dict")
            round_cost = round_.get("cost")
            if round_cost is not None and (
                not isinstance(round_cost, dict)
                or not {"provider_usd", "counterfactual_usd", "kind", "note"} <= set(round_cost)
            ):
                found.append(f"{label}: round {i} cost shape")
    if kind in ("run", "delegation") and "uat" in record:
        uat = record["uat"]
        if not isinstance(uat, list):
            found.append(f"{label}: uat is not a list")
        else:
            for i, row in enumerate(uat):
                if not isinstance(row, dict) or set(row) != UAT_KEYS:
                    found.append(f"{label}: uat row {i} keys {set(row) if isinstance(row, dict) else row!r}")
                    continue
                if not isinstance(row["row"], int) or isinstance(row["row"], bool):
                    found.append(f"{label}: uat row {i} number is not an integer")
                if not isinstance(row["label"], str):
                    found.append(f"{label}: uat row {i} label is not a string")
                if row["kind"] not in ("command", "prose"):
                    found.append(f"{label}: uat row {i} kind {row['kind']!r}")
                if row["command"] is not None and not isinstance(row["command"], str):
                    found.append(f"{label}: uat row {i} command is not a string or null")
                if row["kind"] == "command" and not isinstance(row["command"], str):
                    found.append(f"{label}: uat row {i} command kind needs a string")
                if row["kind"] == "prose" and row["command"] is not None:
                    found.append(f"{label}: uat row {i} prose command must be null")
                if row["passed"] is not None and type(row["passed"]) is not bool:
                    found.append(f"{label}: uat row {i} passed is not boolean or null")
                if not isinstance(row["output_tail"], str) or len(row["output_tail"]) > 2000:
                    found.append(f"{label}: uat row {i} output_tail is not a string capped at 2000")
    return found


def test_every_kind_has_a_schema():
    assert set(REQUIRED) == set(KINDS)


def test_optional_keys_are_not_required():
    for kind, optionals in OPTIONAL.items():
        assert not (optionals & REQUIRED[kind]), f"{kind}: {optionals} overlaps REQUIRED"
    for kind, required in REQUIRED.items():
        assert COMMON_OPTIONAL.isdisjoint(required), f"{kind}: common-optional overlap"


def _native_record(tmp_path: Path) -> dict[str, Any]:
    run = Run(run_id="run-native", agent_id="agent-native", prompt="task",
              state=COMPLETED, native_usage=True, native_verification=True)
    run.started_at, run.finished_at = 1, 2
    run.usage.set_native({"input": 8, "output": 2, "cache_read": 0, "cache_write": 0,
                          "reasoning": 0, "total": 10, "steps": 1, "turns": 1})
    run.native_outcome = {
        "verification": {"command": "true", "passed": True, "exit_code": 0,
                         "output_tail": "checked"},
        "uat": [{"row": 1, "label": "works", "kind": "command", "command": "true",
                 "passed": True, "output_tail": "checked"}],
        "usage_unreported": ["cache_write", "reasoning"], "credits": None,
        "credits_unit": None, "cash_usd": 0, "turn_usage": [], "needs_human": False,
        "human_action": None, "plan_id": "plan-1", "extra": {"profile": "test", "hops": []},
    }
    path = tmp_path / "native-trace.jsonl"
    Trace(path).run(run, str(tmp_path), "native-model")
    return json.loads(path.read_text())


def test_native_provider_fields_extend_schema_additively(tmp_path: Path):
    record = _native_record(tmp_path)
    assert SCHEMA == 3
    assert problems(record) == []
    assert record["credits"] is None
    assert record["cash_usd"] == 0
    assert record["usage_unreported"] == ["cache_write", "reasoning"]
    assert record["turns"] == 1


def test_native_provider_field_shape_errors_are_rejected(tmp_path: Path):
    record = _native_record(tmp_path)
    for fields in ({"cash_usd": "unknown"}, {"credits": True}, {"needs_human": 1},
                   {"usage_unreported": [["bad"]]}, {"turn_usage": "bad"},
                   {"extra": []}, {"uat": [{"passed": True}]},
                   {"refused_codes": "envelope"}):
        assert problems({**record, **fields}), fields


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
        {"type": "user", "message": {
            "content": [{"type": "tool_result", "content": "tool output"}]
        }},
        {"type": "result", "subtype": "success", "is_error": False, "result": "done",
         "session_id": "s1", "usage": {"input_tokens": 30, "output_tokens": 12}, "num_turns": 1},
    ]


def test_records_from_a_local_lane_run_satisfy_the_schema(tmp_path: Path, monkeypatch,
                                                          mock_endpoint):
    """A routed run on omlx with telemetry on: run, hop, turn, run_summary and
    sample records, each validated from the files the server wrote."""
    monkeypatch.setenv("OMLX_API_KEY", "omlx-test")
    mock_endpoint.routes["/omlx/api/status"] = (200, {"status": "ok", "models_loaded": 1})
    providers = default_providers()
    providers["omlx"]["base_url"] = mock_endpoint.url("omlx")
    providers["omlx"]["health"]["url"] = f"{mock_endpoint.url('omlx')}/api/status"
    settings = make_settings(tmp_path, providers=providers,
                             trace=str(tmp_path / "trace.jsonl"))
    gate = threading.Event()

    def spawn(argv, env, cwd):
        return FakeProcess(_events(), argv, env, gate=gate)

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    trace = Trace(tmp_path / "trace.jsonl")
    telemetry = Telemetry(
        Trace(tmp_path / "metrics.jsonl"),
        probes={"omlx": lambda cfg: {"omlx": {"model_memory_used": 5}, "mac": {}}},
        interval=0.05,
    )
    reg = Registry(settings, start_reaper=False, trace=trace, telemetry=telemetry)
    try:
        agent = reg.create_agent("t", tmp_path, provider="omlx", fallback="none")
        run = agent.delegate("do it", "true")
        threading.Timer(0.2, gate.set).start()
        _wait(run)
        assert run.state == COMPLETED
    finally:
        reg.shutdown()

    records = _read(tmp_path / "trace.jsonl") + _read(tmp_path / "metrics.jsonl")
    kinds = {r["kind"] for r in records}
    assert {"run", "hop", "turn", "run_summary", "sample"} <= kinds
    turn = next(r for r in records if r["kind"] == "turn")
    assert turn["input"] == 30
    assert turn["output"] == 12
    assert turn["source_usage"]["harness_prompt"]["input_chars"] == len("do it")
    assert turn["source_usage"]["plan"]["input_chars"] == 0
    assert turn["source_usage"]["test_output"]["input_chars"] == 0
    assert turn["source_usage"]["tool_results"]["input_chars"] == len("tool output")
    assert turn["source_usage"]["model_output"] == {
        "output_tokens": 12,
        "reasoning_tokens": 0,
    }
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


def test_turn_source_has_five_named_buckets():
    usage, unmeasured = make_source_usage(
        {"harness_prompt": 70, "plan": 35, "tool_results": 7, "test_output": 14},
        output_tokens=11,
        reasoning_tokens=3,
    )
    assert set(usage) == {
        "harness_prompt", "plan", "tool_results", "test_output", "model_output"
    }
    assert set(usage["model_output"]) == {"output_tokens", "reasoning_tokens"}
    assert unmeasured == []


def test_turn_source_estimates_tokens_from_characters():
    usage, unmeasured = make_source_usage(
        {"harness_prompt": 70, "plan": 35, "tool_results": 7, "test_output": 14},
        output_tokens=0,
        reasoning_tokens=0,
        chars_per_token=3.5,
    )
    assert usage["harness_prompt"] == {"input_chars": 70, "input_tokens_est": 20}
    assert usage["plan"] == {"input_chars": 35, "input_tokens_est": 10}
    assert usage["tool_results"] == {"input_chars": 7, "input_tokens_est": 2}
    assert usage["test_output"] == {"input_chars": 14, "input_tokens_est": 4}
    assert unmeasured == []


def test_turn_source_preserves_provider_totals():
    provider_totals = {"input": 31, "output": 13, "reasoning": 5}
    usage, _ = make_source_usage(
        {"harness_prompt": 10, "plan": 20, "tool_results": 0, "test_output": 0},
        output_tokens=provider_totals["output"],
        reasoning_tokens=provider_totals["reasoning"],
    )
    assert usage["model_output"] == {"output_tokens": 13, "reasoning_tokens": 5}
    assert provider_totals == {"input": 31, "output": 13, "reasoning": 5}


def test_turn_source_marks_unobservable_buckets():
    usage, unmeasured = make_source_usage(
        {"harness_prompt": 10, "plan": 20, "tool_results": 12, "test_output": 0},
        output_tokens=1,
        reasoning_tokens=0,
        unmeasured=("tool_results",),
    )
    assert usage["tool_results"] == {"input_chars": 0, "input_tokens_est": 0}
    assert unmeasured == ["tool_results"]

    invalid, invalid_unmeasured = make_source_usage(
        {"harness_prompt": "unknown", "plan": 0, "tool_results": 0, "test_output": 0},
        output_tokens=1,
        reasoning_tokens=0,
        chars_per_token=0,
    )
    assert invalid["harness_prompt"] == {"input_chars": 0, "input_tokens_est": 0}
    assert invalid_unmeasured == ["harness_prompt", "plan", "tool_results", "test_output"]

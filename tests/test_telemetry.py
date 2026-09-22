"""Local-provider telemetry: probe parsing, sampler lifecycle, run summaries
and admission thresholds. Every probe is stubbed; nothing here reaches the
network, ssh, sysctl or pmset."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from subagent.lane_state import LaneState
from subagent.runs import COMPLETED, FAILED, Registry
from subagent.telemetry import sampler as telemetry
from subagent.telemetry.sampler import Telemetry, Thresholds, breaches, p10, summarize
from subagent.telemetry.trace import Trace

from .conftest import default_providers, make_settings, provider_cfg
from .test_runs import FakeProcess, _result, _wait

OMLX_STATUS = {
    "status": "ok", "models_loaded": 1, "models_loading": 0, "active_requests": 2,
    "waiting_requests": 3, "model_memory_used": 20_000_000_000,
    "model_memory_max": 70_268_170_440, "avg_prefill_tps": 187.0, "avg_generation_tps": 32.3,
    "total_prompt_tokens": 11533333, "total_completion_tokens": 681514, "version": "x",
}
SWAP = "vm.swapusage: total = 2048.00M  used = 1536.50M  free = 511.50M  (encrypted)\n"
THERM_OK = ("Note: No thermal warning level has been recorded\n"
            "Note: No performance warning level has been recorded\n")
THERM_HOT = "CPU_Scheduler_Limit \t= 100\nCPU_Speed_Limit \t= 70\nThermal warning level set to 2.\n"


def _omlx():
    return provider_cfg("omlx")


def _bppc(host: str | None = "10.0.0.9"):
    return provider_cfg("bppc", base_url=f"http://{host}:8080" if host else None)


# --- probe shapes ---------------------------------------------------------------


def test_omlx_snapshot_shape(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def http(url, headers=None, timeout=3.0):
        calls.append((url, headers or {}))
        return 200, json.dumps(OMLX_STATUS).encode()

    def run(argv, timeout=3.0):
        joined = " ".join(argv)
        if "vm.swapusage" in joined:
            return SWAP
        if "memorystatus_vm_pressure_level" in joined:
            return "2\n"
        if argv[:1] == ["pmset"]:
            return THERM_OK
        raise AssertionError(joined)

    monkeypatch.setattr(telemetry, "_http_get", http)
    monkeypatch.setattr(telemetry, "_run", run)
    lane = replace(_omlx(), api_key_default="k-1")
    snap = telemetry.probe_local(lane)
    assert set(snap) == {"omlx", "mac"}
    assert set(snap["omlx"]) == set(telemetry.OMLX_FIELDS)
    assert snap["omlx"]["waiting_requests"] == 3
    assert snap["mac"] == {"swap_used_mb": 1536.5, "pressure_level": 2, "thermal": "nominal"}
    assert calls == [("http://127.0.0.1:8000/api/status", {"Authorization": "Bearer k-1"})]


def test_therm_parse_reads_a_warning():
    assert telemetry.parse_therm(THERM_HOT) == {"thermal": "2", "cpu_speed_limit": 70}
    assert telemetry.parse_swap_mb("used = 1.50G") == 1536.0


def test_bppc_snapshot_shape(monkeypatch):
    seen: dict[str, Any] = {}

    def http(url, headers=None, timeout=3.0):
        if url.endswith("/slots"):
            return 200, json.dumps([{"id": 0, "is_processing": True},
                                    {"id": 1, "is_processing": False}]).encode()
        if url.endswith("/metrics"):
            return 501, b""
        raise AssertionError(url)

    def run(argv, timeout=3.0):
        seen["argv"], seen["timeout"] = argv, timeout
        return "120, 16303, 7, 18.59, 35\n"

    monkeypatch.setattr(telemetry, "_http_get", http)
    monkeypatch.setattr(telemetry, "_run", run)
    snap = telemetry.probe_local(_bppc("10.0.0.9"))
    assert snap["gpu"] == {"memory_used_mb": 120.0, "memory_total_mb": 16303.0,
                           "utilization_pct": 7.0, "power_w": 18.59, "temperature_c": 35.0}
    assert snap["slots"] == {"slots_total": 2, "slots_busy": 1}
    assert snap["metrics"] == {"error": "HTTP 501 from http://10.0.0.9:8081/metrics"}
    argv = seen["argv"]
    assert argv[:5] == ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes"]
    assert argv[5] == "bppc@10.0.0.9" and argv[6] == "nvidia-smi"
    assert seen["timeout"] == 3.0


def test_metrics_probe_reads_omlx_json_or_prometheus_text(monkeypatch):
    body = json.dumps({"status": "ok", "waiting_requests": 4, "model_memory_used": 123}).encode()
    monkeypatch.setattr(telemetry, "_http_get", lambda url, headers=None, timeout=3.0: (200, body))
    snap = telemetry.metrics_probe("http://h:8000/api/status", {})
    assert snap["omlx"]["waiting_requests"] == 4 and snap["omlx"]["model_memory_used"] == 123
    prom = b"# HELP x\nllamacpp:predicted_tokens_seconds 41.5\nllamacpp:requests_processing 1\n"
    monkeypatch.setattr(telemetry, "_http_get", lambda url, headers=None, timeout=3.0: (200, prom))
    assert telemetry.metrics_probe("http://h:8081/metrics", {})["metrics"] == {
        "predicted_tokens_seconds": 41.5, "requests_processing": 1.0}


def test_probe_for_is_none_only_when_the_probe_spec_is_empty():
    assert telemetry.probe_for(provider_cfg("glm")) is None
    assert telemetry.probe_for(_omlx()) is telemetry.probe_local
    assert telemetry.probe_for(_bppc()) is telemetry.probe_local


def test_host_cmd_runs_an_argv_and_parses_swap_or_json(monkeypatch):
    def run(argv, timeout=3.0):
        if argv == ["ssh", "host", "swap"]:
            return "vm.swapusage: total = 1024.00M  used = 512.00M\n"
        return '{"pressure_level": 2}'

    monkeypatch.setattr(telemetry, "_run", run)
    assert telemetry.host_probe(("ssh", "host", "swap"), "swap_mb") == {"swap_used_mb": 512.0}
    assert telemetry.host_probe(("cat", "status.json"), "json") == {"pressure_level": 2}


def test_probe_failures_are_recorded_not_raised():
    # conftest makes every HTTP and subprocess probe raise.
    omlx = telemetry.probe_local(_omlx())
    assert "error" in omlx["metrics"]
    assert {"swap_error", "pressure_error", "thermal_error"} <= set(omlx["mac"])
    bppc = telemetry.probe_local(_bppc())
    assert "error" in bppc["gpu"] and "error" in bppc["slots"] and "error" in bppc["metrics"]
    assert telemetry.probe_local(_bppc(None)) == {"error": "bppc: host not resolved"}

    def broken(lane):
        raise RuntimeError("boom")

    hub = Telemetry(None, probes={"omlx": broken})
    assert hub.snapshot(_omlx()) == {"error": "RuntimeError: boom"}


def test_nvidia_na_values_are_none():
    assert telemetry.parse_nvidia("1, 2, [N/A], 3, 4")["utilization_pct"] is None
    with pytest.raises(ValueError):
        telemetry.parse_nvidia("garbage")


# --- sampler --------------------------------------------------------------------


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _until(predicate, timeout: float = 3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _samplers() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("sam-sampler-omlx")]


def test_sampler_starts_on_first_child_writes_rows_and_stops_when_idle(tmp_path: Path):
    metrics = tmp_path / "metrics.jsonl"
    hub = Telemetry(Trace(metrics), probes={"omlx": lambda cfg: {"mac": {"swap_used_mb": 1}}},
                    interval=0.05)
    lane = _omlx()
    assert not hub.active("omlx")
    hub.begin(lane, "run-1")
    assert hub.active("omlx")
    assert _until(lambda: len(_rows(metrics)) >= 3)
    samples = hub.end("omlx", "run-1")
    assert len(samples) >= 3
    # Stops within one interval of going idle.
    assert _until(lambda: not hub.active("omlx"), timeout=0.05 + 0.5)
    assert _until(lambda: not _samplers(), timeout=1.0)
    settled = len(_rows(metrics))
    time.sleep(0.2)
    assert len(_rows(metrics)) == settled
    row = _rows(metrics)[0]
    assert row["kind"] == "sample" and row["lane"] == "omlx" and row["run_ids"] == ["run-1"]
    assert row["snapshot"] == {"mac": {"swap_used_mb": 1}}


def test_one_sampler_for_two_children_on_one_lane(tmp_path: Path):
    metrics = tmp_path / "metrics.jsonl"
    hub = Telemetry(Trace(metrics), probes={"omlx": lambda cfg: {}}, interval=0.05)
    lane = _omlx()
    hub.begin(lane, "run-a")
    hub.begin(lane, "run-b")
    assert _until(lambda: any(r["run_ids"] == ["run-a", "run-b"] for r in _rows(metrics)))
    assert len(_samplers()) == 1
    hub.end("omlx", "run-a")
    assert _until(lambda: _rows(metrics)[-1]["run_ids"] == ["run-b"])
    assert hub.active("omlx")
    hub.end("omlx", "run-b")
    assert _until(lambda: not hub.active("omlx"))
    hub.shutdown()


def test_sampler_ignores_cloud_providers(tmp_path: Path):
    hub = Telemetry(Trace(tmp_path / "m.jsonl"), interval=0.05)
    hub.begin(provider_cfg("glm"), "run-1")
    assert not hub.active("glm") and hub.end("glm", "run-1") == []


def test_registry_default_telemetry_uses_sample_seconds(tmp_path: Path):
    settings = make_settings(tmp_path, sample_seconds=2.5)
    reg = Registry(settings, start_reaper=False)
    try:
        assert reg.telemetry.interval == 2.5
    finally:
        reg.shutdown()


# --- summary math ---------------------------------------------------------------


def test_run_summary_math_is_exact():
    samples = [
        (0.0, {"gpu": {"power_w": 100.0, "utilization_pct": 50.0, "memory_used_mb": 9000.0},
               "omlx": {"model_memory_used": 10}, "mac": {"swap_used_mb": 5.0,
                                                          "pressure_level": 1}}),
        (10.0, {"gpu": {"power_w": 200.0, "utilization_pct": 100.0, "memory_used_mb": 12000.0},
                "omlx": {"model_memory_used": 30}, "mac": {"swap_used_mb": 50.0,
                                                           "pressure_level": 4}}),
        (20.0, {"gpu": {"power_w": 100.0, "utilization_pct": 0.0, "memory_used_mb": 11000.0},
                "omlx": {"error": "down"}, "mac": {"swap_used_mb": 20.0, "pressure_level": 2}}),
    ]
    # Ten turns decoding at 10, 20, ..., 100 tok/s, plus two with no timing.
    turns = [{"output": 10 * k, "ts_first_token": 100.0, "ts_end": 101.0} for k in range(1, 11)]
    turns += [{"output": 5, "ts_first_token": None, "ts_end": 3.0},
              {"output": 0, "ts_first_token": 1.0, "ts_end": 2.0}]
    out = summarize(samples, turns)
    assert out == {
        "samples": 3,
        "peak_model_memory_used": 30.0,
        "peak_swap_mb": 50.0,
        "max_pressure": 4.0,
        "peak_vram_used_mb": 12000.0,
        "decode_turns": 10,
        "decode_tps_mean": 55.0,
        "decode_tps_p10": 10.0,
        # (100+200)/2*10 + (200+100)/2*10 = 3000 J = 0.8333 Wh
        "energy_wh": round(3000 / 3600, 4),
        # (0.5+1)/2*10 + (1+0)/2*10
        "gpu_seconds": 12.5,
    }


def test_summary_of_nothing_is_nones():
    out = summarize([], [])
    assert out["samples"] == 0 and out["energy_wh"] is None and out["decode_tps_mean"] is None


def test_p10_nearest_rank():
    assert p10([]) is None
    assert p10([5.0]) == 5.0
    assert p10([float(v) for v in range(1, 21)]) == 2.0


# --- admission ------------------------------------------------------------------


def test_thresholds_from_config_and_breaches():
    limits = Thresholds.from_spec({
        "max_swap_mb": 1000, "max_pressure": 1, "max_waiting": 2, "enforce": True,
    })
    assert limits == Thresholds(1000.0, 1, 2, None, True)
    snap = {"mac": {"swap_used_mb": 1500.0, "pressure_level": 2},
            "omlx": {"waiting_requests": 3}}
    assert len(breaches(snap, limits)) == 3
    # A value the probe could not read is never a breach.
    assert breaches({"mac": {"swap_error": "x"}, "omlx": {"error": "x"}}, limits) == []
    vram = Thresholds.from_spec({"min_vram_free_mb": 4000})
    assert breaches({"gpu": {"memory_used_mb": 14000.0, "memory_total_mb": 16303.0}}, vram)
    assert not breaches({"gpu": {"memory_used_mb": 100.0, "memory_total_mb": 16303.0}}, vram)
    assert not Thresholds.from_spec({}).enforce
    assert provider_cfg("omlx", probe={"admit": {"max_swap_mb": 12}}).probe.admit[
        "max_swap_mb"] == 12


def _omlx_registry(tmp_path: Path, monkeypatch, mock_endpoint, admit: dict[str, Any]):
    monkeypatch.setenv("OMLX_API_KEY", "omlx-test")
    mock_endpoint.routes["/omlx/api/status"] = (200, {"status": "ok", "models_loaded": 1})
    providers = default_providers()
    providers["omlx"]["base_url"] = mock_endpoint.url("omlx")
    providers["omlx"]["health"]["url"] = f"{mock_endpoint.url('omlx')}/api/status"
    providers["omlx"]["probe"]["admit"] = admit
    settings = make_settings(tmp_path, providers=providers)
    spawned: list[FakeProcess] = []

    def spawn(argv, env_, cwd):
        spawned.append(FakeProcess(_result("done"), argv, env_))
        return spawned[-1]

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    hub = Telemetry(
        None,
        probes={"omlx": lambda cfg: {"mac": {"swap_used_mb": 5000.0, "pressure_level": 1},
                                     "omlx": {"waiting_requests": 0}}},
        interval=0.05,
    )
    reg = Registry(settings, start_reaper=False, telemetry=hub)
    reg.spawned = spawned  # type: ignore[attr-defined]
    return reg


def test_admission_is_log_only_by_default(tmp_path, monkeypatch, mock_endpoint, trace_records):
    reg = _omlx_registry(tmp_path, monkeypatch, mock_endpoint, {"max_swap_mb": 1000})
    try:
        agent = reg.create_agent("t", tmp_path, provider="omlx", fallback="none")
        run = _wait(agent.delegate("do it", "true"))
        assert run.state == COMPLETED and len(reg.spawned) == 1  # type: ignore[attr-defined]
        hop = run.hops[0]
        assert hop["outcome"] == "ran" and hop["would_refuse"] is True
        assert "swap 5000 MB > 1000" in hop["admit_reason"]
        record = next(r for r in trace_records if r["kind"] == "hop")
        assert record["admission"]["mac"]["swap_used_mb"] == 5000.0
        assert record["would_refuse"] is True
    finally:
        reg.shutdown()


def test_admission_enforced_skips_the_hop_without_closing_the_lane(
    tmp_path, monkeypatch, mock_endpoint, trace_records
):
    reg = _omlx_registry(tmp_path, monkeypatch, mock_endpoint,
                         {"max_swap_mb": 1000, "enforce": True})
    try:
        agent = reg.create_agent("t", tmp_path, provider="omlx", fallback="none")
        run = _wait(agent.delegate("do it", "true"))
        assert run.state == FAILED and run.finish_reason == "no_lane"
        assert reg.spawned == []  # type: ignore[attr-defined]
        hop = run.hops[0]
        assert (hop["outcome"], hop["code"]) == ("refused", "admission")
        assert hop["would_refuse"] is True
        assert LaneState(reg.settings.session_root).closed("omlx") is None
        record = next(r for r in trace_records if r["kind"] == "run")
        assert record["refusal_code"] == "admission"
    finally:
        reg.shutdown()


def test_admission_under_threshold_records_would_refuse_false(tmp_path, monkeypatch,
                                                             mock_endpoint):
    reg = _omlx_registry(tmp_path, monkeypatch, mock_endpoint,
                         {"max_swap_mb": 9000, "enforce": True})
    try:
        agent = reg.create_agent("t", tmp_path, provider="omlx", fallback="none")
        run = _wait(agent.delegate("do it", "true"))
        assert run.state == COMPLETED
        assert run.hops[0]["would_refuse"] is False and run.hops[0]["admit_reason"] is None
    finally:
        reg.shutdown()


def test_local_run_writes_a_run_summary(tmp_path, monkeypatch, mock_endpoint, trace_records):
    reg = _omlx_registry(tmp_path, monkeypatch, mock_endpoint, {})
    try:
        agent = reg.create_agent("t", tmp_path, provider="omlx", fallback="none")
        _wait(agent.delegate("do it", "true"))
        assert _until(lambda: any(r["kind"] == "run" for r in trace_records))
        summary = next(r for r in trace_records if r["kind"] == "run_summary")
        assert summary["lane"] == "omlx" and summary["samples"] >= 1
        assert summary["peak_swap_mb"] == 5000.0
        assert summary["energy_wh"] is None  # no power draw on this Mac lane
        kinds = [r["kind"] for r in trace_records]
        assert kinds.index("run_summary") < kinds.index("run")
    finally:
        reg.shutdown()


def test_cloud_run_has_no_run_summary(tmp_path, monkeypatch, trace_records):
    settings = make_settings(tmp_path)
    monkeypatch.setattr("subagent.providers.claude._spawn_claude",
                        lambda argv, env, cwd: FakeProcess(_result("done"), argv, env))
    reg = Registry(settings, start_reaper=False)
    try:
        agent = reg.create_agent("t", tmp_path)
        _wait(agent.delegate("do it", "true"))
        assert _until(lambda: any(r["kind"] == "run" for r in trace_records))
        assert not any(r["kind"] == "run_summary" for r in trace_records)
    finally:
        reg.shutdown()

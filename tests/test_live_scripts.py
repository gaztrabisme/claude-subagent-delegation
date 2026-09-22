"""The live scripts' pure parts: smoke_lanes classification, the sampling
proxy, and bench_concurrency aggregation and recommendation — plus fake-server
cases for smoke_guard, hook_isolation_check and reopen_lane's argument
handling and config path.

No network: the one proxy test forwards to a mock upstream on 127.0.0.1.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import threading
import tomllib
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from subagent.lane_state import LaneState
from subagent.runs import Run

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BENCH = ROOT / "bench"  # bench_concurrency lives here since the bench matrix landed


def _load(name: str, path: Path | None = None):
    path = path or (BENCH / f"{name}.py")
    if not path.exists():
        path = SCRIPTS / f"{name}.py"
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(f"sam_script_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look their module up there
    spec.loader.exec_module(mod)
    return mod


smoke = _load("smoke_lanes")
harness = _load("lane_harness")
bench = _load("bench_concurrency", BENCH / "concurrency.py")
guard = _load("smoke_guard")
hcheck = _load("hook_isolation_check")
reopener = _load("reopen_lane")

SSH = "/Users/someone/.ssh/config"
SSH_CONFIG = str(Path.home() / ".ssh" / "config")


# --- smoke_lanes: pass / deferred / fail ----------------------------------------


def _run(lane="omlx", hops=None, state="completed", error=None, total=500) -> dict:
    """A Registry-shaped result: a real Run's detail() plus its ids."""
    run = Run(run_id="run-1", agent_id="a1", prompt="p", lane=lane, state=state, error=error)
    run.hops = hops if hops is not None else [
        {"hop": 0, "lane": lane, "outcome": "ran", "code": None, "message": None}]
    run.usage.output = total // 5
    run.usage.input = total - total // 5
    detail = run.detail()
    detail.update(run_id=run.run_id, agent_id=run.agent_id, lane=run.lane)
    return detail


def _trace(verdict_path=SSH, turns=True, run_tokens=500, summary=True) -> list[dict]:
    records = []
    if verdict_path:
        records.append({"kind": "verdict", "agent_id": "a1", "tool": "Bash", "action": "deny",
                        "tier": "policy", "reason": "refused: credentials",
                        "facts": {"sensitive_path": verdict_path}})
    records.append({"kind": "verdict", "agent_id": "a1", "tool": "Write", "action": "allow",
                    "facts": {}})
    if turns:
        records.append({"kind": "turn", "run_id": "run-1", "input": 0, "output": 40,
                        "cache_write": 4000})
    records.append({"kind": "run", "run_id": "run-1", "usage": {"total": run_tokens}})
    if summary:
        records.append({"kind": "run_summary", "run_id": "run-1", "lane": "omlx"})
    return records


METRICS = [{"kind": "sample", "lane": "omlx", "run_ids": ["run-1"], "snapshot": {}}]
PROXY = [{"method": "POST", "path": "/v1/messages?beta=true", "keys": ["model"],
          "sampling": {}}]


def test_smoke_pass_omlx():
    out = smoke.evaluate("omlx", _run(), _trace(), METRICS, "ok", SSH, PROXY)
    assert out.exit_code == smoke.EXIT_PASS, out.checks
    assert out.headline.startswith("PASS")
    assert any("sampling fields sent: none" in d for _, _, d in out.checks)


def test_smoke_pass_cloud_lane_needs_no_metrics():
    out = smoke.evaluate("glm", _run("glm"), _trace(summary=False), [], "ok\n", SSH)
    assert out.exit_code == smoke.EXIT_PASS, out.checks


@pytest.mark.parametrize("hop,code", [
    ({"outcome": "refused", "code": "zai_1313_exhausted", "message": "[1313][Fair Usage]"},
     "zai_1313_exhausted"),
    ({"outcome": "refused", "code": "deepseek_balance", "message": "Insufficient Balance"},
     "deepseek_balance"),
    ({"outcome": "refused", "code": "codex_usage_limit", "message": "usage limit"},
     "codex_usage_limit"),
    ({"outcome": "health_failed", "code": "health_failed", "message": "bppc: no host"},
     "health_failed"),
    ({"outcome": "skipped_closed", "code": "zai_1308", "message": "closed"}, "zai_1308"),
])
def test_smoke_deferred_on_refusal_before_work(hop, code):
    lane = "glm"
    run = _run(lane, hops=[{"hop": 0, "lane": lane, **hop}], state="failed",
               error="no lane took the run", total=0)
    out = smoke.evaluate(lane, run, [], [], None, SSH)
    assert out.exit_code == smoke.EXIT_DEFERRED
    assert out.headline.startswith(f"DEFERRED: {lane} {code} ")


def test_smoke_deferred_when_ran_but_refused_with_zero_tokens():
    run = _run("glm", state="failed", error="API Error: 429 [1313] fair use", total=0)
    out = smoke.evaluate("glm", run, [], [], None, SSH)
    assert out.exit_code == smoke.EXIT_DEFERRED
    assert "1313" in out.headline


def test_smoke_unavailable_lane_is_a_failure_not_deferred():
    run = _run("deepseek", hops=[{"hop": 0, "lane": "deepseek", "outcome": "unavailable",
                                  "code": None, "message": "missing API key"}],
               state="failed", total=0)
    out = smoke.evaluate("deepseek", run, [], [], None, SSH)
    assert out.exit_code == smoke.EXIT_FAIL


@pytest.mark.parametrize("change,failed_check", [
    ({"trace": _trace(verdict_path=None)}, "verdict deny for ~/.ssh/config"),
    ({"trace": _trace(verdict_path="/Users/someone/.ssh/id_rsa")},
     "verdict deny for ~/.ssh/config"),
    ({"hello": None}, "hello.txt written"),
    ({"hello": "nope"}, "hello.txt written"),
    ({"trace": _trace(turns=False)}, "turn records with tokens"),
    ({"trace": _trace(run_tokens=0)}, "run record with tokens"),
    ({"metrics": []}, "metrics sample rows"),
    ({"trace": _trace(summary=False)}, "run_summary record"),
    ({"proxy": []}, "proxy logged /v1/messages bodies"),
])
def test_smoke_fail_names_the_check(change, failed_check):
    args = {"trace": _trace(), "metrics": METRICS, "hello": "ok", "proxy": PROXY, **change}
    out = smoke.evaluate("omlx", _run(), args["trace"], args["metrics"], args["hello"], SSH,
                         args["proxy"])
    assert out.exit_code == smoke.EXIT_FAIL
    assert [name for name, ok, _ in out.checks if not ok] == [failed_check]


def test_smoke_fail_when_it_ran_on_another_lane():
    run = _run("omlx", hops=[{"hop": 0, "lane": "glm", "outcome": "ran"}])
    run["lane"] = "glm"
    out = smoke.evaluate("omlx", run, _trace(), METRICS, "ok", SSH, PROXY)
    assert out.exit_code == smoke.EXIT_FAIL
    assert not {n: ok for n, ok, _ in out.checks}["ran on lane"]


def test_smoke_verdicts_of_other_agents_do_not_count():
    trace = [dict(r, agent_id="a9") if r["kind"] == "verdict" else r for r in _trace()]
    out = smoke.evaluate("omlx", _run(), trace, METRICS, "ok", SSH, PROXY)
    assert out.exit_code == smoke.EXIT_FAIL


# --- the sampling proxy -------------------------------------------------------------


def test_extract_sampling_top_level_only():
    body = {"model": "m", "temperature": 0.7, "top_k": 20, "stream": True,
            "metadata": {"top_p": 0.9}}
    assert harness.extract_sampling(body) == {"temperature": 0.7, "top_k": 20}
    assert harness.extract_sampling({"model": "m"}) == {}
    assert harness.extract_sampling(["temperature"]) == {}


def test_request_record_keys_and_non_json():
    raw = json.dumps({"model": "m", "messages": [], "top_p": 1}).encode()
    rec = harness.request_record("POST", "/v1/messages", raw)
    assert rec["keys"] == ["messages", "model", "top_p"]
    assert rec["sampling"] == {"top_p": 1}
    bad = harness.request_record("POST", "/x", b"not json")
    assert bad["keys"] is None and bad["sampling"] == {}


def test_sampling_summary():
    assert harness.sampling_summary([{"sampling": {}}, {}]) == "none"
    records = [{"sampling": {"temperature": 1}}, {"sampling": {"temperature": 0.5, "top_k": 20}},
               {"sampling": {"temperature": 1}}]
    assert harness.sampling_summary(records) == "temperature=1/0.5, top_k=20"


def test_proxy_forwards_streams_and_logs_without_headers(tmp_path, mock_endpoint):
    mock_endpoint.routes["/up/v1/messages"] = (200, {"ok": True})
    log = tmp_path / "log.jsonl"
    proxy = harness.SamplingProxy(mock_endpoint.url("up"), log).start()
    try:
        body = json.dumps({"model": "m", "temperature": 0.2, "messages": []}).encode()
        req = urllib.request.Request(f"{proxy.url}/v1/messages", data=body, method="POST",
                                     headers={"Authorization": "Bearer secret-key-123",
                                              "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=5) as resp:
            assert json.loads(resp.read()) == {"ok": True}
    finally:
        proxy.stop()
    sent = {k.lower(): v for k, v in mock_endpoint.headers["/up/v1/messages"].items()}
    assert sent["authorization"] == "Bearer secret-key-123"
    text = log.read_text()
    assert "secret-key-123" not in text and "uthorization" not in text
    (record,) = harness.read_jsonl(log)
    assert record["sampling"] == {"temperature": 0.2}
    assert record["keys"] == ["messages", "model", "temperature"]


# --- bench aggregation and recommendation ----------------------------------------


def test_bench_percentile_nearest_rank():
    assert bench.percentile([], 90) is None
    assert bench.percentile([5.0], 90) == 5.0
    assert bench.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == 9
    assert bench.percentile([3, 1, 2], 50) == 2


def test_bench_child_stats_from_turns():
    turns = [
        {"output": 100, "ts_first_token": 10.0, "ts_end": 12.0},
        {"output": 50, "ts_first_token": 20.0, "ts_end": 21.0},
        {"output": 7, "ts_first_token": None, "ts_end": 30.0},
    ]
    stats = bench.child_stats(turns)
    assert stats == {"output_tokens": 157, "decode_tps": 50.0, "turns": 3}
    assert bench.child_stats([])["decode_tps"] is None


def _child(ttft, out, verified=True, state="completed", decode=50.0):
    return {"ttft_s": ttft, "output_tokens": out, "verified": verified, "state": state,
            "decode_tps": decode}


def test_bench_level_stats():
    children = [_child(2.0, 100), _child(4.0, 200), _child(9.0, 300, verified=False,
                                                           state="completed_unverified")]
    samples = [
        {"snapshot": {"omlx": {"model_memory_used": 10}, "mac": {"swap_used_mb": 5.0}}},
        {"snapshot": {"omlx": {"model_memory_used": 30}, "mac": {"swap_used_mb": 1.0}}},
        {"snapshot": {"omlx": {"error": "x"}}},
    ]
    row = bench.level_stats(3, children, 10.0, samples)
    assert row["aggregate_tps"] == 60.0
    assert row["ttft_median_s"] == 4.0 and row["ttft_p90_s"] == 9.0
    assert (row["completed"], row["verified"], row["failures"]) == (3, 2, 1)
    assert row["peak_model_memory_used"] == 30 and row["peak_swap_mb"] == 5.0
    assert set(bench.LEVEL_COLUMNS) <= set(row)


def _level(n, tps, p90=5.0, swap=0.0, failures=0):
    return {"level": n, "children": n, "aggregate_tps": tps, "ttft_p90_s": p90,
            "peak_swap_mb": swap, "failures": failures}


def test_bench_recommend_largest_passing_level():
    rows = [_level(1, 50), _level(2, 90), _level(4, 150), _level(8, 170)]
    chosen, line = bench.recommend(rows)
    assert chosen == 4
    assert line.startswith("recommend max_agents=4") and "level 8 broke speedup" in line


def test_bench_recommend_all_pass_takes_the_top():
    rows = [_level(1, 50), _level(2, 100), _level(4, 200), _level(8, 400)]
    chosen, line = bench.recommend(rows)
    assert chosen == 8 and "largest level tested" in line


def test_bench_recommend_ttft_rule():
    rows = [_level(1, 50), _level(2, 100), _level(4, 200, p90=75.0), _level(8, 400, p90=90)]
    chosen, line = bench.recommend(rows)
    assert chosen == 2 and "level 4 broke ttft" in line


def test_bench_recommend_swap_rule():
    rows = [_level(1, 50, swap=100.0), _level(2, 100, swap=300.0),
            _level(4, 200, swap=500.0), _level(8, 400, swap=900.0)]
    chosen, line = bench.recommend(rows)
    assert chosen == 4 and "level 8 broke swap" in line


def test_bench_recommend_failures_rule_and_unmeasured_swap():
    rows = [_level(1, 50), _level(2, 100, failures=1), _level(4, 300, swap=None)]
    chosen, line = bench.recommend(rows)
    assert chosen == 1
    assert "no level above 1 passed" in line and "level 2 broke failures" in line


def test_bench_recommend_is_judged_against_the_next_lower_level():
    # 4 is only 1.2x of 2, but 8 is 1.5x of 4: 8 still qualifies.
    rows = [_level(1, 50), _level(2, 100), _level(4, 120), _level(8, 180)]
    chosen, _ = bench.recommend(rows)
    assert chosen == 8


def test_bench_recommend_edge_cases():
    assert bench.recommend([])[0] is None
    chosen, line = bench.recommend([_level(1, 50)])
    assert chosen == 1 and "only one level" in line


def test_bench_swap_growth_over_level_one():
    rows = bench.with_swap_growth([_level(1, 1, swap=100.0), _level(2, 1, swap=650.0),
                                   _level(4, 1, swap=None)])
    assert [r["swap_growth_mb"] for r in rows] == [0.0, 550.0, None]


def test_bench_module_source_has_three_functions():
    import ast

    source = bench.module_source()
    assert 180 <= len(source.splitlines()) <= 220
    names = [n.name for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)]
    assert names == list(bench.FUNCTIONS)
    assert bench.FUNCTIONS[1] in bench.VERIFICATION


def test_bench_main_writes_one_row_per_level(tmp_path, monkeypatch):
    """main() end to end with a fake server: CSVs and the recommendation line."""
    import csv
    import threading
    import time
    from types import SimpleNamespace

    class FakeServer:
        def __init__(self, session_root, config=None, *, overrides=None):
            # The caps arrive as config overrides, not environment.
            assert overrides["providers"]["omlx"]["max_agents"] == 8
            assert overrides["core"]["max_agents"] == 8
            self.session_root = session_root
            session_root.mkdir(parents=True, exist_ok=True)
            self.metrics_path = session_root / "metrics.jsonl"
            self.trace_path = session_root / "trace.jsonl"
            self.settings = SimpleNamespace(
                providers={"omlx": SimpleNamespace(name="omlx", model="m", max_agents=8)},
                default_provider="omlx")
            self.count = 0
            self.samples: list[str] = []

        def start(self):
            return self

        def delegate(self, *, provider, task, verification, workspace, fallback, name):
            assert fallback == "none" and (workspace / "module.py").exists()
            self.count += 1
            now = time.time()
            done = threading.Event()
            done.set()
            run_id = f"run-{self.count}"
            self.samples.append(json.dumps({"kind": "sample", "lane": provider,
                                            "run_ids": [run_id],
                                            "snapshot": {"omlx": {"model_memory_used": 1},
                                                         "mac": {"swap_used_mb": 0.0}}}))
            self.metrics_path.write_text("\n".join(self.samples) + "\n")
            return SimpleNamespace(
                run_id=run_id, agent_id=f"a{self.count}", state="completed", done=done,
                created_at=now - 2, finished_at=now, ttft_seconds=0.5, error=None,
                verification_result=SimpleNamespace(passed=True),
                turn_log=[{"output": 60, "ts_first_token": now - 1.5, "ts_end": now - 0.5}])

        def close_agent(self, agent_id):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(bench.lane_harness, "InProcessServer", FakeServer)
    assert bench.main(["--provider", "omlx", "--levels", "1,2,4,8", "--out", str(tmp_path)]) == 0
    with open(tmp_path / "bench.csv") as handle:
        rows = list(csv.DictReader(handle))
    assert [r["level"] for r in rows] == ["1", "2", "4", "8"]
    assert all(r["failures"] == "0" and r["peak_swap_mb"] == "0.0" for r in rows)
    with open(tmp_path / "bench_children.csv") as handle:
        assert len(list(csv.DictReader(handle))) == 15
    assert json.loads((tmp_path / "recommendation.json").read_text())["recommended"] in (
        1, 2, 4, 8)


def test_bench_main_takes_the_default_provider_from_the_config(tmp_path, monkeypatch):
    """With no --provider, the config's default provider gets the overrides."""
    import threading
    import time
    from types import SimpleNamespace

    seen: dict[str, Any] = {}

    class FakeServer:
        def __init__(self, session_root, config=None, *, overrides=None):
            seen["overrides"] = overrides
            self.session_root = session_root
            session_root.mkdir(parents=True, exist_ok=True)
            self.metrics_path = session_root / "metrics.jsonl"
            self.trace_path = session_root / "trace.jsonl"
            self.settings = SimpleNamespace(
                providers={"llama": SimpleNamespace(name="llama", model="m", max_agents=1)},
                default_provider="llama")

        def start(self):
            return self

        def delegate(self, *, provider, task, verification, workspace, fallback, name):
            now = time.time()
            done = threading.Event()
            done.set()
            return SimpleNamespace(
                run_id="run-1", agent_id="a1", state="completed", done=done,
                created_at=now - 2, finished_at=now, ttft_seconds=0.5, error=None,
                verification_result=SimpleNamespace(passed=True),
                turn_log=[{"output": 60, "ts_first_token": now - 1.5, "ts_end": now - 0.5}])

        def close_agent(self, agent_id):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(bench.lane_harness, "config_table",
                        lambda config: {"core": {"default_provider": "llama"}})
    monkeypatch.setattr(bench.lane_harness, "InProcessServer", FakeServer)
    assert bench.main(["--levels", "1", "--out", str(tmp_path)]) == 0
    assert seen["overrides"]["core"]["max_agents"] == 1
    assert seen["overrides"]["providers"]["llama"]["max_agents"] == 1


# --- the config-driven server -------------------------------------------------------


def test_toml_dump_writes_sections_and_drops_nones():
    text = harness.toml_dump({
        "core": {"max_agents": 8, "sample_seconds": 5.0, "trace": None},
        "providers": {"omlx": {"driver": "claude", "local": True,
                               "api_key_env": ["OMLX_API_KEY"],
                               "health": {"kind": "omlx"}}},
    })
    parsed = tomllib.loads(text)
    assert parsed == {"core": {"max_agents": 8, "sample_seconds": 5.0},
                      "providers": {"omlx": {"driver": "claude", "local": True,
                                             "api_key_env": ["OMLX_API_KEY"],
                                             "health": {"kind": "omlx"}}}}
    assert "trace" not in parsed["core"]


def test_provider_config_declares_exactly_the_one_provider(tmp_path):
    path = harness.write_provider_config("omlx", directory=tmp_path,
                                         core={"max_steps": 8, "run_timeout": 600.0})
    assert path.is_file()
    assert set(tomllib.loads(path.read_text())["providers"]) == {"omlx"}
    settings = harness.load_settings(path)
    assert settings.default_provider == "omlx"
    cfg = settings.providers["omlx"]
    assert cfg.driver == "claude" and cfg.local is True
    assert cfg.base_url == "http://127.0.0.1:8000/v1"
    assert "OMLX_API_KEY" in cfg.api_key_envs
    assert (cfg.max_steps, cfg.run_timeout) == (8, 600.0)


def test_provider_config_takes_the_given_values_over_the_builtins(tmp_path):
    path = harness.write_provider_config("omlx", directory=tmp_path,
                                         base_url="http://127.0.0.1:9000/v1")
    cfg = harness.load_settings(path).providers["omlx"]
    assert cfg.base_url == "http://127.0.0.1:9000/v1"


def test_an_unknown_provider_needs_a_config():
    with pytest.raises(SystemExit):
        harness.write_provider_config("bppc", directory=Path(tempfile.gettempdir()))


def test_load_settings_layers_overrides_over_the_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[core]\nmax_agents = 2\nmax_steps = 5\n'
                      '[providers.p]\ndriver = "claude"\nmax_agents = 2\n')
    settings = harness.load_settings(config, overrides={
        "core": {"max_agents": 8}, "providers": {"p": {"max_agents": 8}}})
    assert settings.max_agents == 8
    assert settings.providers["p"].max_agents == 8
    assert settings.providers["p"].max_steps == 5  # the config's own value stands


# --- smoke_guard: one delegation, one deny verdict ----------------------------------


def _fake_guard_server(seen: dict[str, Any], verdict: dict[str, Any] | None):
    """An InProcessServer stand-in that writes one verdict into its trace."""

    class FakeGuardServer:
        def __init__(self, session_root, config=None, *, overrides=None):
            seen["config"], seen["overrides"] = config, overrides
            self.session_root = session_root
            session_root.mkdir(parents=True, exist_ok=True)
            self.trace_path = session_root / "trace.jsonl"
            self.settings = SimpleNamespace(providers={"glm": SimpleNamespace(
                name="glm", driver="claude", model="glm-5.3-flash[1m]",
                base_url="https://api.z.ai/api/anthropic")})

        def start(self):
            return self

        def delegate(self, *, provider, task, verification, workspace, fallback, name):
            seen["provider"], seen["task"] = provider, task
            assert verification == "true" and fallback == "none" and name == "smoke-guard"
            if verdict is not None:
                self.trace_path.write_text(json.dumps(verdict) + "\n")
            done = threading.Event()
            done.set()
            return SimpleNamespace(
                run_id="run-1", agent_id="a1", state="completed", done=done,
                detail=lambda: {"state": "completed", "finish_reason": "end_turn",
                                "error": None})

        def close_agent(self, agent_id):
            pass

        def stop(self):
            pass

    return FakeGuardServer


DENY_SSH = {"kind": "verdict", "agent_id": "a1", "tool": "Bash", "action": "deny",
            "tier": "policy", "reason": "refused: credentials",
            "facts": {"sensitive_path": SSH_CONFIG}}


@pytest.mark.parametrize("verdict,expected", [
    (DENY_SSH, guard.EXIT_PASS),
    (dict(DENY_SSH, facts={"sensitive_path": "/Users/someone/.ssh/id_rsa"}),
     guard.EXIT_FAIL),
    (dict(DENY_SSH, action="allow"), guard.EXIT_FAIL),
    (None, guard.EXIT_NO_ATTEMPT),
])
def test_smoke_guard_judges_the_trace(tmp_path, monkeypatch, verdict, expected):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(guard.lane_harness, "InProcessServer",
                        _fake_guard_server(seen, verdict))
    assert guard.main(["--provider", "glm"]) == expected
    assert seen["provider"] == "glm"
    assert "cat ~/.ssh/config" in seen["task"]


def test_smoke_guard_writes_a_provider_toml_for_a_built_in(tmp_path, monkeypatch):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(guard.lane_harness, "InProcessServer",
                        _fake_guard_server(seen, None))
    assert guard.main(["--provider", "glm"]) == guard.EXIT_NO_ATTEMPT
    assert seen["overrides"] is None and seen["config"].name == "provider.toml"
    parsed = tomllib.loads(seen["config"].read_text())
    assert parsed["providers"]["glm"]["driver"] == "claude"
    assert parsed["core"]["default_provider"] == "glm"
    # The run-time knobs are config, never environment.
    assert (parsed["core"]["max_steps"], parsed["core"]["rate_limit_retries"]) == (6, 1)
    assert parsed["core"]["run_timeout"] == 420.0 and "workspace" in parsed["core"]


def test_smoke_guard_takes_a_config_naming_the_provider(tmp_path, monkeypatch):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(guard.lane_harness, "InProcessServer",
                        _fake_guard_server(seen, None))
    config = tmp_path / "my.toml"
    config.write_text('[core]\ndefault_provider = "glm"\n'
                      '[providers.glm]\ndriver = "claude"\n')
    assert guard.main(["--config", str(config), "--timeout", "60"]) == guard.EXIT_NO_ATTEMPT
    assert seen["config"] == config
    assert (seen["overrides"]["core"]["max_steps"],
            seen["overrides"]["core"]["run_timeout"]) == (6, 60.0)


def test_smoke_guard_lane_alias_and_default_provider(tmp_path, monkeypatch):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(guard.lane_harness, "InProcessServer",
                        _fake_guard_server(seen, None))

    def settings_with(provider: str):
        return lambda config=None, overrides=None: SimpleNamespace(default_provider=provider,
                                                                   session_root=tmp_path)

    monkeypatch.setattr(guard.lane_harness, "load_settings", settings_with(""))
    with pytest.raises(SystemExit):
        guard.main([])
    monkeypatch.setattr(guard.lane_harness, "load_settings", settings_with("glm"))
    assert guard.main(["--lane", "glm"]) == guard.EXIT_NO_ATTEMPT
    assert seen["provider"] == "glm"
    assert guard.main([]) == guard.EXIT_NO_ATTEMPT  # the config's default provider runs
    assert seen["provider"] == "glm"


# --- hook_isolation_check: the claude driver's guard, with a fake claude ------------


def _fake_claude():
    """A `claude` that honours the hook protocol the real one is subject to.

    It fires the PreToolUse hook through the real fake supervisor when the
    --settings file carries one and the setting sources do not switch it off,
    and only then does it leave `x` unwritten.
    """
    def run(argv, env, workspace):
        payload = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        planted = workspace / ".claude" / "settings.json"
        sources = argv[argv.index("--setting-sources") + 1]
        loaded = "project" in sources and planted.exists() and json.loads(
            planted.read_text()).get("disableAllHooks")
        if payload.get("hooks") and not loaded:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.connect(env["SUBAGENT_APPROVAL_SOCKET"])
                conn.sendall(json.dumps(
                    {"tool_input": {"command": hcheck.COMMAND}}).encode() + b"\n")
                reply = json.loads(conn.recv(65536).split(b"\n", 1)[0])
            assert reply["action"] == "deny"
        else:
            (workspace / "x").write_text("hi")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    return run


def test_hook_isolation_check_holds_with_a_fake_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "mock-key")
    monkeypatch.setattr(hcheck, "run_claude", _fake_claude())
    assert hcheck.main(["--provider", "glm"]) == 0


def test_hook_isolation_check_fails_when_the_hook_misses(tmp_path, monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "mock-key")

    def unguarded(argv, env, workspace):
        (workspace / "x").write_text("hi")  # even the hook case writes: no guard
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(hcheck, "run_claude", unguarded)
    assert hcheck.main(["--lane", "glm"]) == 1


def test_hook_isolation_check_points_the_provider_at_the_mock(tmp_path, monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "mock-key")
    seen: dict[str, Any] = {}
    real_load = hcheck.lane_harness.load_settings

    def record(config=None, overrides=None):
        seen["config"], seen["overrides"] = config, overrides
        return real_load(config, overrides)

    monkeypatch.setattr(hcheck.lane_harness, "load_settings", record)
    monkeypatch.setattr(hcheck, "run_claude", _fake_claude())
    assert hcheck.main(["--provider", "glm"]) == 0
    parsed = tomllib.loads(Path(seen["config"]).read_text())
    assert parsed["core"]["default_provider"] == "glm"
    assert parsed["providers"]["glm"]["base_url"].startswith("http://127.0.0.1:")
    assert seen["overrides"]["guard"]["approval_socket"].endswith(".sock")
    assert seen["overrides"]["core"]["max_steps"] == 3
    assert seen["overrides"]["core"]["session_root"].endswith("sessions")


def test_hook_isolation_check_takes_a_config_naming_the_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "mock-key")
    seen: dict[str, Any] = {}
    real_load = hcheck.lane_harness.load_settings

    def record(config=None, overrides=None):
        seen["config"], seen["overrides"] = config, overrides
        return real_load(config, overrides)

    monkeypatch.setattr(hcheck.lane_harness, "load_settings", record)
    monkeypatch.setattr(hcheck, "run_claude", _fake_claude())
    config = tmp_path / "my.toml"
    config.write_text('[core]\ndefault_provider = "glm"\n'
                      '[providers.glm]\ndriver = "claude"\nmodel = "mock-model"\n')
    assert hcheck.main(["--config", str(config)]) == 0
    assert seen["config"] == config
    assert seen["overrides"]["providers"]["glm"]["base_url"].startswith("http://127.0.0.1:")


# --- reopen_lane: the session root comes from the settings --------------------------


def _closed(root: Path, name: str = "glm", code: str = "zai_1308"):
    LaneState(root).close(name, datetime.now().astimezone() + timedelta(hours=1), code,
                          "fair use")
    return LaneState(root)


def test_reopen_lane_lists_then_reopens(tmp_path, capsys):
    root = tmp_path / "sessions"
    _closed(root)
    assert reopener.main(["--session-root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "glm: closed until" in out and "(zai_1308)" in out
    assert reopener.main(["--session-root", str(root), "glm"]) == 0
    assert "glm: reopened" in capsys.readouterr().out
    assert reopener.main(["--session-root", str(root)]) == 0
    assert "no lane closed" in capsys.readouterr().out


def test_reopen_lane_takes_the_session_root_from_the_config(tmp_path, capsys):
    root = tmp_path / "sessions"
    config = tmp_path / "subagent.toml"
    config.write_text(f"[core]\nsession_root = {json.dumps(str(root))}\n")
    _closed(root, code="deepseek_balance")
    assert reopener.main(["--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "glm: closed until" in out and "(deepseek_balance)" in out
    assert reopener.main(["--config", str(config), "glm", "omlx"]) == 0
    out = capsys.readouterr().out
    assert "glm: reopened" in out and "omlx: was not closed" in out


def test_reopen_lane_reads_the_settings_when_no_root_is_given(tmp_path, monkeypatch, capsys):
    root = tmp_path / "settings-sessions"
    _closed(root)
    monkeypatch.setattr(
        reopener.lane_harness, "load_settings",
        lambda config=None, overrides=None: SimpleNamespace(session_root=root))
    assert reopener.main([]) == 0
    assert "glm: closed until" in capsys.readouterr().out
    assert reopener.main(["omlx"]) == 0
    assert "omlx: was not closed" in capsys.readouterr().out


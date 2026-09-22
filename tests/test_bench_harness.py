"""The bench harness: per-CLI parsers, the matrix and the per-cell config.

The parser fixtures in tests/fixtures/bench/ are hand-written from the
documented headless shapes (the claude one is a real run's shape with a
warning line before the JSON), so no test talks to an orchestrator CLI.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "bench"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


harness = _load("sam_bench_harness", ROOT / "bench" / "harness.py")
run = _load("sam_bench_run", ROOT / "bench" / "run.py")
concurrency = _load("sam_bench_concurrency", ROOT / "bench" / "concurrency.py")


def _parsed(name: str) -> dict:
    return harness.get(name).parse((FIXTURES / f"{name}.txt").read_text(), "")


# --- the parsers -----------------------------------------------------------------


def test_claude_prefers_the_modelusage_sums():
    parsed = _parsed("claude")
    assert parsed["ok"] is True
    assert parsed["cost_usd"] == pytest.approx(0.432)
    # modelUsage covers the subagents; the result's top-level usage does not.
    assert parsed["usage"] == {"input": 750, "output": 460, "cache_read": 2800, "cache_write": 200}
    assert parsed["turns"] == 6
    assert parsed["session_id"].startswith("b6d2")
    assert parsed["models"] == ["claude-haiku-4.5", "claude-opus-5"]


def test_codex_takes_the_last_completed_turn_and_uncaches_input():
    parsed = _parsed("codex")
    assert parsed["ok"] is True
    assert parsed["cost_usd"] is None  # codex reports no USD
    assert parsed["usage"] == {"input": 400, "output": 80, "cache_read": 3000, "cache_write": 0}
    assert parsed["turns"] == 2
    assert parsed["session_id"] == "0196ab3d-6f2e-7a31-b0cf-5a4f10d2e9c7"
    assert parsed["models"] == ["gpt-5.6"]


def test_gemini_sums_the_per_model_stats():
    parsed = _parsed("gemini")
    assert parsed["ok"] is True
    # prompt counts the cached reads; output is candidates + thoughts.
    assert parsed["usage"] == {"input": 3000, "output": 800, "cache_read": 2000, "cache_write": 0}
    assert parsed["models"] == ["gemini-3-pro"]
    assert parsed["session_id"] == "sess-9e0f1a2b"
    assert parsed["turns"] == 4


def test_grok_reads_usage_and_cost_defensively():
    parsed = _parsed("grok")
    assert parsed["ok"] is True
    assert parsed["cost_usd"] == pytest.approx(0.07)
    assert parsed["usage"] == {"input": 4000, "output": 350, "cache_read": 1500, "cache_write": 0}
    assert parsed["session_id"] == "grok-77c1"
    assert parsed["models"] == ["grok-5"]


def test_copilot_reads_the_last_usage_checkpoint_in_ai_units():
    parsed = _parsed("copilot")
    assert parsed["ok"] is True
    assert parsed["credits"] == pytest.approx(2.5)
    assert parsed["cost_usd"] is None
    assert parsed["usage"] == {"input": 120, "output": 30, "cache_read": 20, "cache_write": 0}
    assert parsed["turns"] == 1


@pytest.mark.parametrize("name", sorted(harness.HARNESSES))
def test_a_parse_of_garbage_is_a_failed_cell_not_a_crash(name):
    parsed = harness.get(name).parse("no json on any line\n", "boom on stderr")
    assert parsed["ok"] is False
    assert parsed["usage"] == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    assert parsed["cost_usd"] is None
    assert parsed["raw"] == "no json on any line\n"


# --- the prompts -----------------------------------------------------------------


@pytest.mark.parametrize("mode", ["alone", "delegate", "force"])
def test_every_harness_has_a_prompt_for_each_mode(mode):
    for name in harness.HARNESSES:
        assert mode in harness.prompts(name), name
    assert harness.prompts("claude")["delegate"].startswith("/delegate")
    # The other orchestrators have no /delegate command: name the skill instead.
    for name in ("codex", "gemini", "grok", "copilot"):
        assert "subagent run" in harness.prompts(name)["delegate"], name
        assert "skills/delegate/SKILL.md" in harness.prompts(name)["force"], name


# --- the matrix ------------------------------------------------------------------


def test_the_matrix_expands_to_the_expected_cells():
    glm = Path("examples/config.glm.toml")
    assert run.expand_cells(["claude"], [glm], ["cron"], ["alone"], 1) == [
        ("claude", glm, "cron", "alone", 1)]
    cells = run.expand_cells(["claude", "codex"], [glm, Path("examples/config.ds.toml")],
                             ["cron", "spreadsheet"], ["alone", "force"], 2)
    assert len(cells) == 2 * 2 * 2 * 2 * 2
    assert cells[1] == ("claude", glm, "cron", "alone", 2)
    assert cells[-1] == ("codex", Path("examples/config.ds.toml"), "spreadsheet", "force", 2)


def test_a_cell_id_names_harness_config_task_mode_and_run():
    assert run.cell_id(("claude", Path("examples/config.glm.toml"), "cron", "alone", 2)) \
        == "claude-glm-cron-alone-2"


def test_the_dry_run_prints_the_one_cell_it_would_run(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run.py", "--harness", "claude",
                                      "--config", "examples/config.glm.toml", "--tasks", "cron",
                                      "--modes", "alone", "--runs", "1", "--dry-run"])
    assert run.main() == 0
    out = capsys.readouterr().out
    assert out.count("claude-glm-cron-alone-1") == 1
    assert "--output-format json" in out
    assert "--permission-mode acceptEdits" in out
    # Nothing ran: no results directory was created.
    assert "results.json" not in out


def test_an_unknown_harness_or_config_is_rejected(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run.py", "--harness", "cursor", "--dry-run"])
    with pytest.raises(SystemExit):
        run.main()
    monkeypatch.setattr(sys, "argv", ["run.py", "--config", "examples/nope.toml", "--dry-run"])
    with pytest.raises(SystemExit):
        run.main()


# --- the per-cell config ---------------------------------------------------------


def test_the_cell_config_overrides_session_root(tmp_path, monkeypatch):
    source = ROOT / "examples" / "config.glm.toml"
    path = run.write_cell_config(source, tmp_path, tmp_path / "sessions")
    data = tomllib.loads(path.read_text())
    original = tomllib.loads(source.read_text())
    original["core"]["session_root"] = str(tmp_path / "sessions")
    assert data == original  # the file, plus the override and nothing else

    # The server reads the cell's copy as the top config layer.
    monkeypatch.setenv("SUBAGENT_CONFIG", str(path))
    from subagent import config as subconfig
    assert subconfig.load().session_root == (tmp_path / "sessions").resolve()


def test_toml_dump_round_trips_the_shapes_a_bench_config_has():
    data = {
        "core": {"default_provider": "glm", "max_agents": 8, "rate_limit_retries": 3,
                 "trace": None},
        "providers": {"glm": {"driver": "claude", "local": False,
                              "api_key_env": ["GLM_API_KEY", "ZAI_API_KEY"],
                              "pricing": {"kind": "flat_plan", "monthly_usd": 80}}},
    }
    parsed = tomllib.loads(run.toml_dump(data))
    # TOML has no null: a None key is dropped rather than fatal.
    data["core"].pop("trace")
    assert parsed == data
    assert "trace" not in parsed["core"]


# --- the worker side -------------------------------------------------------------


def test_delegation_fields_without_records_are_null_columns():
    assert run.delegation_fields([]) == dict.fromkeys(run.NULL_COLUMNS)


def test_delegation_fields_sum_the_worker_side():
    records = [
        {"rounds": [{"provider": "glm", "status": "completed"}],
         "usage_total": {"input": 1000, "output": 200, "cache_read": 300},
         "cost_total": {"provider_usd": 0.1, "counterfactual_usd": 0.2},
         "credits_total": 12.5, "reviews": [{"status": "ok"}], "verified_pass": True},
        {"rounds": [{"provider": "glm"}, {"provider": "deepseek"}],
         "usage_total": {"input_tokens": 500, "output_tokens": 50},
         "credits_total": 3.0, "reviews": []},
    ]
    fields = run.delegation_fields(records)
    assert fields["worker_provider"] == "deepseek"  # the worker that ran last
    assert fields["worker_tokens_in"] == 1500
    assert fields["worker_tokens_out"] == 250
    assert fields["worker_tokens_cache"] == 300
    assert fields["worker_credits"] == pytest.approx(15.5)
    assert fields["worker_usd"] == pytest.approx(0.1)
    assert fields["counterfactual_usd"] == pytest.approx(0.2)
    assert fields["rounds"] == 3
    assert fields["reviews"] == 1
    assert fields["verified_pass"] is True


def test_delegation_fields_tolerate_records_missing_the_fields():
    fields = run.delegation_fields([{}, {"rounds": None, "usage_total": None}])
    assert fields["worker_provider"] is None
    assert fields["worker_tokens_in"] is None
    assert fields["rounds"] is None
    assert fields["verified_pass"] is None


def test_read_delegations_takes_only_delegation_records(tmp_path):
    trace = tmp_path / "sessions" / "trace.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text("\n".join([
        json.dumps({"kind": "run", "run_id": "r1"}),
        "not json at all",
        json.dumps({"kind": "delegation", "rounds": [{"provider": "glm"}]}),
    ]))
    records = run.read_delegations(tmp_path)
    assert len(records) == 1
    assert records[0]["rounds"] == [{"provider": "glm"}]
    assert run.read_delegations(tmp_path / "empty") == []


# --- the dashboard ---------------------------------------------------------------


def test_write_dashboard_calls_subagent_report_over_every_trace(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    traces = [tmp_path / "cell-a" / "trace.jsonl", tmp_path / "cell-b" / "trace.jsonl"]
    assert run.write_dashboard(tmp_path, traces) is True
    cmd = calls[0]
    assert cmd[:4] == [sys.executable, "-m", "subagent.cli", "report"]
    assert cmd[4:-4] == ["--trace", str(traces[0]), "--trace", str(traces[1])]
    assert cmd[-4:] == ["--html", str(tmp_path / "dashboard.html"), "--csv", str(tmp_path)]


def test_a_dashboard_failure_does_not_fail_the_bench_run(tmp_path, monkeypatch, capsys):
    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

    monkeypatch.setattr(run.subprocess, "run", timeout)
    assert run.write_dashboard(tmp_path, []) is False
    monkeypatch.setattr(run.subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="no report"))
    assert run.write_dashboard(tmp_path, []) is False
    out = capsys.readouterr().out
    assert "dashboard: skipped" in out and "dashboard: `subagent report` failed" in out


# --- the concurrency bench, moved from scripts/ ----------------------------------


def test_concurrency_keeps_its_level_columns_and_recommends_a_level():
    assert concurrency.LEVEL_COLUMNS[0] == "level"
    assert "aggregate_tps" in concurrency.LEVEL_COLUMNS and "ttft_p90_s" in concurrency.LEVEL_COLUMNS
    chosen, line = concurrency.recommend([
        {"level": 1, "failures": 0, "aggregate_tps": 10.0, "ttft_p90_s": 2.0,
         "peak_swap_mb": 0.0},
        {"level": 2, "failures": 0, "aggregate_tps": 25.0, "ttft_p90_s": 3.0,
         "peak_swap_mb": 100.0},
    ])
    assert chosen == 2
    assert line.startswith("recommend max_agents=2:")


def test_concurrency_picks_the_provider_from_the_config():
    settings = SimpleNamespace(
        providers={"glm": SimpleNamespace(model="glm-5.3-flash[1m]", max_agents=4)},
        default_provider="glm")
    name, cfg = concurrency._pick_provider(settings, None)
    assert (name, cfg.model) == ("glm", "glm-5.3-flash[1m]")
    name, cfg = concurrency._pick_provider(settings, "glm")
    assert name == "glm" and cfg.max_agents == 4
    with pytest.raises(SystemExit):
        concurrency._pick_provider(settings, "omlx")

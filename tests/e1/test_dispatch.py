"""E1-4: the check inside the dispatch hop loop, driven through Registry with fake spawns."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from subagent.runs import COMPLETED, FAILED

from ._helpers import error_text, kinds, seed_percent, seed_runs, write_plan


def over_codex(settings):
    seed_percent(settings, "codex", 93.0)


def under_codex(settings):
    seed_percent(settings, "codex", 84.9)


def test_refused_before_any_vendor_call(make_env):
    env = make_env(seed=over_codex)
    run = env.run("codex")
    assert run.state == FAILED
    assert env.spawned == []
    hop = run.hops[0]
    assert (hop["lane"], hop["outcome"], hop["code"]) == ("codex", "refused", "envelope")


def test_refusal_message_names_lane_use_limit_and_reason(make_env):
    env = make_env(seed=over_codex)
    run = env.run("codex")
    text = error_text(run) + " " + (run.hops[0].get("message") or "")
    for part in ("codex", "93", "85"):
        assert part in text
    assert "reserve" in text.lower() or "limit" in text.lower()


def test_router_constant():
    from subagent import router

    assert getattr(router, "ENVELOPE", None) == "envelope"


def test_hop_record_in_trace_has_envelope_code(make_env):
    env = make_env(seed=over_codex)
    env.run("codex")
    hops = [r for r in env.trace() if r["kind"] == "hop" and r["lane"] == "codex"]
    assert hops and hops[0]["code"] == "envelope" and hops[0]["outcome"] == "refused"


def test_just_under_the_line_runs_and_writes_no_envelope_record(make_env):
    env = make_env(seed=under_codex)
    run = env.run("codex")
    assert run.state == COMPLETED and len(env.spawned) == 1
    assert env.envelope_records() == []


def test_refused_lane_falls_back_to_the_next_lane(make_env):
    env = make_env(seed=over_codex)
    run = env.run("codex", fallback="full")
    assert run.state == COMPLETED
    assert run.lane == "glm"
    assert [h["outcome"] for h in run.hops[:2]] == ["refused", "ran"]
    assert run.hops[0]["code"] == "envelope"
    assert len(env.spawned) == 1


def test_closed_lane_row_refuses_always(make_env):
    env = make_env()
    run = env.run("glm")
    assert run.state == FAILED and env.spawned == []
    assert run.hops[0]["code"] == "envelope"


def test_weekly_run_cap_refuses_codex(make_env):
    def seed(settings):
        seed_percent(settings, "codex", 10.0)

    env = make_env(seed=seed)
    seed_runs(env.tmp_path, "codex", 4, age=3 * 86400.0)
    run = env.run("codex")
    assert run.state == FAILED and env.spawned == []
    text = error_text(run)
    assert "4" in text and "week" in text.lower()


@pytest.mark.parametrize(("count", "refused"), [(9, False), (10, True)])
def test_gemini_daily_cap_is_shared_across_hydrogen_and_neon(make_env, count, refused):
    env = make_env(("gemini", "gemini-neon"))
    seed_runs(env.tmp_path, "gemini", count - 2)
    seed_runs(env.tmp_path, "gemini-neon", 2)
    run = env.run("gemini-neon")
    assert (run.state == FAILED) is refused
    assert (env.spawned == []) is refused
    if refused:
        assert run.hops[0]["code"] == "envelope"


def test_money_over_the_report_line_warns_and_runs(make_env):
    env = make_env(("deepseek", "glm"))
    seed_runs(env.tmp_path, "deepseek", 1, age=60.0, usd=6.0)
    run = env.run("deepseek")
    assert run.state == COMPLETED and len(env.spawned) == 1
    assert run.warning and "deepseek" in run.warning and "6" in run.warning
    warns = kinds(env.trace(), "envelope_warning")
    assert len(warns) == 1 and warns[0]["lane"] == "deepseek"


def test_money_at_the_limit_refuses(make_env):
    env = make_env(("deepseek", "glm"))
    seed_runs(env.tmp_path, "deepseek", 1, age=60.0, usd=10.0)
    run = env.run("deepseek")
    assert run.state == FAILED and env.spawned == []
    assert "10" in error_text(run)


def test_no_reading_runs_and_records_the_missing_keys(make_env):
    env = make_env()
    seed_runs(env.tmp_path, "glm", 0)
    run = env.run("codex")
    assert run.state == COMPLETED and len(env.spawned) == 1
    (rec,) = kinds(env.trace(), "envelope_no_reading")
    assert rec["lane"] == "codex"
    assert "used_percent" in json.dumps(rec)


def test_row_without_rules_runs_with_no_reading_record(make_env):
    env = make_env(("claude", "glm"))
    run = env.run("claude")
    assert run.state == COMPLETED
    recs = kinds(env.trace(), "envelope_no_reading")
    assert [r["lane"] for r in recs] == ["claude"]


def test_lane_with_no_row_writes_nothing(make_env):
    env = make_env(("zeta", "glm"))
    run = env.run("zeta")
    assert run.state == COMPLETED and len(env.spawned) == 1
    assert env.envelope_records() == []


def test_check_is_off_without_envelope_config(make_env):
    env = make_env(plan=None, seed=over_codex)
    run = env.run("codex")
    assert run.state == COMPLETED and len(env.spawned) == 1
    assert env.envelope_records() == []


def test_existing_behaviour_is_untouched_when_no_envelope_is_configured(make_env):
    env = make_env(plan=None)
    run = env.run("codex")
    assert run.state == COMPLETED and run.warning is None
    assert [h["outcome"] for h in run.hops] == ["ran"]
    assert run.hops[0]["code"] is None
    assert {r["kind"] for r in env.trace()} <= {"run", "hop", "turn", "sample", "run_summary",
                                                "verdict", "calibration", "delegation"}


@pytest.mark.parametrize("plan_kind", ["missing", "garbage"])
def test_bad_plan_file_never_blocks_and_warns_once_per_run(make_env, tmp_path, plan_kind):
    plan = tmp_path / "nope.md"
    if plan_kind == "garbage":
        plan.write_text("not a lane plan at all\n")
    env = make_env(plan=plan, seed=over_codex)
    run = env.run("codex")
    assert run.state == COMPLETED and len(env.spawned) == 1
    assert len(kinds(env.trace(), "envelope_config_warning")) == 1


def test_home_tilde_in_lane_plan_is_expanded(make_env, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    write_plan(home)
    monkeypatch.setenv("HOME", str(home))
    env = make_env(plan="~/lane-plan.md", seed=over_codex)
    run = env.run("codex")
    assert run.state == FAILED and env.spawned == []


def test_check_runs_before_the_telemetry_admission(make_env, monkeypatch):
    env = make_env(seed=over_codex)
    calls: list[str] = []
    monkeypatch.setattr(env.registry.telemetry, "covers", lambda cfg: True)

    def admission(cfg):
        calls.append(cfg.name)
        return SimpleNamespace(snapshot=None, would_refuse=False, refuse=False, reason="")

    monkeypatch.setattr(env.registry.telemetry, "admission", admission)
    env.run("codex")
    assert calls == []


def test_plan_file_is_the_only_input_for_limits(make_env, tmp_path: Path):
    text = (Path(__file__).parent / "fixtures" / "lane-plan-usage-envelopes.md").read_text()
    plan = write_plan(tmp_path, text.replace("15% of the window", "20% of the window"))
    env = make_env(plan=plan, seed=lambda s: seed_percent(s, "codex", 83.0))
    run = env.run("codex")
    assert run.state == FAILED and "80" in error_text(run)

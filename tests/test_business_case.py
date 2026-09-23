"""Closed-form tests for bench/business_case.py.

The module is loaded from bench/ by path (like tests/test_live_scripts.py does
for scripts/), so the tests exercise the exact file the CLI runs.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench"


def _load():
    path = BENCH / "business_case.py"
    spec = importlib.util.spec_from_file_location("sam_business_case", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


bc = _load()


def inputs(**overrides):
    base = {
        "seats": 100,
        "hours_per_day": 8.0,
        "work_days_per_month": 21.0,
        "tokens_in_per_seat_hour": 30_000.0,
        "tokens_out_per_seat_hour": 6_000.0,
        "cache_read_share": 0.8,
        "cloud_price": bc.CloudPrice(5.0, 25.0, 0.5),
        "cloud_seat": None,
        "hardware_capex_usd": 14_750.0,
        "hardware_count": None,
        "power_w_per_unit": 600.0,
        "usd_per_kwh": 0.13,
        "utilisation": 0.6,
        "amortisation_months": 36,
        "ops_hours_per_month": 4.0,
        "ops_usd_per_hour": 100.0,
        "concurrency_per_unit": 5.0,
        "measured_tps": 28.0,
        "parity": 1.0,
        "retry_factor": 1.0,
    }
    base.update(overrides)
    return bc.Inputs(**base)


def test_zero_capex_zero_power_parity_one_reduces_to_token_price():
    # No hardware and parity 1: the only cost left is the cloud token bill, so
    # cloud cost must be the exact per-token arithmetic and local cost must be 0.
    case = inputs(
        seats=1,
        hours_per_day=1.0,
        work_days_per_month=1.0,
        tokens_in_per_seat_hour=1_000_000.0,
        tokens_out_per_seat_hour=200_000.0,
        cache_read_share=0.25,
        cloud_price=bc.CloudPrice(2.0, 10.0, 0.2),
        hardware_capex_usd=0.0,
        hardware_count=0,
        power_w_per_unit=0.0,
        usd_per_kwh=0.0,
        ops_hours_per_month=0.0,
        ops_usd_per_hour=0.0,
        measured_tps=1.0,
        concurrency_per_unit=1.0,
    )
    result = bc.model(case)
    # 750k non-cache in *2 + 200k out *10 + 250k cache *0.2 = 3,550,000 / 1e6
    assert result["cloud"]["monthly_cost_usd"] == pytest.approx(3.55)
    assert result["local"]["monthly_local_cost_usd"] == pytest.approx(0.0)
    assert result["adjusted_local_cost_usd"] == pytest.approx(0.0)
    # A zero-GPU "fleet" cannot serve the demand: no ROI against the cloud bill.
    assert result["undersized_fleet"] is True
    assert result["break_even_month"] is None
    assert result["roi"] == {"12": None, "24": None, "36": None}


def test_a_fleet_below_demand_reports_infeasible_roi_not_positive_roi():
    """`hardware_count` fixed below `units_needed`: the model still prices the
    undersized fleet, but break-even/ROI against the full cloud bill are None."""
    case = inputs(hardware_count=1)
    result = bc.model(case)
    assert result["capacity"]["units_needed"] == 12
    assert result["capacity"]["shortfall_tokens_per_hour"] > 0
    assert result["undersized_fleet"] is True
    assert result["local"]["units"] == 1  # priced what was declared...
    assert result["break_even_month"] is None  # ...but cannot serve the demand
    assert result["roi"] == {"12": None, "24": None, "36": None}

    sized = bc.model(inputs())  # hardware_count None sizes the fleet to demand
    assert sized["undersized_fleet"] is False
    assert sized["roi"]["12"] is not None


def test_markdown_marks_the_undersized_fleet(capsys):
    case = inputs(hardware_count=1)
    text = bc.render_markdown(bc.model(case))
    assert "infeasible" in text
    assert "below the 12 unit(s)" in text


def test_parity_and_retry_scale_adjusted_cost():
    # capex 1000 amortised over 10 months -> monthly local cost 100; no power,
    # no ops. Adjusted cost = local cost * retry_factor / parity.
    case = inputs(
        seats=1,
        hours_per_day=1.0,
        work_days_per_month=1.0,
        tokens_in_per_seat_hour=0.0,
        tokens_out_per_seat_hour=0.0,
        hardware_capex_usd=1_000.0,
        hardware_count=1,
        power_w_per_unit=0.0,
        usd_per_kwh=0.0,
        ops_hours_per_month=0.0,
        ops_usd_per_hour=0.0,
        amortisation_months=10,
        parity=0.8,
        retry_factor=1.5,
    )
    result = bc.model(case)
    assert result["local"]["monthly_local_cost_usd"] == pytest.approx(100.0)
    assert result["adjusted_local_cost_usd"] == pytest.approx(100.0 * 1.5 / 0.8)


def test_capacity_shortfall_by_hand():
    case = inputs(
        seats=10,
        tokens_in_per_seat_hour=15_000.0,
        tokens_out_per_seat_hour=15_000.0,
        concurrency_per_unit=4.0,
        measured_tps=10.0,
        utilisation=1.0,
        hardware_count=1,
    )
    cap = bc.capacity(case)
    # demand 10 * 30k = 300k/hour; unit 4 * 10 * 3600 = 144k/hour.
    assert cap["demand_tokens_per_hour"] == pytest.approx(300_000.0)
    assert cap["capacity_per_unit_per_hour"] == pytest.approx(144_000.0)
    assert cap["units_needed"] == 3  # ceil(300000 / 144000)
    assert cap["installed_capacity_per_hour"] == pytest.approx(144_000.0)
    assert cap["shortfall_tokens_per_hour"] == pytest.approx(156_000.0)


def test_break_even_month_by_hand():
    assert bc.break_even_month(1_200.0, 1_000.0, 600.0) == 3  # 1200 / 400
    assert bc.break_even_month(1_200.0, 1_000.0, 1_000.0) is None  # never
    assert bc.break_even_month(0.0, 1_000.0, 600.0) == 0  # already paid off


def test_cloud_seat_subscription():
    case = inputs(
        seats=2,
        hours_per_day=1.0,
        work_days_per_month=1.0,
        tokens_in_per_seat_hour=500.0,
        tokens_out_per_seat_hour=0.0,
        cloud_price=None,
        cloud_seat=bc.CloudSeat(19.0, allowance_tokens_per_seat=1_000.0, overage_usd_per_mtok=1.0),
    )
    # 2 seats * 500 in = 1000 tokens, allowance 2000 -> no overage.
    assert bc.cloud_monthly_cost(case) == pytest.approx(38.0)
    case2 = inputs(
        seats=2,
        hours_per_day=1.0,
        work_days_per_month=1.0,
        tokens_in_per_seat_hour=2_000.0,
        tokens_out_per_seat_hour=0.0,
        cloud_price=None,
        cloud_seat=bc.CloudSeat(19.0, allowance_tokens_per_seat=1_000.0, overage_usd_per_mtok=1.0),
    )
    # 4000 tokens, allowance 2000 -> 2000 overage * 1 / 1e6 = 0.002.
    assert bc.cloud_monthly_cost(case2) == pytest.approx(38.002)


def test_sensitivity_grid_shape():
    result = bc.model(inputs())
    cells = result["sensitivity"]
    assert len(cells) == 9
    assert {c["parity"] for c in cells} == set(bc.SENSITIVITY_PARITIES)
    assert {c["concurrency_multiplier"] for c in cells} == set(
        bc.SENSITIVITY_CONCURRENCY_MULTIPLIERS
    )
    for cell in cells:
        assert {"parity", "concurrency_multiplier", "units_needed", "break_even_month",
                "roi_12", "roi_24", "roi_36"} <= cell.keys()


def test_cli_json_has_break_even_and_capacity(capsys):
    assert bc.main(["--seats", "100", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "break_even_month" in result
    assert "capacity" in result


def test_from_report_fills_parity_retry_and_demand(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"providers": [
        {
            "provider": "bppc", "tokens_in": 90_000, "tokens_out": 9_000,
            "tokens_cache_read": 10_000, "verified_pass_rate": 0.9,
            "rounds_per_delegation": 1.2, "wall_p50_s": 360.0,
            "provider_usd": 0.0, "counterfactual_usd": 1.0,
        },
        {
            "provider": "claude", "tokens_in": 80_000, "tokens_out": 10_000,
            "tokens_cache_read": 20_000, "verified_pass_rate": 1.0,
            "rounds_per_delegation": 1.0, "wall_p50_s": 300.0,
            "provider_usd": 2.0, "counterfactual_usd": 1.0,
        },
    ]}))
    local, cloud = bc.report_rows_for(bc.read_report(report), "bppc", "claude")
    overrides = bc.report_overrides(local, cloud)
    # local total input (90k + 10k cache) over a 360 s delegation, back to back.
    assert overrides["tokens_in_per_seat_hour"] == pytest.approx(1_000_000.0)
    assert overrides["tokens_out_per_seat_hour"] == pytest.approx(90_000.0)
    assert overrides["parity"] == pytest.approx(0.9)
    assert overrides["retry_factor"] == pytest.approx(1.2)
    # cloud cache share: 20k / (80k + 20k).
    assert overrides["cache_read_share"] == pytest.approx(0.2)


def test_from_report_reads_the_report_field_names(tmp_path, capsys):
    """`subagent report --json` emits `wall_p50` and `verified_rate`; the same
    overrides must be filled as from the documented spellings."""
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"providers": [
        {
            "provider": "bppc", "tokens_in": 90_000, "tokens_out": 9_000,
            "tokens_cache_read": 10_000, "verified_rate": 0.9,
            "rounds_per_delegation": 1.2, "wall_p50": 360.0,
            "provider_usd": 0.0, "counterfactual_usd": 1.0,
        },
        {
            "provider": "claude", "tokens_in": 80_000, "tokens_out": 10_000,
            "tokens_cache_read": 20_000, "verified_rate": 1.0,
            "rounds_per_delegation": 1.0, "wall_p50": 300.0,
            "provider_usd": 2.0, "counterfactual_usd": 1.0,
        },
    ]}))
    local, cloud = bc.report_rows_for(bc.read_report(report), "bppc", "claude")
    overrides = bc.report_overrides(local, cloud)
    assert overrides["tokens_in_per_seat_hour"] == pytest.approx(1_000_000.0)
    assert overrides["tokens_out_per_seat_hour"] == pytest.approx(90_000.0)
    assert overrides["parity"] == pytest.approx(0.9)
    assert overrides["retry_factor"] == pytest.approx(1.2)
    assert overrides["cache_read_share"] == pytest.approx(0.2)

    # A report with none of the measured fields keeps the defaults, loudly.
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps({"providers": [
        {"provider": "bppc"}, {"provider": "claude"},
    ]}))
    local, cloud = bc.report_rows_for(bc.read_report(bare), "bppc", "claude")
    assert bc.report_overrides(local, cloud) == {}
    argv = ["--from-report", str(bare), "--provider", "bppc",
            "--cloud-provider", "claude", "--json"]
    assert bc.main(argv) == 0
    assert "none of the measured fields" in capsys.readouterr().err


def test_pick_concurrency_chooses_largest_under_ttft(tmp_path):
    bench = tmp_path / "bench.csv"
    with open(bench, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["level", "aggregate_tps", "ttft_p90_s"])
        writer.writeheader()
        writer.writerow({"level": "1", "aggregate_tps": "46", "ttft_p90_s": "3.0"})
        writer.writerow({"level": "2", "aggregate_tps": "80", "ttft_p90_s": "70.0"})
        writer.writerow({"level": "4", "aggregate_tps": "120", "ttft_p90_s": "40.0"})
    row = bc.pick_concurrency(bc.read_concurrency(bench))
    assert row["level"] == 4
    overrides = bc.concurrency_overrides(row)
    assert overrides["concurrency_per_unit"] == 4
    assert overrides["measured_tps"] == pytest.approx(30.0)


def test_inputs_reject_two_cloud_models():
    with pytest.raises(ValueError):
        inputs(cloud_price=bc.CloudPrice(1, 1, 1), cloud_seat=bc.CloudSeat(1))
    with pytest.raises(ValueError):
        inputs(cloud_price=None, cloud_seat=None)

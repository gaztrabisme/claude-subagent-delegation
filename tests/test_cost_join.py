"""Tests for subagent.telemetry.cost against synthetic fixtures in tests/fixtures/cost/."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from subagent.telemetry import cost as cost_join

pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow")

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "cost"
# Fixture copy of scripts/pricing.toml with provider prices "null", so tests of
# unknown-cost handling do not break when the real file gains provider prices.
PRICING_UNKNOWN = FIX / "pricing_unknown.toml"



@pytest.fixture(scope="module")
def out(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("cost-out")
    rc = cost_join.main(
        [
            "--traces",
            str(FIX / "glm-subagent" / "trace.jsonl"),
            str(FIX / "subagent-mcp" / "trace.jsonl"),
            str(FIX / "missing" / "trace.jsonl"),
            "--transcripts",
            str(FIX / "projects"),
            "--pricing",
            str(PRICING_UNKNOWN),
            "--out",
            str(out_dir),
        ]
    )
    assert rc == 0
    return out_dir


@pytest.fixture(scope="module")
def runs(out):
    return pd.read_parquet(out / "runs.parquet").set_index("run_id")


@pytest.fixture(scope="module")
def lanes(out):
    return pd.read_parquet(out / "lanes.parquet").set_index("lane")


def test_only_run_records_are_read(runs):
    assert sorted(runs.index) == [
        "run-aaaaaaaaaaaa",
        "run-bbbbbbbbbbbb",
        "run-cccccccccccc",
        "run-dddddddddddd",
    ]
    assert runs.loc["run-aaaaaaaaaaaa", "lane"] == "glm"  # schema 1: lane from file
    assert runs.loc["run-cccccccccccc", "lane"] == "bppc"  # schema 3: lane from record
    assert not runs["duplicate_run_id"].any()


def test_parent_calls_counted_per_tool_use(runs):
    # delegate (line repeated), await, transcript, cancel resolved via agent_id.
    assert runs.loc["run-aaaaaaaaaaaa", "parent_calls"] == 4
    assert runs.loc["run-cccccccccccc", "parent_calls"] == 1
    assert (
        runs.loc["run-aaaaaaaaaaaa", "parent_session_id"] == "11111111-2222-3333-4444-555555555555"
    )
    assert runs.loc["run-aaaaaaaaaaaa", "parent_project"] == "-synthetic-proj"


def test_parent_usd_dedupes_message_id(runs):
    # msg_01 opus: 100*5 + 600*5*1.25 + 400*5*2 + 10000*0.5 + 200*25 = 18250 -> 0.01825
    #   (appears on three lines, counted once)
    # msg_02 sonnet: 50*2 + 20000*0.2 + 100*10 = 5100 -> 0.0051 (two tool_use lines, once)
    # msg_04 opus: 10*5 + 10*25 = 300 -> 0.0003
    # The unrelated Bash message (msg_03) is not counted.
    assert runs.loc["run-aaaaaaaaaaaa", "parent_messages"] == 3
    assert runs.loc["run-aaaaaaaaaaaa", "parent_usd"] == pytest.approx(0.02365, abs=1e-9)


def test_counterfactual_exact_to_the_cent(runs):
    # 10k in *5 + 20k out *25 + 1M read *0.5 + 40k write *6.25 = 1,300,000 / 1e6
    assert round(runs.loc["run-aaaaaaaaaaaa", "counterfactual_usd"], 2) == 1.30
    assert runs.loc["run-aaaaaaaaaaaa", "counterfactual_usd"] == pytest.approx(1.30, abs=1e-9)


def test_counterfactual_override(tmp_path):
    cost_join.main(
        [
            "--traces",
            str(FIX / "glm-subagent" / "trace.jsonl"),
            "--transcripts",
            str(FIX / "projects"),
            "--pricing",
            str(PRICING_UNKNOWN),
            "--out",
            str(tmp_path),
            "--counterfactual",
            "claude-sonnet-5",
        ]
    )
    df = pd.read_parquet(tmp_path / "runs.parquet").set_index("run_id")
    # 10k*2 + 20k*10 + 1M*0.2 + 40k*2.5 = 520,000 / 1e6
    assert df.loc["run-aaaaaaaaaaaa", "counterfactual_usd"] == pytest.approx(0.52, abs=1e-9)


def test_unknown_provider_cost_is_null_not_zero(runs, lanes):
    a = runs.loc["run-aaaaaaaaaaaa"]
    assert pd.isna(a["provider_usd"])
    assert pd.isna(a["net_saving_usd"])
    assert a["net_saving_usd_excl_provider"] == pytest.approx(1.30 - 0.02365, abs=1e-9)
    assert pd.isna(lanes.loc["glm", "provider_usd"])
    assert pd.isna(lanes.loc["glm", "net_saving_usd"])


def test_real_pricing_file_has_known_provider_costs():
    # The unknown-cost tests above rely on the nulled fixture; this guards the
    # real scripts/pricing.toml, where provider prices are filled in.
    pricing = cost_join.load_pricing(ROOT / "scripts" / "pricing.toml", None)
    monthly_usd = pricing.providers["glm"]["monthly_usd"]
    assert isinstance(monthly_usd, (int, float)) and not isinstance(monthly_usd, bool)
    deepseek = pricing.providers["deepseek"]
    for key in ("input", "output", "cache_read"):
        assert isinstance(deepseek[key], (int, float))


def test_local_provider_is_zero(runs):
    c = runs.loc["run-cccccccccccc"]
    assert c["provider_usd"] == 0
    # 100k*5 + 10k*25 = 0.75; parent msg_05 = 0.0003
    assert c["net_saving_usd"] == pytest.approx(0.75 - 0.0003, abs=1e-9)


def test_unmatched_lists_orphans(out):
    with open(out / "unmatched.csv", newline="") as fh:
        ids = {r["run_id"] for r in csv.DictReader(fh)}
    assert ids == {"run-bbbbbbbbbbbb", "run-dddddddddddd"}


def test_refusal_counted(lanes, runs):
    assert lanes.loc["deepseek", "refusals"] == 1
    assert lanes.loc["glm", "refusals"] == 0
    assert runs.loc["run-dddddddddddd", "counterfactual_usd"] == 0


def test_lane_table(lanes):
    glm = lanes.loc["glm"]
    assert glm["runs"] == 2
    assert glm["verified_passed"] == 1
    assert glm["verified_rate"] == pytest.approx(0.5)
    assert glm["joined_fraction"] == pytest.approx(0.5)
    assert glm["child_tokens"] == 1_070_000 + 8_000


def test_duplicate_run_id_flagged_and_disambiguated(tmp_path):
    trace = tmp_path / "glm-subagent" / "trace.jsonl"
    trace.parent.mkdir()
    lines = (FIX / "glm-subagent" / "trace.jsonl").read_text().splitlines()
    # Same run_id from a later server process, ending long after the parent's calls.
    later = lines[0].replace('"ts":1789700600.0', '"ts":1799999999.0').replace('"a1"', '"a9"')
    trace.write_text("\n".join([*lines, later]) + "\n")
    out = tmp_path / "out"
    cost_join.main(
        [
            "--traces",
            str(trace),
            "--transcripts",
            str(FIX / "projects"),
            "--pricing",
            str(PRICING_UNKNOWN),
            "--out",
            str(out),
        ]
    )
    df = pd.read_parquet(out / "runs.parquet")
    dup = df[df["run_id"] == "run-aaaaaaaaaaaa"].sort_values("ts")
    assert len(dup) == 2 and dup["duplicate_run_id"].all()
    assert list(dup["parent_calls"]) == [4, 0]


def test_unknown_parent_model_priced_at_counterfactual_and_flagged():
    pricing = cost_join.load_pricing(PRICING_UNKNOWN, None)
    usd, unknown = cost_join.message_usd(
        pricing, {"model": "<synthetic>", "usage": {"input_tokens": 1000, "output_tokens": 0}}
    )
    assert unknown and usd == pytest.approx(0.005)
    usd, unknown = cost_join.message_usd(
        pricing, {"model": "claude-fable-5-1[1m]", "usage": {"cache_read_input_tokens": 1_000_000}}
    )
    assert not unknown and usd == pytest.approx(0.25)

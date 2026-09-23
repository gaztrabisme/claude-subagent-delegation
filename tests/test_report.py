"""`subagent report`: tables from a fixture trace, and the self-contained HTML."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest

from subagent import report

FIXTURE = Path(__file__).parent / "fixtures" / "report" / "trace.jsonl"

# The fixture's two providers: `glm` is a flat_plan subscription whose monthly
# fee is spread over its runs in the month; `omlx` is local and free.
CONFIG = """\
[pricing]
counterfactual = "claude-opus-5"

[pricing.cache_multipliers]
cache_write_5m = 1.25
cache_write_1h = 2.0
cache_read = 0.1

[pricing.models."claude-opus-5"]
input = 5.0
output = 25.0

[providers.glm]
driver = "claude"
model = "glm-5.3-flash[1m]"
vendor = "zai"

[providers.glm.pricing]
kind = "flat_plan"
monthly_usd = 80

[providers.glm.pricing.api_equivalent]
input = 1.0
output = 5.0
cache_read = 0.1

[providers.omlx]
driver = "claude"
model = "qwen-local"

[providers.omlx.pricing]
kind = "local"
usd = 0
"""


@pytest.fixture
def settings_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `config.load` at this fixture config, away from the user's own."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("SUBAGENT_CONFIG", str(cfg))


def _build() -> report.Report:
    return report.build([FIXTURE], None, None)


def test_provider_table(settings_env: None) -> None:
    rep = _build()
    assert rep.runs == 3
    assert rep.counterfactual == "claude-opus-5"
    by = {p["provider"]: p for p in rep.providers}
    assert list(by) == ["glm", "omlx"]

    glm = by["glm"]
    assert glm["runs"] == 2
    assert glm["verified_passed"] == 1
    assert glm["verified_rate"] == pytest.approx(0.5)
    assert glm["tokens_in"] == 3_000_000
    assert glm["tokens_out"] == 300_000
    assert glm["tokens_cache_read"] == 500_000
    assert glm["tokens_cache_write"] == 0
    assert glm["credits"] == 0
    assert glm["provider_usd"] == pytest.approx(80.0)  # 2 runs, 80/month spread
    assert glm["api_equivalent_usd"] == pytest.approx(4.55)
    assert glm["counterfactual_usd"] == pytest.approx(22.75)
    assert glm["saving_usd"] == pytest.approx(-57.25)
    assert glm["wall_p50"] == pytest.approx(20.0)
    assert glm["wall_p90"] == pytest.approx(28.0)
    assert glm["ttft_p50"] == pytest.approx(0.7)
    assert glm["rounds_per_delegation"] == pytest.approx(1.0)
    assert glm["guard_denials"] == 1

    omlx = by["omlx"]
    assert omlx["runs"] == 1
    assert omlx["verified_rate"] == pytest.approx(1.0)
    assert omlx["provider_usd"] == pytest.approx(0.0)
    assert omlx["api_equivalent_usd"] is None  # local providers have no plan rates
    assert omlx["counterfactual_usd"] == pytest.approx(3.75)
    assert omlx["saving_usd"] == pytest.approx(3.75)
    assert omlx["wall_p50"] == pytest.approx(12.0)
    assert omlx["ttft_p50"] == pytest.approx(0.4)
    assert omlx["guard_denials"] == 0


def test_delegation_table(settings_env: None) -> None:
    rep = _build()
    by = {d["delegation_id"]: d for d in rep.delegations}
    assert list(by) == ["del-1", "del-2", "del-3"]

    d1 = by["del-1"]
    assert d1["bench_run_id"] == "bench-1"
    assert d1["orchestrator"] == {"harness": "cli", "model": "claude-opus-5"}
    assert d1["providers"] == ["glm"]
    assert d1["status"] == "done"
    assert d1["rounds"] == 1
    assert d1["reviews"] == 0
    assert d1["tests"] == 3
    assert d1["tokens"] == 1_100_000
    assert d1["provider_usd"] == pytest.approx(40.0)
    assert d1["counterfactual_usd"] == pytest.approx(7.5)
    assert d1["wall_seconds"] == pytest.approx(10.0)
    assert d1["verified_pass"] is True

    d2 = by["del-2"]
    assert d2["status"] == "failed"
    assert d2["verified_pass"] is False
    assert d2["tests"] == 2
    assert d2["tokens"] == 2_700_000

    d3 = by["del-3"]
    assert d3["providers"] == ["omlx"]
    assert d3["provider_usd"] == pytest.approx(0.0)
    assert d3["tests"] == 1
    assert d3["tokens"] == 550_000


def test_since_and_bench_run_filters(settings_env: None) -> None:
    cutoff = dt.datetime.fromtimestamp(1789700150.0, dt.UTC)
    assert report.build([FIXTURE], cutoff, None).runs == 2  # run-1 is earlier

    empty = report.build([FIXTURE], None, "bench-2")
    assert empty.runs == 0
    assert empty.delegations == []

    bench = report.build([FIXTURE], None, "bench-1")
    assert bench.runs == 3
    assert len(bench.delegations) == 3


def test_run_only_trace_is_priced_from_the_config(settings_env: None, tmp_path: Path) -> None:
    """A trace of bare `run` records (no `cost`, no `delegation`) still builds:
    the config's PricingSpec prices them even without a run-end `cost` dict."""
    run = {
        "schema": 3, "ts": 1789700100.0, "kind": "run",
        "run_id": "run-x", "agent_id": "a1", "session_id": "s1", "model": "m",
        "lane": "glm", "provider": "glm", "driver": "claude", "workspace": "/w",
        "guard": "policy", "state": "completed", "finish_reason": "completed",
        "elapsed_seconds": 1.0, "wall_seconds": 2.0, "ttft_seconds": 0.1,
        "turns": 1, "tool_calls": 1, "continues": 0, "end_state": "done",
        "verification_passed": True,
        "guard_verdicts": {"allow": 1, "deny": 0, "escalate": 0},
        "refusal_code": None,
        "usage": {"input": 1000, "output": 100, "cache_read": 0, "cache_write": 0,
                  "reasoning": 0, "total": 1100, "steps": 1, "turns": 1},
        "prompt_chars": 10, "result_chars": 20, "distilled": False, "truncated": False,
        "verification": {"command": "true", "passed": True, "exit_code": 0},
        "error": None,
    }
    trace = tmp_path / "bare.jsonl"
    trace.write_text(json.dumps(run) + "\n", encoding="utf-8")

    rep = report.build([trace], None, None)
    assert rep.runs == 1
    p = rep.providers[0]
    assert p["provider"] == "glm"
    assert p["vendor"] == "zai"  # shown as a column, not used as the key
    assert p["runs"] == 1
    assert p["tokens_in"] == 1000
    assert p["provider_usd"] == pytest.approx(80.0)  # 1 run, 80/month spread
    assert p["counterfactual_usd"] is None  # no `cost` dict, no counterfactual rates here
    assert p["api_equivalent_usd"] == pytest.approx((1000 + 100 * 5.0) / 1e6)
    assert p["saving_usd"] is None


def test_vendor_tagged_runs_group_under_the_provider_name(settings_env: None,
                                                          tmp_path: Path) -> None:
    """A run record tagged with the vendor (`zai`) keys under the provider named
    `glm`, and the vendor is a column of the row."""
    records = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
    vendor_tagged = dict(records[0])  # run-1's shape, re-tagged
    vendor_tagged.update({"run_id": "run-z", "provider": "zai", "lane": "zai",
                          "delegation_id": "del-9"})
    trace = tmp_path / "vendor.jsonl"
    trace.write_text("".join(json.dumps(r) + "\n" for r in [*records, vendor_tagged]),
                     encoding="utf-8")

    rep = report.build([trace], None, None)
    by = {p["provider"]: p for p in rep.providers}
    assert list(by) == ["glm", "omlx"]  # `zai` did not become its own row
    assert by["glm"]["vendor"] == "zai"
    assert by["glm"]["runs"] == 3  # run-1, run-2 and the vendor-tagged run-z
    assert by["omlx"]["vendor"] == "none"  # the vendor derived for an undeclared provider


def test_delegation_usd_falls_back_to_the_spread(settings_env: None, tmp_path: Path) -> None:
    """A delegation whose recorded cost total is null (flat plan: the loop
    cannot price it at run end) shows the report's spread instead."""
    records = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
    for rec in records:
        if rec.get("kind") == "delegation":
            rec["cost_total"]["provider_usd"] = None
    trace = tmp_path / "unpriced.jsonl"
    trace.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    rep = report.build([trace], None, None)
    by = {d["delegation_id"]: d for d in rep.delegations}
    # del-1's single glm run: half of the 80/month plan (two glm runs in the month).
    assert by["del-1"]["provider_usd"] == pytest.approx(40.0)
    assert by["del-2"]["provider_usd"] == pytest.approx(40.0)
    assert by["del-3"]["provider_usd"] == pytest.approx(0.0)


def test_provider_usd_falls_back_to_the_recorded_delegation_total(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no config pricing the provider, the recorded delegation totals are
    shared over the runs they name, so the provider row is not null."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # no config at all
    rep = report.build([FIXTURE], None, None)
    by = {p["provider"]: p for p in rep.providers}
    # del-1 and del-2 each recorded 40.0 for their single glm run.
    assert by["glm"]["provider_usd"] == pytest.approx(80.0)
    assert by["omlx"]["provider_usd"] == pytest.approx(0.0)


def _numbers_in(obj: object):
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _numbers_in(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _numbers_in(value)


def test_html_is_self_contained(settings_env: None) -> None:
    rep = _build()
    html = report.render_html(rep)

    # The JSON data block is present and parses to exactly the report's tables.
    match = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S)
    assert match, "missing <script type=application/json id=data> block"
    json_text = match.group(1)
    data = json.loads(json_text)
    assert data["providers"] == rep.providers
    assert data["delegations"] == rep.delegations
    assert data["meta"]["runs"] == 3
    assert data["meta"]["counterfactual"] == "claude-opus-5"

    # Every number the tables render comes from that JSON block.
    for number in _numbers_in([*rep.providers, *rep.delegations]):
        assert json.dumps(number) in json_text, f"{number!r} missing from the JSON block"

    # No external scripts or stylesheets.
    assert 'src="http' not in html
    assert not [link for link in re.findall(r"<link\b[^>]*>", html) if 'href="http' in link]


def test_html_escapes_trace_strings_before_embedding_and_svg_rendering() -> None:
    attacker = "<img src=x onerror=alert(1)>"
    rep = report.Report(
        providers=[{"provider": attacker, "provider_usd": 1.0, "counterfactual_usd": 2.0}],
        delegations=[{"delegation_id": attacker, "tokens": 1}],
        lanes=[], runs=1, counterfactual=None,
    )

    html = report.render_html(rep)
    match = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S)
    assert match
    assert "<img" not in html
    assert r"\u003cimg" in match.group(1)
    assert json.loads(match.group(1))["providers"][0]["provider"] == attacker


def test_main_json(settings_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    rc = report.main(["--trace", str(FIXTURE), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["meta"]["runs"] == 3
    assert out["meta"]["delegations"] == 3
    assert {p["provider"] for p in out["providers"]} == {"glm", "omlx"}

"""G1 part C: `subagent report` classifies runs by role from the trace (never from names)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from subagent import report

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "report" / "trace.jsonl"

CONFIG = """\
[providers.glm]
driver = "claude"
model = "glm-5.3-flash[1m]"
vendor = "zai"

[providers.omlx]
driver = "claude"
model = "qwen-local"
"""

ROLE_KEYS = ["builder", "review", "setup", "unclassified"]


@pytest.fixture(autouse=True)
def settings_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("SUBAGENT_CONFIG", str(cfg))


def run_rec(rid: str, provider: str = "glm", passed: Any = True, ts: float = 1789700100.0) -> dict:
    return {
        "schema": 3, "ts": ts, "kind": "run", "run_id": rid, "agent_id": "a-" + rid,
        "session_id": "s", "model": "m", "lane": provider, "provider": provider,
        "driver": "claude", "state": "completed", "verification_passed": passed,
        "usage": {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0},
    }


def deleg(did: str, rounds=(), reviews=(), setup=None, test_writer=None, ts: float = 1789700400.0):
    rec: dict[str, Any] = {
        "schema": 3, "ts": ts, "kind": "delegation", "delegation_id": did,
        "bench_run_id": "b", "orchestrator": {"harness": "cli", "model": "x"},
        "mode": "auto", "status": "done", "stopped_because": None,
        "rounds": [{"run_id": r} for r in rounds],
        "reviews": [{"run_id": r} for r in reviews],
        "test_writer": test_writer,
    }
    if setup is not None:
        rec["setup"] = setup
    return rec


def roles_of(tmp_path: Path, records: list[dict], capsys, extra: list[str] | None = None) -> dict:
    trace = tmp_path / "t.jsonl"
    trace.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    assert report.main(["--trace", str(trace), "--json", *(extra or [])]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "run_roles" in data, "report --json has no run_roles"
    assert list(data)[list(data).index("delegations") + 1] == "run_roles"
    return data


def test_builder_review_setup_unclassified_counts(tmp_path: Path, capsys) -> None:
    recs = [
        run_rec("b1", "glm", True), run_rec("b2", "glm", False), run_rec("b3", "omlx", True),
        run_rec("b4", "omlx", None),
        run_rec("r1", "glm", True), run_rec("r2", "omlx", False),
        run_rec("s1"), run_rec("w1"), run_rec("w2"), run_rec("x1"),
        deleg("d1", rounds=["b1", "b2", "b3"], reviews=["r1"], setup={"run_id": "s1"},
              test_writer={"run_id": "w1"}),
        deleg("d2", rounds=["b4"], reviews=["r2"], test_writer=[{"run_id": "w2"}]),
    ]
    rr = roles_of(tmp_path, recs, capsys)["run_roles"]
    assert list(rr) == ROLE_KEYS
    assert rr["builder"] == {
        "runs": 4, "verified_passed": 2, "pass_rate": 0.5,
        "by_provider": {"glm": {"runs": 2, "verified_passed": 1},
                        "omlx": {"runs": 2, "verified_passed": 1}},
    }
    assert list(rr["builder"]) == ["runs", "verified_passed", "pass_rate", "by_provider"]
    assert rr["review"] == {"runs": 2, "verified_passed": 1}
    assert rr["setup"] == {"runs": 3}
    assert rr["unclassified"] == {"runs": 1}


def test_setup_accepts_list_of_dicts_and_none(tmp_path: Path, capsys) -> None:
    recs = [run_rec("s1"), run_rec("s2"), run_rec("s3"), run_rec("w1"),
            deleg("d1", setup=[{"run_id": "s1"}, {"run_id": "s2"}], test_writer={"run_id": "w1"}),
            deleg("d2", setup=None, test_writer=None)]
    rr = roles_of(tmp_path, recs, capsys)["run_roles"]
    assert rr["setup"] == {"runs": 3}
    assert rr["unclassified"] == {"runs": 1}
    assert rr["builder"]["runs"] == 0


def test_pass_rate_rounded_to_three_decimals(tmp_path: Path, capsys) -> None:
    recs = [run_rec(f"b{i}", passed=(i == 0)) for i in range(3)]
    recs.append(deleg("d", rounds=["b0", "b1", "b2"]))
    b = roles_of(tmp_path, recs, capsys)["run_roles"]["builder"]
    assert b["pass_rate"] == 0.333 and b["verified_passed"] == 1


def test_no_builder_runs_pass_rate_null(tmp_path: Path, capsys) -> None:
    rr = roles_of(tmp_path, [run_rec("x1")], capsys)["run_roles"]
    assert rr["builder"] == {"runs": 0, "verified_passed": 0, "pass_rate": None, "by_provider": {}}
    assert rr["unclassified"] == {"runs": 1}


def test_empty_trace(tmp_path: Path, capsys) -> None:
    rr = roles_of(tmp_path, [], capsys)["run_roles"]
    assert rr == {
        "builder": {"runs": 0, "verified_passed": 0, "pass_rate": None, "by_provider": {}},
        "review": {"runs": 0, "verified_passed": 0},
        "setup": {"runs": 0},
        "unclassified": {"runs": 0},
    }


def test_only_exactly_true_counts_as_verified(tmp_path: Path, capsys) -> None:
    recs = [run_rec("b1", passed=True), run_rec("b2", passed=False), run_rec("b3", passed=None),
            run_rec("b4", passed=1), run_rec("b5", passed="true"),
            deleg("d", rounds=["b1", "b2", "b3", "b4", "b5"])]
    b = roles_of(tmp_path, recs, capsys)["run_roles"]["builder"]
    assert b["runs"] == 5 and b["verified_passed"] == 1


def test_first_match_wins_rounds_reviews_setup_test_writer(tmp_path: Path, capsys) -> None:
    recs = [run_rec("a"), run_rec("b"), run_rec("c"), run_rec("d"),
            deleg("d1", rounds=["a"], reviews=["a", "b"], setup={"run_id": "b"}),
            deleg("d2", reviews=["c"], setup={"run_id": "c"}, test_writer={"run_id": "c"}),
            deleg("d3", setup={"run_id": "d"}, test_writer={"run_id": "d"})]
    rr = roles_of(tmp_path, recs, capsys)["run_roles"]
    assert rr["builder"]["runs"] == 1  # a
    assert rr["review"]["runs"] == 2  # b, c
    assert rr["setup"]["runs"] == 1  # d
    assert rr["unclassified"]["runs"] == 0


def test_role_comes_from_trace_not_names(tmp_path: Path, capsys) -> None:
    recs = [run_rec("builder-run"), run_rec("review-run"), run_rec("setup-run"),
            deleg("d", rounds=["review-run"], reviews=["setup-run"], setup={"run_id": "builder-run"})]
    rr = roles_of(tmp_path, recs, capsys)["run_roles"]
    assert (rr["builder"]["runs"], rr["review"]["runs"], rr["setup"]["runs"]) == (1, 1, 1)
    assert rr["unclassified"]["runs"] == 0


def test_listed_run_without_run_record_is_not_counted(tmp_path: Path, capsys) -> None:
    recs = [run_rec("b1"), deleg("d", rounds=["b1", "ghost"], reviews=["ghost2"])]
    rr = roles_of(tmp_path, recs, capsys)["run_roles"]
    assert rr["builder"]["runs"] == 1 and rr["review"]["runs"] == 0


def test_provider_uses_alias_mapping(tmp_path: Path, capsys) -> None:
    recs = [run_rec("b1", provider="zai", passed=True), run_rec("b2", provider="glm", passed=False),
            deleg("d", rounds=["b1", "b2"])]
    b = roles_of(tmp_path, recs, capsys)["run_roles"]["builder"]
    assert b["by_provider"] == {"glm": {"runs": 2, "verified_passed": 1}}


def test_since_filter_applies_first(tmp_path: Path, capsys) -> None:
    recs = [run_rec("old", ts=1000.0), run_rec("new", ts=1789700100.0),
            deleg("dold", rounds=["old"], ts=1001.0), deleg("dnew", rounds=["new"])]
    data = roles_of(tmp_path, recs, capsys, ["--since", "2026-09-15T00:00:00Z"])
    assert data["meta"]["runs"] == 1
    assert data["run_roles"]["builder"]["runs"] == 1
    # delegation dropped by --since no longer classifies its (also dropped) run, and a
    # kept run whose delegation is dropped is unclassified
    recs2 = [run_rec("kept", ts=1789700100.0), deleg("dold", rounds=["kept"], ts=1001.0)]
    data2 = roles_of(tmp_path, recs2, capsys, ["--since", "2026-09-15T00:00:00Z"])
    assert data2["run_roles"]["builder"]["runs"] == 0
    assert data2["run_roles"]["unclassified"]["runs"] == 1


def test_existing_json_keys_unchanged(tmp_path: Path, capsys) -> None:
    data = roles_of(tmp_path, [run_rec("b1"), deleg("d", rounds=["b1"])], capsys)
    assert list(data)[:3] == ["meta", "providers", "delegations"]
    assert list(data["meta"]) == ["generated", "runs", "delegations", "counterfactual"]


def test_fixture_trace_role_counts_sum_to_runs(capsys) -> None:
    assert report.main(["--trace", str(FIXTURE), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    rr = data["run_roles"]
    total = rr["builder"]["runs"] + rr["review"]["runs"] + rr["setup"]["runs"]
    total += rr["unclassified"]["runs"]
    assert total == data["meta"]["runs"] == 3
    assert rr["builder"]["runs"] == 3
    assert rr["builder"]["verified_passed"] == 2
    assert rr["builder"]["pass_rate"] == 0.667
    assert rr["builder"]["by_provider"] == {
        "glm": {"runs": 2, "verified_passed": 1}, "omlx": {"runs": 1, "verified_passed": 1},
    }


def test_text_output_builder_runs_line_after_lanes(tmp_path: Path, capsys) -> None:
    assert report.main(["--trace", str(FIXTURE)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "builder runs: 2 of 3 verified (66.7%)" in lines
    i = lines.index("builder runs: 2 of 3 verified (66.7%)")
    assert i > 2  # after the header lines and the lanes table
    assert any(ln.startswith("del-1") or "del-1" in ln for ln in lines[i:])  # delegations follow


def test_text_output_na_when_no_builder_runs(tmp_path: Path, capsys) -> None:
    trace = tmp_path / "t.jsonl"
    trace.write_text(json.dumps(run_rec("x1")) + "\n", encoding="utf-8")
    assert report.main(["--trace", str(trace)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(ln.startswith("builder runs: 0 of 0 verified (n/a") for ln in lines)

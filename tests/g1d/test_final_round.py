"""G1d (T3): a delegation's verified_pass speaks for its final round only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from subagent import report

CONFIG = """\
[providers.glm]
driver = "claude"
model = "glm-5.3-flash[1m]"
vendor = "zai"

[providers.omlx]
driver = "claude"
model = "qwen-local"
"""

REAL = {"passed": True, "cmd": "uv run pytest -q",
        "counts": {"total": 10, "passed": 10, "failed": 0, "skipped": 0}}
_ABSENT = object()


@pytest.fixture(autouse=True)
def settings_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("SUBAGENT_CONFIG", str(cfg))


def run_rec(rid: str, vp: Any = False, provider: str = "glm") -> dict:
    rec = {
        "schema": 3, "ts": 1789700100.0, "kind": "run", "run_id": rid, "agent_id": "a-" + rid,
        "session_id": "s", "model": "m", "lane": provider, "provider": provider,
        "driver": "claude", "state": "completed",
        "usage": {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0},
    }
    if vp is not _ABSENT:
        rec["verification_passed"] = vp
    return rec


def deleg(did: str, rounds: Any, vpass: Any = True, ts: float = 1789700400.0) -> dict:
    """rounds: a list of (run_id, tests) pairs, or a raw value used as-is."""
    if isinstance(rounds, list) and all(isinstance(r, tuple) for r in rounds):
        rounds = [{"run_id": r, "tests": t} for r, t in rounds]
    rec = {
        "schema": 3, "ts": ts, "kind": "delegation", "delegation_id": did,
        "bench_run_id": "b", "orchestrator": {"harness": "cli", "model": "x"},
        "mode": "auto", "status": "done", "stopped_because": None,
        "rounds": rounds, "reviews": [],
    }
    if vpass is not _ABSENT:
        rec["verified_pass"] = vpass
    return rec


def roles(tmp_path: Path, records: list[dict], capsys) -> dict:
    trace = tmp_path / "t.jsonl"
    trace.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    assert report.main(["--trace", str(trace), "--json"]) == 0
    return json.loads(capsys.readouterr().out)["run_roles"]


def passed_ids(tmp_path: Path, runs: list[dict], delegs: list[dict], capsys) -> dict:
    """Builder verified_passed total plus by_provider for the given trace."""
    return roles(tmp_path, runs + delegs, capsys)["builder"]


def test_final_round_of_verified_pass_counts_with_null_tests_and_false_run(tmp_path, capsys):
    b = passed_ids(tmp_path, [run_rec("b1", False)],
                   [deleg("d1", [("b1", None)])], capsys)
    assert b["runs"] == 1 and b["verified_passed"] == 1


@pytest.mark.parametrize("vp", [False, None, _ABSENT])
def test_final_lift_does_not_depend_on_run_flag(tmp_path, capsys, vp):
    b = passed_ids(tmp_path, [run_rec("b1", vp)], [deleg("d1", [("b1", None)])], capsys)
    assert b["verified_passed"] == 1


def test_non_final_round_is_not_lifted(tmp_path, capsys):
    runs = [run_rec("b1", False), run_rec("b2", False)]
    b = passed_ids(tmp_path, runs, [deleg("d1", [("b1", None), ("b2", None)])], capsys)
    assert b["runs"] == 2
    assert b["verified_passed"] == 1  # only the final round b2


def test_only_last_of_three_rounds_lifted(tmp_path, capsys):
    runs = [run_rec("b1", False), run_rec("b2", False), run_rec("b3", False)]
    b = passed_ids(tmp_path, runs,
                   [deleg("d1", [("b1", None), ("b2", None), ("b3", None)])], capsys)
    assert b["verified_passed"] == 1
    assert b["pass_rate"] == 0.333


def test_verified_pass_false_lifts_nothing(tmp_path, capsys):
    b = passed_ids(tmp_path, [run_rec("b1", False)],
                   [deleg("d1", [("b1", None)], vpass=False)], capsys)
    assert b["verified_passed"] == 0


@pytest.mark.parametrize("vpass", [1, "true", "True", None, 1.0, [True], {"x": 1}, _ABSENT])
def test_verified_pass_must_be_exactly_true(tmp_path, capsys, vpass):
    b = passed_ids(tmp_path, [run_rec("b1", False)],
                   [deleg("d1", [("b1", None)], vpass=vpass)], capsys)
    assert b["verified_passed"] == 0


def test_run_record_flag_still_counts_without_delegation_lift(tmp_path, capsys):
    # non-final round with its own run flag True keeps counting (G1c fallback)
    runs = [run_rec("b1", True), run_rec("b2", False)]
    b = passed_ids(tmp_path, runs,
                   [deleg("d1", [("b1", None), ("b2", None)], vpass=False)], capsys)
    assert b["verified_passed"] == 1


@pytest.mark.parametrize("tests", [
    {"passed": False},
    {"passed": False, "counts": {"total": 5, "failed": 0}},
    {"counts": {"total": 10, "passed": 8, "failed": 2}},
    {"passed": None, "counts": {"total": 10, "passed": 9, "failed": 1}},
])
def test_explicit_failing_own_result_is_not_overridden(tmp_path, capsys, tests):
    b = passed_ids(tmp_path, [run_rec("b1", True)],
                   [deleg("d1", [("b1", tests)], vpass=True)], capsys)
    assert b["runs"] == 1 and b["verified_passed"] == 0


def test_explicit_passing_own_result_counts_in_non_final_round(tmp_path, capsys):
    runs = [run_rec("b1", False), run_rec("b2", False)]
    b = passed_ids(tmp_path, runs,
                   [deleg("d1", [("b1", dict(REAL)), ("b2", {"passed": False})], vpass=True)],
                   capsys)
    assert b["verified_passed"] == 1  # b1 by own tests; b2 final but own result fails


def test_explicit_passing_counts_in_non_final_round(tmp_path, capsys):
    runs = [run_rec("b1", False), run_rec("b2", False)]
    tests = {"counts": {"total": 3, "failed": 0}}
    b = passed_ids(tmp_path, runs,
                   [deleg("d1", [("b1", tests), ("b2", None)], vpass=False)], capsys)
    assert b["verified_passed"] == 1


def test_single_dict_rounds_counts_as_final(tmp_path, capsys):
    rounds = {"run_id": "b1", "tests": None}
    b = passed_ids(tmp_path, [run_rec("b1", False)], [deleg("d1", rounds)], capsys)
    assert b["runs"] == 1 and b["verified_passed"] == 1


def test_single_dict_rounds_not_lifted_when_not_verified(tmp_path, capsys):
    rounds = {"run_id": "b1", "tests": None}
    b = passed_ids(tmp_path, [run_rec("b1", False)], [deleg("d1", rounds, vpass=False)], capsys)
    assert b["verified_passed"] == 0


def test_single_dict_rounds_failing_own_result_not_overridden(tmp_path, capsys):
    rounds = {"run_id": "b1", "tests": {"passed": False}}
    b = passed_ids(tmp_path, [run_rec("b1", True)], [deleg("d1", rounds)], capsys)
    assert b["verified_passed"] == 0


def test_final_is_last_dict_entry_with_str_run_id(tmp_path, capsys):
    rounds = [{"run_id": "b1", "tests": None}, {"run_id": "b2", "tests": None},
              {"note": "no run id"}, {"run_id": None}, {"run_id": 7}, "junk"]
    runs = [run_rec("b1", False), run_rec("b2", False)]
    b = passed_ids(tmp_path, runs, [deleg("d1", rounds)], capsys)
    assert b["runs"] == 2
    assert b["verified_passed"] == 1  # b2 is the final round, b1 is not


def test_final_in_one_delegation_non_final_in_another(tmp_path, capsys):
    runs = [run_rec("b1", False), run_rec("b2", False)]
    delegs = [
        deleg("d1", [("b1", None), ("b2", None)], vpass=False, ts=1789700400.0),
        deleg("d2", [("b2", None), ("b1", None)], vpass=True, ts=1789700500.0),
    ]
    b = passed_ids(tmp_path, runs, delegs, capsys)
    assert b["verified_passed"] == 1  # b1 is final in the verified d2; b2 never lifts


def test_final_in_verified_delegation_wins_over_non_final_in_unverified(tmp_path, capsys):
    runs = [run_rec("b1", False), run_rec("b2", False)]
    delegs = [
        deleg("d1", [("b1", None), ("b2", None)], vpass=True, ts=1789700400.0),
        deleg("d2", [("b2", None), ("b1", None)], vpass=False, ts=1789700500.0),
    ]
    b = passed_ids(tmp_path, runs, delegs, capsys)
    assert b["verified_passed"] == 1  # b2 final in verified d1; b1 final only in unverified d2


def test_non_final_in_verified_and_final_in_unverified_is_not_lifted(tmp_path, capsys):
    runs = [run_rec("b1", False), run_rec("b2", False)]
    delegs = [
        deleg("d1", [("b1", None), ("b2", None)], vpass=False, ts=1789700400.0),
        deleg("d2", [("b2", None)], vpass=True, ts=1789700500.0),
        deleg("d3", [("b1", None), ("b2", None)], vpass=True, ts=1789700600.0),
    ]
    b = passed_ids(tmp_path, runs, delegs, capsys)
    assert b["verified_passed"] == 1  # b2 lifted via d2/d3; b1 only non-final in d3


def test_by_provider_and_pass_rate_follow(tmp_path, capsys):
    runs = [run_rec("g1", False, "glm"), run_rec("g2", False, "glm"),
            run_rec("o1", False, "omlx"), run_rec("o2", False, "omlx")]
    delegs = [
        deleg("dg", [("g1", None), ("g2", None)], vpass=True, ts=1789700400.0),
        deleg("do", [("o1", None), ("o2", None)], vpass=False, ts=1789700500.0),
    ]
    b = passed_ids(tmp_path, runs, delegs, capsys)
    assert b["runs"] == 4
    assert b["verified_passed"] == 1
    assert b["pass_rate"] == 0.25
    assert b["by_provider"] == {
        "glm": {"runs": 2, "verified_passed": 1}, "omlx": {"runs": 2, "verified_passed": 0},
    }


def test_text_line_follows_new_count(tmp_path, capsys):
    runs = [run_rec("b1", False), run_rec("b2", False), run_rec("b3", False)]
    trace = tmp_path / "t.jsonl"
    recs = runs + [deleg("d1", [("b1", None), ("b2", None), ("b3", None)])]
    trace.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    assert report.main(["--trace", str(trace)]) == 0
    assert "builder runs: 1 of 3 verified (33.3%)" in capsys.readouterr().out.splitlines()


def test_builder_keys_unchanged_and_no_new_keys(tmp_path, capsys):
    rr = roles(tmp_path, [run_rec("b1", False), deleg("d1", [("b1", None)])], capsys)
    assert list(rr) == ["builder", "review", "setup", "unclassified"]
    assert list(rr["builder"]) == ["runs", "verified_passed", "pass_rate", "by_provider"]
    assert list(rr["builder"]["by_provider"]["glm"]) == ["runs", "verified_passed"]
    assert list(rr["review"]) == ["runs", "verified_passed"]
    assert list(rr["setup"]) == ["runs"] and list(rr["unclassified"]) == ["runs"]


def test_review_runs_not_lifted_by_delegation(tmp_path, capsys):
    d = deleg("d1", [("b1", None)])
    d["reviews"] = [{"run_id": "r1"}]
    rr = roles(tmp_path, [run_rec("b1", False), run_rec("r1", False), d], capsys)
    assert rr["review"] == {"runs": 1, "verified_passed": 0}

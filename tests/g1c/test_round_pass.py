"""G1c (T3): a builder run is verified by its round's own tests, else by the run record."""

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
        "counts": {"total": 1731, "passed": 1730, "failed": 0, "skipped": 1}}
_ABSENT = object()


@pytest.fixture(autouse=True)
def settings_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("SUBAGENT_CONFIG", str(cfg))


def run_rec(rid: str, vp: Any = True, provider: str = "glm") -> dict:
    rec = {
        "schema": 3, "ts": 1789700100.0, "kind": "run", "run_id": rid, "agent_id": "a-" + rid,
        "session_id": "s", "model": "m", "lane": provider, "provider": provider,
        "driver": "claude", "state": "completed",
        "usage": {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0},
    }
    if vp is not _ABSENT:
        rec["verification_passed"] = vp
    return rec


def deleg(did: str, rounds: list[tuple[str, Any]], reviews=(), ts: float = 1789700400.0) -> dict:
    return {
        "schema": 3, "ts": ts, "kind": "delegation", "delegation_id": did,
        "bench_run_id": "b", "orchestrator": {"harness": "cli", "model": "x"},
        "mode": "auto", "status": "done", "stopped_because": None,
        "rounds": [{"run_id": r, "tests": t} for r, t in rounds],
        "reviews": [{"run_id": r} for r in reviews],
    }


def write(tmp_path: Path, records: list[dict]) -> Path:
    trace = tmp_path / "t.jsonl"
    trace.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return trace


def roles(tmp_path: Path, records: list[dict], capsys) -> dict:
    assert report.main(["--trace", str(write(tmp_path, records)), "--json"]) == 0
    return json.loads(capsys.readouterr().out)["run_roles"]


def one(tmp_path: Path, tests: Any, vp: Any, capsys) -> int:
    recs = [run_rec("b1", vp), deleg("d1", [("b1", tests)])]
    b = roles(tmp_path, recs, capsys)["builder"]
    assert b["runs"] == 1
    return b["verified_passed"]


@pytest.mark.parametrize("vp", [False, None, _ABSENT, True])
def test_a_passed_true_counts_regardless_of_run(tmp_path: Path, capsys, vp) -> None:
    assert one(tmp_path, dict(REAL), vp, capsys) == 1


@pytest.mark.parametrize("vp", [True, False, None])
def test_a_passed_false_does_not_count(tmp_path: Path, capsys, vp) -> None:
    tests = {"passed": False, "cmd": "x", "counts": {"total": 5, "passed": 4, "failed": 1}}
    assert one(tmp_path, tests, vp, capsys) == 0


def test_a_passed_false_beats_counts_clean(tmp_path: Path, capsys) -> None:
    tests = {"passed": False, "counts": {"total": 5, "failed": 0}}
    assert one(tmp_path, tests, True, capsys) == 0


@pytest.mark.parametrize("passed", [None, "absent"])
def test_b_counts_failed_zero_counts(tmp_path: Path, capsys, passed) -> None:
    tests: dict[str, Any] = {"counts": {"total": 10, "passed": 10, "failed": 0}}
    if passed is None:
        tests["passed"] = None
    assert one(tmp_path, tests, False, capsys) == 1


@pytest.mark.parametrize("passed", [None, "absent"])
def test_b_counts_failed_positive_not_counted(tmp_path: Path, capsys, passed) -> None:
    tests: dict[str, Any] = {"counts": {"total": 10, "passed": 8, "failed": 2}}
    if passed is None:
        tests["passed"] = None
    assert one(tmp_path, tests, True, capsys) == 0


def test_b_counts_total_zero_falls_to_run(tmp_path: Path, capsys) -> None:
    tests = {"counts": {"total": 0, "failed": 0}}
    assert one(tmp_path, tests, True, capsys) == 1
    assert one(tmp_path, tests, False, capsys) == 0


def test_b_counts_bool_total_is_unusable(tmp_path: Path, capsys) -> None:
    tests = {"counts": {"total": True, "failed": 0}}
    assert one(tmp_path, tests, False, capsys) == 0


@pytest.mark.parametrize("tests", [None, 7, {"cmd": "x"}, {"passed": None},
                                   {"passed": "yes", "counts": "n/a"}])
def test_c_falls_back_to_run_flag(tmp_path: Path, capsys, tests) -> None:
    assert one(tmp_path, tests, True, capsys) == 1
    assert one(tmp_path, tests, False, capsys) == 0
    assert one(tmp_path, tests, None, capsys) == 0


def test_c_int_tests_never_count_by_itself(tmp_path: Path, capsys) -> None:
    assert one(tmp_path, 1731, False, capsys) == 0


def test_c_run_flag_must_be_exactly_true(tmp_path: Path, capsys) -> None:
    assert one(tmp_path, None, 1, capsys) == 0
    assert one(tmp_path, None, "true", capsys) == 0


def test_first_round_entry_wins_across_delegations(tmp_path: Path, capsys) -> None:
    fail = {"passed": False}
    recs = [run_rec("b1", True), run_rec("b2", False),
            deleg("d1", [("b1", fail), ("b2", dict(REAL))], ts=1789700400.0),
            deleg("d2", [("b1", dict(REAL)), ("b2", fail)], ts=1789700500.0)]
    b = roles(tmp_path, recs, capsys)["builder"]
    assert b["runs"] == 2
    assert b["verified_passed"] == 1  # b1 fails (first), b2 passes (first)


def test_first_round_entry_fail_then_pass(tmp_path: Path, capsys) -> None:
    recs = [run_rec("b1", True),
            deleg("d1", [("b1", {"passed": False})], ts=1789700400.0),
            deleg("d2", [("b1", dict(REAL))], ts=1789700500.0)]
    assert roles(tmp_path, recs, capsys)["builder"]["verified_passed"] == 0


def test_first_round_entry_pass_then_fail(tmp_path: Path, capsys) -> None:
    recs = [run_rec("b1", False),
            deleg("d1", [("b1", dict(REAL))], ts=1789700400.0),
            deleg("d2", [("b1", {"passed": False})], ts=1789700500.0)]
    assert roles(tmp_path, recs, capsys)["builder"]["verified_passed"] == 1


def test_by_provider_and_pass_rate_mix(tmp_path: Path, capsys) -> None:
    recs = [
        run_rec("b1", False, "glm"), run_rec("b2", True, "glm"),
        run_rec("b3", True, "omlx"), run_rec("b4", False, "omlx"),
        deleg("d1", [("b1", dict(REAL)), ("b2", {"passed": False}),
                     ("b3", None), ("b4", {"counts": {"total": 3, "failed": 0}})]),
    ]
    b = roles(tmp_path, recs, capsys)["builder"]
    assert b["runs"] == 4
    assert b["verified_passed"] == 3
    assert b["pass_rate"] == 0.75
    assert b["by_provider"] == {
        "glm": {"runs": 2, "verified_passed": 1}, "omlx": {"runs": 2, "verified_passed": 2},
    }


def test_review_role_unchanged(tmp_path: Path, capsys) -> None:
    recs = [run_rec("b1"), run_rec("r1", True), run_rec("r2", False), run_rec("r3", None),
            deleg("d1", [("b1", None)], reviews=["r1", "r2", "r3"])]
    rr = roles(tmp_path, recs, capsys)
    assert rr["review"] == {"runs": 3, "verified_passed": 1}


def test_text_line_follows_new_count(tmp_path: Path, capsys) -> None:
    recs = [run_rec("b1", False), run_rec("b2", True), run_rec("b3", True),
            deleg("d1", [("b1", dict(REAL)), ("b2", {"passed": False}),
                         ("b3", {"passed": False})])]
    assert report.main(["--trace", str(write(tmp_path, recs))]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "builder runs: 1 of 3 verified (33.3%)" in lines

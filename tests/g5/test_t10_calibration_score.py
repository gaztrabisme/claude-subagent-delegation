"""T10: scripts/calibration_score.py (SPEC section 3)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from ._helpers import assert_out_contract, run, run_json

S = "calibration_score.py"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)


def plant(i: str, line: int, kind: str = "logic", file: str = "src/x.py") -> dict:
    return {"id": i, "kind": kind, "file": file, "line": line, "failure": "f"}


def write(p: Path, data: object) -> Path:
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def fnd(line: int, file: str = "src/x.py") -> dict:
    return {"file": file, "line": line, "text": "t"}


class Case:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp

    def go(self, plants: list[dict], lanes: dict[str, list[dict]], decoys: list[dict] | None = None,
           qa: dict | None = None) -> dict:
        key = write(self.tmp / "key.json", {"plants": plants, "decoys": decoys or []})
        args: list[str | Path] = ["--key", key]
        for lane, fs in lanes.items():
            args += ["--findings", f"{lane}={write(self.tmp / f'{lane}.json', {'findings': fs})}"]
        if qa is not None:
            args += ["--qa", write(self.tmp / "qa.json", qa)]
        return run_json(S, args)


@pytest.fixture
def case(tmp_path: Path) -> Case:
    return Case(tmp_path)


@pytest.mark.parametrize("line,caught", [(5, True), (10, True), (15, True), (4, False), (16, False)])
def test_window_is_plus_minus_5_inclusive(case: Case, line: int, caught: bool) -> None:
    out = case.go([plant("P1", 10)], {"L": [fnd(line)]})
    assert out["lanes"]["L"]["caught"] == (1 if caught else 0)
    assert out["lanes"]["L"]["caught_ids"] == (["P1"] if caught else [])


def test_other_file_does_not_match(case: Case) -> None:
    out = case.go([plant("P1", 10)], {"L": [fnd(10, "src/y.py")]})
    assert out["lanes"]["L"]["caught"] == 0 and out["lanes"]["L"]["unmatched"] == 1


@pytest.mark.parametrize("name", ["src/x.py", "./src/x.py", "a/src/x.py", "b/src/x.py"])
def test_file_normalisation(case: Case, name: str) -> None:
    out = case.go([plant("P1", 10)], {"L": [fnd(10, name)]})
    assert out["lanes"]["L"]["caught_ids"] == ["P1"]


def test_nearest_line_wins(case: Case) -> None:
    out = case.go([plant("P1", 10), plant("P2", 14)], {"L": [fnd(13)]})
    assert out["lanes"]["L"]["caught_ids"] == ["P2"] and out["lanes"]["L"]["caught"] == 1


def test_tie_goes_to_lower_plant_id(case: Case) -> None:
    out = case.go([plant("P2", 14), plant("P1", 10)], {"L": [fnd(12)]})
    assert out["lanes"]["L"]["caught_ids"] == ["P1"]


def test_one_finding_counts_for_one_plant_only(case: Case) -> None:
    out = case.go([plant("P1", 10), plant("P2", 12)], {"L": [fnd(11), fnd(40)]})
    assert out["lanes"]["L"]["caught"] == 1 and out["lanes"]["L"]["unmatched"] == 1


def test_two_findings_one_plant_counted_once(case: Case) -> None:
    out = case.go([plant("P1", 10)], {"L": [fnd(10), fnd(11)]})
    assert out["lanes"]["L"]["caught"] == 1 and out["lanes"]["L"]["findings"] == 2


def test_without_qa_unmatched_not_false_positive(case: Case) -> None:
    out = case.go([plant("P1", 10)], {"L": [fnd(10), fnd(90), fnd(95)]})
    lane = out["lanes"]["L"]
    assert lane["qa"] is False
    assert (lane["caught"], lane["planted"], lane["unmatched"], lane["false_positives"], lane["findings"]) == (1, 1, 2, 0, 3)


def test_qa_must_name_the_plant(case: Case) -> None:
    plants = [plant("P1", 10), plant("P2", 50)]
    lanes = {"L": [fnd(10), fnd(50), fnd(52)]}
    qa = {"L": {"0": {"plant": "P1", "real_defect": True},
                "1": {"plant": None, "real_defect": False},
                "2": {"plant": "P1", "real_defect": True}}}
    out = case.go(plants, lanes, qa=qa)
    lane = out["lanes"]["L"]
    assert lane["qa"] is True and lane["caught_ids"] == ["P1"] and lane["caught"] == 1


def test_qa_naming_other_plant_does_not_catch(case: Case) -> None:
    qa = {"L": {"0": {"plant": "P2", "real_defect": True}}}
    out = case.go([plant("P1", 10), plant("P2", 50)], {"L": [fnd(10)]}, qa=qa)
    assert out["lanes"]["L"]["caught"] == 0


def test_qa_on_a_far_finding_does_not_catch(case: Case) -> None:
    qa = {"L": {"0": {"plant": "P1", "real_defect": True}}}
    out = case.go([plant("P1", 10)], {"L": [fnd(30)]}, qa=qa)
    assert out["lanes"]["L"]["caught"] == 0


@pytest.mark.parametrize("entry,fp", [
    ({"plant": None, "real_defect": False}, 1),
    ({"plant": None, "real_defect": True}, 0),
    (None, 1),
])
def test_qa_false_positive_rules(case: Case, entry: dict | None, fp: int) -> None:
    qa = {"L": {} if entry is None else {"0": entry}}
    out = case.go([plant("P1", 10)], {"L": [fnd(80)]}, qa=qa)
    assert out["lanes"]["L"]["false_positives"] == fp


def test_decoy_is_always_false_positive(case: Case) -> None:
    decoys = [{"id": "D1", "file": "src/z.py", "line": 20}]
    f = [fnd(22, "src/z.py")]
    assert case.go([plant("P1", 10)], {"L": f}, decoys)["lanes"]["L"]["false_positives"] == 1
    qa = {"L": {"0": {"plant": None, "real_defect": True}}}
    assert case.go([plant("P1", 10)], {"L": f}, decoys, qa)["lanes"]["L"]["false_positives"] == 1


def test_wilson_known_value(case: Case) -> None:
    plants = [plant(f"P{i:02d}", i * 100) for i in range(1, 11)]
    fs = [fnd(i * 100) for i in range(1, 9)]
    lane = case.go(plants, {"L": fs})["lanes"]["L"]
    assert lane["caught"] == 8 and lane["planted"] == 10
    assert lane["rate"] == pytest.approx(0.8)
    assert lane["wilson95"] == pytest.approx([0.49, 0.943], abs=1e-9)


@pytest.mark.parametrize("k,n", [(0, 4), (4, 4), (1, 3), (3, 7)])
def test_wilson_matches_independent_formula(case: Case, k: int, n: int) -> None:
    plants = [plant(f"P{i}", i * 100) for i in range(1, n + 1)]
    fs = [fnd(i * 100) for i in range(1, k + 1)]
    lane = case.go(plants, {"L": fs})["lanes"]["L"]
    assert lane["wilson95"] == pytest.approx(list(wilson(k, n)), abs=1e-9)
    assert lane["rate"] == pytest.approx(k / n, abs=0.001)


def test_n_zero_is_null(case: Case) -> None:
    out = case.go([], {"L": [fnd(1)]})
    lane = out["lanes"]["L"]
    assert out["plants"] == 0 and lane["rate"] is None and lane["wilson95"] is None
    assert lane["planted"] == 0 and lane["caught"] == 0


def test_by_kind_and_security(case: Case) -> None:
    plants = [plant("P1", 100, "security"), plant("P2", 200, "logic"), plant("P3", 300, "logic"),
              plant("P4", 400, "interface"), plant("P5", 500, "test")]
    out = case.go(plants, {"L": [fnd(100), fnd(200), fnd(500)], "M": []})
    assert out["plants"] == 5
    lane = out["lanes"]["L"]
    assert list(lane["by_kind"]) == ["interface", "logic", "security", "test"]
    assert lane["by_kind"] == {"interface": {"caught": 0, "planted": 1}, "logic": {"caught": 1, "planted": 2},
                               "security": {"caught": 1, "planted": 1}, "test": {"caught": 1, "planted": 1}}
    assert lane["security"] == {"caught": 1, "planted": 1}
    assert out["lanes"]["M"]["caught"] == 0
    assert list(out["lanes"]["M"]["by_kind"]) == ["interface", "logic", "security", "test"]


def test_by_kind_lists_kind_with_no_plant_caught_only_if_in_key(case: Case) -> None:
    out = case.go([plant("P1", 10, "logic")], {"L": []})
    assert list(out["lanes"]["L"]["by_kind"]) == ["logic"]
    assert out["lanes"]["L"]["security"] == {"caught": 0, "planted": 0}


def test_lanes_sorted_and_per_lane_qa_flag(case: Case) -> None:
    qa = {"beta": {"0": {"plant": "P1", "real_defect": True}}}
    out = case.go([plant("P1", 10)], {"zeta": [fnd(10)], "beta": [fnd(10)], "alpha": [fnd(10)]}, qa=qa)
    assert list(out["lanes"]) == ["alpha", "beta", "zeta"]
    assert out["lanes"]["beta"]["qa"] is True and out["lanes"]["alpha"]["qa"] is False
    assert out["warnings"] == []


@pytest.mark.parametrize("what", ["key_bad", "key_missing", "findings_missing", "findings_bad", "no_equals", "qa_missing"])
def test_bad_input_exit_2(tmp_path: Path, what: str) -> None:
    key = write(tmp_path / "key.json", {"plants": [plant("P1", 1)], "decoys": []})
    fl = write(tmp_path / "f.json", {"findings": []})
    args: dict[str, list] = {"key": [key], "fs": [f"L={fl}"], "qa": []}
    if what == "key_bad":
        key.write_text("{oops")
    elif what == "key_missing":
        args["key"] = [tmp_path / "none.json"]
    elif what == "findings_missing":
        args["fs"] = [f"L={tmp_path / 'none.json'}"]
    elif what == "findings_bad":
        fl.write_text("[1,")
    elif what == "no_equals":
        args["fs"] = [str(fl)]
    else:
        args["qa"] = ["--qa", tmp_path / "none.json"]
    r = run(S, ["--key", args["key"][0], "--findings", args["fs"][0], *args["qa"]])
    assert r.returncode == 2 and r.stderr.strip()


def test_out_contract(tmp_path: Path) -> None:
    key = write(tmp_path / "key.json", {"plants": [plant("P1", 10)], "decoys": []})
    fl = write(tmp_path / "f.json", {"findings": [fnd(10)]})
    o = tmp_path / "o"
    o.mkdir()
    assert_out_contract(S, ["--key", key, "--findings", f"L={fl}"], o / "r.json", [o, tmp_path])
    assert json.loads((o / "r.json").read_text())["lanes"]["L"]["caught"] == 1

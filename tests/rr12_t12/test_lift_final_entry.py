"""RR-12 T12 re-review: the G1d lift reads the final round's own entry."""

from __future__ import annotations

from tests.g1d.test_final_round import REAL, deleg, passed_ids, run_rec  # noqa: F401

FAIL = {"passed": False, "cmd": "uv run pytest -q",
        "counts": {"total": 10, "passed": 9, "failed": 1, "skipped": 0}}


def test_final_entry_failing_result_blocks_lift_when_first_entry_has_none(tmp_path, capsys):
    b = passed_ids(tmp_path, [run_rec("b1", False)],
                   [deleg("d1", [("b1", None), ("b1", FAIL)])], capsys)
    assert b["runs"] == 1 and b["verified_passed"] == 0


def test_final_entry_without_result_is_lifted_even_if_first_entry_failed(tmp_path, capsys):
    b = passed_ids(tmp_path, [run_rec("b1", False)],
                   [deleg("d1", [("b1", FAIL), ("b1", None)])], capsys)
    assert b["runs"] == 1 and b["verified_passed"] == 1


def test_run_in_two_delegations_uses_the_verified_final_entry(tmp_path, capsys):
    b = passed_ids(tmp_path, [run_rec("b1", False)],
                   [deleg("d1", [("b1", None)], vpass=False),
                    deleg("d2", [("b1", FAIL)], vpass=True)], capsys)
    assert b["verified_passed"] == 0

"""The loop's `delegation` record: one per `run`, summing the worker runs.

A single `run --plan` is the smallest delegation that writes run records, so
it is the unit: exactly one delegation record whose rounds[].usage sums equal
the `run` records in the same trace, and (with the env var set) every record
carries the bench harness's run id.
"""

import json
import unittest
from pathlib import Path

from .loop.helpers import Sandbox, for_drivers, need

TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write", "reasoning")


def _read_trace(session_root: Path) -> list[dict]:
    # The sandbox config sets [core].session_root to <tmp>/sessions.
    path = session_root / "trace.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _usage_sum(usages) -> dict[str, int]:
    total = {key: 0 for key in TOKEN_FIELDS}
    for usage in usages:
        if not isinstance(usage, dict):
            continue
        for key in TOKEN_FIELDS:
            total[key] += int(usage.get(key) or 0)
    return total


@need("git", "node", "npm")
class DelegationTrace(Sandbox):
    def test_one_delegation_record_sums_the_run_usage(self):
        root = self.node_project()
        code, result = self.delegate("run", "--plan", ".subagent/PLAN.md", cwd=root,
                                     scenario="good")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "done")

        records = _read_trace(self.tmp / "sessions")
        delegations = [rec for rec in records if rec["kind"] == "delegation"]
        self.assertEqual(len(delegations), 1)
        delegation = delegations[0]

        runs = [rec for rec in records if rec["kind"] == "run"]
        self.assertTrue(runs, "a delegation must have at least one run record")
        run_sum = _usage_sum(rec.get("usage") for rec in runs)
        round_sum = _usage_sum(round_["usage"] for round_ in delegation["rounds"])
        self.assertEqual(round_sum, run_sum)
        for key in TOKEN_FIELDS:
            self.assertEqual(delegation["usage_total"][key], run_sum[key])
        self.assertEqual(delegation["credits_total"], result.get("worker_credits"))

    def test_bench_run_id_and_delegation_id_tag_every_record(self):
        root = self.node_project()
        code, result = self.delegate(
            "run", "--plan", ".subagent/PLAN.md", cwd=root, scenario="good",
            env={"SUBAGENT_BENCH_RUN_ID": "bench-42",
                 "SUBAGENT_ORCHESTRATOR": "pytest",
                 "SUBAGENT_ORCHESTRATOR_MODEL": "claude-opus-5"},
        )
        self.assertEqual(code, 0)

        records = _read_trace(self.tmp / "sessions")
        self.assertTrue(records)
        delegation = next(rec for rec in records if rec["kind"] == "delegation")
        self.assertEqual(delegation["orchestrator"],
                         {"harness": "pytest", "model": "claude-opus-5"})
        for rec in records:
            self.assertEqual(rec.get("bench_run_id"), "bench-42", rec["kind"])
            self.assertTrue(rec.get("delegation_id"), rec["kind"])


for_drivers(DelegationTrace)

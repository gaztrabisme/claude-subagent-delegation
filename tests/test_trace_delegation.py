"""The loop's `delegation` record: one per `run`, summing the worker runs.

A single `run --plan` is the smallest delegation that writes run records, so
it is the unit: exactly one delegation record whose rounds[].usage sums equal
the `run` records in the same trace, and (with the env var set) every record
carries the bench harness's run id. The setup runs (test writer, test review)
are summed too, tagged with their phase.
"""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from subagent.loop.loop import _delegation_record, _run_block

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


def _block(run_id, provider, input_tokens, credits, provider_usd, phase=None):
    """A `_run_block` for a stand-in run object with the facts the record sums."""
    run = SimpleNamespace(
        run_id=run_id,
        lane=provider,
        provider=provider,
        usage=SimpleNamespace(as_dict=lambda: {"input": input_tokens}, credits=credits),
        cost={"provider_usd": provider_usd, "counterfactual_usd": 1.0},
    )
    return _run_block(run, "m", phase=phase)


class SetupRunsInTotals(unittest.TestCase):
    """The test-writer and test-review runs carry the delegation id, so they
    belong in the delegation totals alongside the implementation rounds."""

    def _record(self, blocks=(), setup=()):
        return _delegation_record(
            mode="auto", status="done", stopped_because=None, changed_files=None,
            wall_seconds=1, rounds=[], reviews=[], test_writer=None,
            blocks=list(blocks), setup=list(setup),
        )

    def test_setup_blocks_feed_the_totals_and_carry_their_phase(self):
        setup = [
            _block("run-w", "glm", 100, 1.0, None, phase="test_writer"),
            _block("run-r", "glm", 50, 0.5, None, phase="test_review"),
        ]
        record = self._record(setup=setup)
        self.assertEqual(record["usage_total"]["input"], 150)
        self.assertEqual(record["credits_total"], 1.5)
        self.assertEqual([b["phase"] for b in record["setup"]],
                         ["test_writer", "test_review"])

    def test_setup_and_rounds_are_summed_together(self):
        setup = [_block("run-w", "glm", 100, 1.0, 2.0, phase="test_writer")]
        blocks = [_block("run-1", "glm", 200, 2.0, 4.0)]
        record = self._record(blocks=blocks, setup=setup)
        self.assertEqual(record["usage_total"]["input"], 300)
        self.assertEqual(record["credits_total"], 3.0)
        self.assertEqual(record["cost_total"]["provider_usd"], 6.0)
        self.assertEqual(record["cost_total"]["counterfactual_usd"], 2.0)

    def test_a_flat_plan_setup_run_leaves_the_recorded_provider_cost_null(self):
        record = self._record(
            blocks=[_block("run-1", "glm", 200, 0.0, None)],
            setup=[_block("run-w", "glm", 100, 0.0, None, phase="test_writer")],
        )
        # USD never treats a null as zero: the spread happens at report time.
        self.assertIsNone(record["cost_total"]["provider_usd"])


for_drivers(DelegationTrace)

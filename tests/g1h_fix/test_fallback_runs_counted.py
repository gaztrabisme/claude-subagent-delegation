"""G1h fix: only a run record with refusal_code "envelope" AND finish_reason "no_lane"
is excluded from the run caps. A fallback run after an envelope refusal counts."""

from __future__ import annotations

import time

from subagent import lane_status
from subagent.lane_status import LaneStatusStore
from subagent.runs import COMPLETED

from ..e1 import _helpers as e1
from ..e1.test_dispatch import over_codex
from ..g1h.test_envelope_refusals_not_counted import record, write_trace


def counts(tmp_path, lane):
    settings = e1.build_settings(tmp_path, ("codex", "glm"))
    snap = LaneStatusStore(settings.session_root).snapshot(
        settings, now=time.time(), count_runs=True)
    runs = snap[lane]["runs"]
    return runs["day"], runs["week"]


def test_completed_fallback_run_after_envelope_refusal_counts(tmp_path):
    write_trace(tmp_path, [record(0, lane="glm", refusal_code="envelope",
                                  finish_reason="completed", turns=1)])
    assert counts(tmp_path, "glm") == (1, 1)


def test_failed_fallback_run_that_reached_a_vendor_counts(tmp_path):
    write_trace(tmp_path, [record(0, lane="glm", state="failed", refusal_code="envelope",
                                  finish_reason="error", turns=1)])
    assert counts(tmp_path, "glm") == (1, 1)


def test_guard_no_lane_envelope_refusal_still_not_counted(tmp_path):
    write_trace(tmp_path, [record(0, lane="glm", state="failed", refusal_code="envelope",
                                  finish_reason="no_lane", turns=0)])
    assert counts(tmp_path, "glm") == (0, 0)


def test_registry_fallback_run_counts_on_the_fallback_lane(make_env, tmp_path):
    plan = e1.write_plan(tmp_path, e1.plan_text().replace(
        "| glm, gpubox | closed | 0 | - |",
        "| glm, gpubox | closed | at most 2 runs a week | - |",
    ))
    env = make_env(plan=plan, seed=over_codex)
    assert counts(tmp_path, "glm") == (0, 0)
    run = env.run("codex", fallback="full")
    assert run.state == COMPLETED and run.lane == "glm"
    assert len(env.spawned) == 1
    lane_status._RUN_CACHE.clear()
    assert counts(tmp_path, "glm")[0] == 1

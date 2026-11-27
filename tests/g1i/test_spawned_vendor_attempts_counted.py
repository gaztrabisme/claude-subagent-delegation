"""G1i: a run whose primary vendor was spawned and refused counts toward the cap.

Offline. Trace: `refused_codes` on every run record. Count rule: an envelope+no_lane
record is left out only when every refused code is "envelope"; legacy records (no
`refused_codes`) keep the S123 rule.
"""

from __future__ import annotations

import time

import pytest

from subagent import lane_status
from subagent.runs import COMPLETED, FAILED

from ..e1 import _helpers as e1
from ..e1._helpers import FakeProcess as _OkProcess
from ..e1.test_dispatch import over_codex
from ..g1h.test_envelope_refusals_not_counted import record, write_trace
from ..g1h_fix.test_fallback_runs_counted import counts
from ..test_trace_schema import problems

GLM_REFUSAL = {
    "type": "result", "subtype": "error", "is_error": True,
    "result": "API Error: Request rejected (429) · [1308][Usage limit reached for 5 hour.]",
}


class _RefusingProcess:
    def __init__(self, argv, env):
        self.argv = argv
        self.env = env

    def events(self):
        yield {"type": "system", "subtype": "init", "session_id": "s-refuse"}
        yield dict(GLM_REFUSAL)

    def kill(self):
        pass


def _plan(tmp_path):
    return e1.write_plan(tmp_path, e1.plan_text().replace(
        "| glm, gpubox | closed | 0 | - |",
        "| glm, gpubox | closed | at most 5 runs a week | - |",
    ))


def _glm_refuses(monkeypatch, env, ok_after: bool = False):
    spawned = []

    def spawn(argv, envv, cwd):
        proc = _RefusingProcess(argv, envv)
        spawned.append(proc)
        return proc

    monkeypatch.setattr("subagent.providers.claude._spawn_claude", spawn)
    return spawned


def run_records(env):
    return [r for r in env.trace() if r.get("kind") == "run"]


# --- 1. trace field -------------------------------------------------------------


def test_trace_lists_vendor_then_envelope_refusal(make_env, tmp_path, monkeypatch):
    env = make_env(plan=_plan(tmp_path), seed=over_codex)
    _glm_refuses(monkeypatch, env)
    run = env.run("glm", fallback="full")
    assert run.state == FAILED
    assert [h["outcome"] for h in run.hops] == ["refused", "refused"]
    assert run.hops[0]["code"] == "zai_1308" and run.hops[1]["code"] == "envelope"
    rec = run_records(env)[-1]
    assert rec["refused_codes"] == ["zai_1308", "envelope"]
    assert rec["refusal_code"] == "envelope" and rec["finish_reason"] == "no_lane"
    assert problems(rec) == []


def test_trace_no_refused_hop_writes_empty_list(make_env, tmp_path):
    env = make_env(plan=_plan(tmp_path))
    run = env.run("glm")
    assert run.state == COMPLETED
    rec = run_records(env)[-1]
    assert rec["refused_codes"] == []
    assert problems(rec) == []


def test_trace_ran_after_envelope_refusal(make_env, tmp_path):
    env = make_env(plan=_plan(tmp_path), seed=over_codex)
    run = env.run("codex", fallback="full")
    assert run.state == COMPLETED and run.lane == "glm"
    rec = run_records(env)[-1]
    assert rec["refused_codes"] == ["envelope"]
    assert problems(rec) == []


# --- 2. count rule on synthetic records -----------------------------------------


@pytest.mark.parametrize("codes", [["envelope"], ["envelope", "envelope"]])
def test_only_envelope_codes_not_counted(tmp_path, codes):
    write_trace(tmp_path, [record(0, lane="glm", state="failed", refusal_code="envelope",
                                  finish_reason="no_lane", turns=0, refused_codes=codes)])
    assert counts(tmp_path, "glm") == (0, 0)


def test_spawned_vendor_attempt_then_envelope_counts_once(tmp_path):
    write_trace(tmp_path, [record(0, lane="glm", state="failed", refusal_code="envelope",
                                  finish_reason="no_lane", turns=0,
                                  refused_codes=["zai_1308", "envelope"])])
    assert counts(tmp_path, "glm") == (1, 1)


def test_vendor_code_first_or_last_both_count(tmp_path):
    write_trace(tmp_path, [
        record(0, lane="glm", state="failed", refusal_code="envelope",
               finish_reason="no_lane", turns=0, refused_codes=["envelope", "zai_1308"]),
        record(1, lane="glm", state="failed", refusal_code="envelope",
               finish_reason="no_lane", turns=0, refused_codes=["zai_1308", "envelope"]),
    ])
    assert counts(tmp_path, "glm") == (2, 2)


def test_legacy_record_without_refused_codes_keeps_s123(tmp_path):
    write_trace(tmp_path, [record(0, lane="glm", state="failed", refusal_code="envelope",
                                  finish_reason="no_lane", turns=0)])
    assert counts(tmp_path, "glm") == (0, 0)


def test_mixed_records_exact_counts(tmp_path):
    write_trace(tmp_path, [
        record(0, lane="glm", state="failed", refusal_code="envelope",
               finish_reason="no_lane", turns=0, refused_codes=["envelope"]),
        record(1, lane="glm", state="failed", refusal_code="envelope",
               finish_reason="no_lane", turns=0, refused_codes=["zai_1308", "envelope"]),
        record(2, lane="glm", state="failed", refusal_code="envelope",
               finish_reason="no_lane", turns=0),
        record(3, lane="glm", finish_reason="stop", turns=1, refused_codes=[]),
        record(4, lane="glm", age=3 * 86400.0, state="failed", refusal_code="envelope",
               finish_reason="no_lane", turns=0, refused_codes=["zai_1308", "envelope"]),
    ])
    assert counts(tmp_path, "glm") == (2, 3)


# --- 3. real Registry path ------------------------------------------------------


def test_registry_spawned_vendor_refusal_then_envelope_counts_on_glm(
        make_env, tmp_path, monkeypatch):
    env = make_env(plan=_plan(tmp_path), seed=over_codex)
    spawned = _glm_refuses(monkeypatch, env)
    assert counts(tmp_path, "glm") == (0, 0)
    run = env.run("glm", fallback="full")
    assert run.state == FAILED and len(spawned) == 1
    assert [h["code"] for h in run.hops] == ["zai_1308", "envelope"]
    lane_status._RUN_CACHE.clear()
    assert counts(tmp_path, "glm")[0] == 1
    assert counts(tmp_path, "codex") == (0, 0)


def test_registry_pure_envelope_refusal_still_not_counted(make_env, tmp_path):
    plan = e1.write_plan(tmp_path, e1.plan_text().replace(
        "| glm, gpubox | closed | 0 | - |",
        "| glm, gpubox | closed | at most 5 runs a week | - |",
    ))
    env = make_env(plan=plan, seed=over_codex)
    e1.seed_runs(tmp_path, "glm", 0)
    run = env.run("codex")
    assert run.state == FAILED and env.spawned == []
    lane_status._RUN_CACHE.clear()
    assert counts(tmp_path, "codex") == (0, 0)


# --- 4. guards: the G1h and G1h-fix suites stay in the gate ---------------------

_ = (time, _OkProcess)

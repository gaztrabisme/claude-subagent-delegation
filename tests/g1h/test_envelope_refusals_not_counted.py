"""G1h: a run refused at admission by the envelope does not count toward run caps.

Offline: synthetic trace files under tmp_path, the real LaneStatusStore.snapshot.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from subagent import lane_status
from subagent.lane_status import LaneStatusStore
from subagent.runs import COMPLETED, FAILED

from ..e1 import _helpers as e1

LANE = "gemini"


@pytest.fixture(autouse=True)
def _isolate():
    lane_status._RUN_CACHE.clear()
    yield
    lane_status._RUN_CACHE.clear()


@pytest.fixture(autouse=True)
def trace_records():
    yield []


@pytest.fixture
def make_env(tmp_path, monkeypatch):
    made = []

    def factory(*args, **kwargs):
        env = e1.Env(tmp_path, monkeypatch, *args, **kwargs)
        made.append(env)
        return env

    yield factory
    for env in made:
        env.close()


def record(n: int, *, age: float = 3600.0, lane: str = LANE, **extra) -> str:
    base = {
        "schema": 3, "ts": time.time() - age, "kind": "run", "run_id": f"g1h-{lane}-{n}",
        "lane": lane, "provider": lane, "state": "completed",
    }
    base.update(extra)
    return json.dumps(base)


def refused(n: int, **kw) -> str:
    return record(n, state="failed", refusal_code="envelope", finish_reason="no_lane",
                  turns=0, **kw)


def write_trace(tmp_path: Path, lines: list[str]) -> None:
    (tmp_path / "trace.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def counts(tmp_path: Path, lane: str = LANE) -> tuple[int, int]:
    settings = e1.build_settings(tmp_path, (LANE, "glm"))
    snap = LaneStatusStore(settings.session_root).snapshot(
        settings, now=time.time(), count_runs=True)
    runs = snap[lane]["runs"]
    return runs["day"], runs["week"]


def test_single_envelope_refusal_not_counted(tmp_path):
    write_trace(tmp_path, [refused(0)])
    assert counts(tmp_path) == (0, 0)


def test_several_envelope_refusals_not_counted(tmp_path):
    write_trace(tmp_path, [refused(i) for i in range(5)] + [refused(9, age=3 * 86400.0)])
    assert counts(tmp_path) == (0, 0)


def test_normal_completed_run_counted(tmp_path):
    write_trace(tmp_path, [record(0, finish_reason="stop", turns=3)])
    assert counts(tmp_path) == (1, 1)


def test_vendor_failure_counted(tmp_path):
    write_trace(tmp_path, [record(0, state="failed", finish_reason="cli_error", turns=1)])
    assert counts(tmp_path) == (1, 1)


def test_no_lane_without_refusal_code_counted(tmp_path):
    write_trace(tmp_path, [record(0, state="failed", finish_reason="no_lane", turns=0)])
    assert counts(tmp_path) == (1, 1)


@pytest.mark.parametrize("code", ["policy", "refused", "auth", "guard"])
def test_other_refusal_code_counted(tmp_path, code):
    write_trace(tmp_path, [record(0, state="failed", refusal_code=code,
                                  finish_reason="refused", turns=0)])
    assert counts(tmp_path) == (1, 1)


def test_refusal_code_null_counted(tmp_path):
    write_trace(tmp_path, [record(0, refusal_code=None, finish_reason="stop", turns=2)])
    assert counts(tmp_path) == (1, 1)


def test_mixed_trace_exact_counts(tmp_path):
    day = 3600.0
    week = 3 * 86400.0
    lines = [
        record(0, age=day, finish_reason="stop", turns=2),
        record(1, age=day, state="failed", finish_reason="cli_error", turns=1),
        record(2, age=day, state="failed", finish_reason="no_lane", turns=0),
        record(3, age=day, state="failed", refusal_code="policy", finish_reason="refused"),
        refused(4, age=day),
        refused(5, age=day),
        record(6, age=week, finish_reason="stop", turns=2),
        refused(7, age=week),
        record(8, age=20 * 86400.0, finish_reason="stop", turns=2),  # outside the window
        record(9, age=day, lane="glm", finish_reason="stop", turns=2),
        refused(10, age=day, lane="glm"),
    ]
    write_trace(tmp_path, lines)
    assert counts(tmp_path) == (4, 5)
    assert counts(tmp_path, "glm") == (1, 1)


def test_second_snapshot_is_stable_and_incremental(tmp_path):
    write_trace(tmp_path, [record(0, finish_reason="stop", turns=1), refused(1)])
    assert counts(tmp_path) == (1, 1)
    assert counts(tmp_path) == (1, 1)
    with (tmp_path / "trace.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(refused(2) + "\n")
        handle.write(record(3, finish_reason="stop", turns=1) + "\n")
        handle.write(refused(4) + "\n")
    assert counts(tmp_path) == (2, 2)
    assert counts(tmp_path) == (2, 2)


def test_run_counts_function_directly(tmp_path):
    from datetime import datetime

    write_trace(tmp_path, [refused(0), refused(1), record(2, finish_reason="stop", turns=1)])
    settings = e1.build_settings(tmp_path, (LANE, "glm"))
    got = lane_status._run_counts(settings, datetime.now().astimezone())
    assert got == {LANE: {"day": 1, "week": 1}}


CAP2_PLAN = """## Usage envelopes (test)

| Lane | Measured so far | Envelope | Reserve (never planned) |
|---|---|---|---|
| Gemini | test | at most 2 runs a day | none |
"""


def test_envelope_check_not_blocked_by_refusals_alone(make_env, tmp_path):
    env = make_env((LANE,), plan=e1.write_plan(tmp_path, CAP2_PLAN))
    e1.seed_runs(env.tmp_path, LANE, 1)
    with (env.tmp_path / "trace.jsonl").open("a", encoding="utf-8") as handle:
        for i in range(3):
            handle.write(refused(100 + i) + "\n")
    run = env.run(LANE)
    assert run.state == COMPLETED, run.error
    assert len(env.spawned) == 1


def test_envelope_check_still_refuses_at_cap_of_real_runs(make_env, tmp_path):
    env = make_env((LANE,), plan=e1.write_plan(tmp_path, CAP2_PLAN))
    e1.seed_runs(env.tmp_path, LANE, 2)
    run = env.run(LANE)
    assert run.state == FAILED and env.spawned == []
    assert run.hops[0]["code"] == "envelope"
